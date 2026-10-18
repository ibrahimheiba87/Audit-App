"""Firm request lists, as Inflo and Caseware keep standard "prepared by client" lists: the
documents a firm asks every client of a kind for, saved once and sent on any engagement in
one go.

Each item has the document, optional details for the client, and when it is due as a number
of days after the period end, so the same list fits any year end. Sending a list adds a client
request for each item the engagement doesn't already have (matched by the document's name).
A partner can also save an engagement's requests as a new firm list.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, portal
from .auth import log, require
from .db import DocumentRequest, Engagement, Guard, RequestTemplate, User, get_session, utcnow
from .engagements import get_or_404

router = APIRouter(prefix="/api")
GUARD = "request-templates"
MAX_LISTS = 100
MAX_ITEMS = 200

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ItemIn(BaseModel):
    title: Name
    details: str = Field(default="", max_length=5000)
    due_days: int | None = Field(default=None, ge=-366, le=366)


class ListIn(BaseModel):
    name: Name
    items: list[ItemIn] = Field(min_length=1, max_length=MAX_ITEMS)


class SendIn(BaseModel):
    template_id: int


class SaveIn(BaseModel):
    name: Name


def list_out(t: RequestTemplate) -> dict:
    return {"id": t.id, "name": t.name, "items": t.items, "updated_by": t.updated_by,
            "updated_at": t.updated_at.isoformat()}


def items_of(body: ListIn) -> list[dict]:
    seen, items = set(), []
    for it in body.items:
        key = it.title.casefold()
        if key in seen:
            raise HTTPException(400, f"The list has {it.title} twice.")
        seen.add(key)
        items.append({"title": it.title, "details": it.details.strip(),
                      "due_days": it.due_days})
    return items


def held(session: Session) -> None:
    auth.hold(session, GUARD)
    session.expire_all()


def check_name(session: Session, name: str, template_id: int | None = None) -> None:
    clash = session.scalar(select(RequestTemplate).where(RequestTemplate.name == name))
    if clash is not None and clash.id != template_id:
        raise HTTPException(409, f"There's already a request list called {name}.")


def check_count(session: Session) -> None:
    if len(session.scalars(select(RequestTemplate.id)).all()) >= MAX_LISTS:
        raise HTTPException(400, f"The firm can have up to {MAX_LISTS} request lists.")


def list_or_404(session: Session, template_id: int) -> RequestTemplate:
    t = session.get(RequestTemplate, template_id)
    if t is None:
        raise HTTPException(404, "Request list not found.")
    return t


@router.get("/request-templates")
def list_lists(_: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> list[dict]:
    return [list_out(t) for t in session.scalars(select(RequestTemplate)
                                                 .order_by(RequestTemplate.name))]


@router.post("/request-templates", status_code=201)
def create_list(body: ListIn, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> dict:
    items = items_of(body)
    held(session)
    check_name(session, body.name)
    check_count(session)
    t = RequestTemplate(name=body.name, items=items, updated_by=user.name, updated_at=utcnow())
    session.add(t)
    session.flush()
    log(session, user, "request_list.create", f"Added the firm request list {t.name}",
        details={"template_id": t.id, "items": items})
    session.commit()
    return list_out(t)


@router.put("/request-templates/{template_id}")
def update_list(template_id: int, body: ListIn, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> dict:
    items = items_of(body)
    held(session)
    t = list_or_404(session, template_id)
    check_name(session, body.name, t.id)
    before = list_out(t)
    t.name, t.items = body.name, items
    t.updated_by, t.updated_at = user.name, utcnow()
    log(session, user, "request_list.update", f"Changed the firm request list {t.name}",
        details={"template_id": t.id, "before": before, "items": items})
    session.commit()
    return list_out(t)


@router.delete("/request-templates/{template_id}", status_code=204)
def delete_list(template_id: int, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> Response:
    held(session)
    t = list_or_404(session, template_id)
    log(session, user, "request_list.delete", f"Deleted the firm request list {t.name}",
        details={"template_id": t.id, "items": t.items})
    session.delete(t)
    session.commit()
    return Response(status_code=204)


@router.post("/engagements/{engagement_id}/requests/from-template")
def send_list(engagement_id: int, body: SendIn, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)  # before holding the counter
    t = list_or_404(session, body.template_id)

    def missing(e: Engagement) -> list[dict]:
        asked = {r.title.casefold() for r in e.document_requests}
        return [it for it in t.items if it["title"].casefold() not in asked]

    reqs = []
    if missing(e):  # only then take numbers, so sending a list again uses none up
        first = portal.next_number(session, engagement_id)  # holds the counter
        session.expire_all()  # requests may have been added before we held it
        e = get_or_404(session, Engagement, engagement_id, user)
        for i, item in enumerate(missing(e)):
            due = (e.period_end + timedelta(days=item["due_days"])
                   if item.get("due_days") is not None else None)
            reqs.append(DocumentRequest(engagement=e, number=first + i, title=item["title"],
                                        details=item.get("details", ""), due_date=due,
                                        created_by=user.name))
        session.add_all(reqs)  # before the Guard lookup autoflushes
        if reqs:
            session.get(Guard, f"request-number:{engagement_id}").n = first + len(reqs) - 1
        session.flush()
    added, skipped = len(reqs), len(t.items) - len(reqs)
    for req in reqs:
        log(session, user, "request.create", f"Asked the client for {portal.label(req)}", e,
            details={"request_id": req.id, "due_date": str(req.due_date or ""),
                     "template_id": t.id})
    log(session, user, "request.template", f"Sent the firm request list {t.name}: {added} "
        f"requests added, {skipped} already asked for", e,
        details={"template_id": t.id, "added": added, "skipped": skipped,
                 "request_ids": [r.id for r in reqs]})
    session.commit()
    return {"added": added, "skipped": skipped}


@router.post("/engagements/{engagement_id}/requests/save-template", status_code=201)
def save_list(engagement_id: int, body: SaveIn, user: User = Depends(require("manage")),
              session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    seen, items = set(), []
    for r in e.document_requests:
        if r.title.casefold() in seen:
            continue
        seen.add(r.title.casefold())
        items.append({"title": r.title, "details": r.details,
                      "due_days": max(-366, min(366, (r.due_date - e.period_end).days))
                      if r.due_date else None})
    if not items:
        raise HTTPException(400, "This engagement has no requests to save.")
    items = items[:MAX_ITEMS]
    held(session)
    check_name(session, body.name)
    check_count(session)
    t = RequestTemplate(name=body.name, items=items, updated_by=user.name, updated_at=utcnow())
    session.add(t)
    session.flush()
    log(session, user, "request_list.create", f"Saved the requests as the firm request list "
        f"{t.name}", e, details={"template_id": t.id, "items": items})
    session.commit()
    return list_out(t)
