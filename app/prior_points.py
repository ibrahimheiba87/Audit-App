"""Following up last year's points (ISA 265 and ISA 710 in practice): the management letter
points, the findings added by hand that weren't cleared, and the points still open the year
before are brought into this year's file, where the auditor records whether the client dealt
with each one and how that was checked.

Points come in when an engagement is rolled forward, or by bringing them in from an earlier
engagement of the same client (for files started before this was here). A point that is not
resolved can become one of this year's findings with one click. A reviewer signs each point
off once it is followed up; a change afterwards takes the sign-off away.
"""

from __future__ import annotations

import io
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user, log, require
from .db import AiDraft, Engagement, Finding, FindingNote, PriorPoint, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem, visible_engagements
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

ORIGINS = {"letter": "Management letter", "finding": "Finding", "point": "Still open last year",
           "manual": "Added by hand"}
STATUSES = {"to_follow_up": "To follow up", "resolved": "Resolved",
            "partly": "Partly resolved", "not_resolved": "Not resolved",
            "not_applicable": "No longer applies"}
PRIORITIES = ("high", "medium", "low")
STILL_OPEN = ("to_follow_up", "partly", "not_resolved")  # carried again next year
MAX_POINTS = 300
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Status = Literal[tuple(STATUSES)]  # type: ignore[valid-type]
Priority = Literal[PRIORITIES]  # type: ignore[valid-type]


class PointIn(BaseModel):
    title: Title
    detail: Long = ""
    recommendation: Long = ""
    priority: Priority = "medium"


class PointPatch(BaseModel):
    title: Title | None = None
    detail: Long | None = None
    recommendation: Long | None = None
    priority: Priority | None = None
    management_response: Long | None = None
    status: Status | None = None
    follow_up: Long | None = None
    reviewed: bool | None = None


class ImportIn(BaseModel):
    from_engagement_id: int


# --- What carries forward ------------------------------------------------------------

def first_line(text: str, limit: int = 300) -> tuple[str, str]:
    """A title from a finding's text, and the rest of it."""
    text = (text or "").strip()
    head, _, rest = text.partition("\n")
    if len(head) > limit:
        return head[:limit - 1].rstrip() + "…", text
    return head, rest.strip()


def points_from(old: Engagement) -> list[dict]:
    """The points last year's file leaves for this year: its latest management letter's
    points, findings added by hand and not cleared (unless a letter point covers them), and
    its own points from the year before that are still open."""
    out: list[dict] = []
    letters = [d for d in old.ai_drafts if d.kind == "letter"]
    # Findings a letter point or one of last year's points already stands for.
    covered: set[int] = {p.finding_id for p in old.prior_points if p.finding_id}
    if letters:
        letter: AiDraft = max(letters, key=lambda d: (d.created_at, d.id))
        for n, p in enumerate(letter.content.get("points", []), 1):
            covered.update(p.get("finding_ids") or [])
            detail = "\n\n".join(x for x in (p.get("observation", ""),
                                              p.get("implication", "")) if x)
            out.append({"origin": "letter", "ref": f"letter:{letter.id}:{n}",
                        "title": p.get("title", "")[:300], "detail": detail,
                        "recommendation": p.get("recommendation", ""),
                        "priority": p.get("priority") if p.get("priority") in PRIORITIES
                        else "medium", "first_raised": old.name})
    for f in old.findings:
        if f.source != "manual" or f.status == "cleared" or f.id in covered:
            continue
        title, rest = first_line(f.detail or f.description or f.check_name)
        out.append({"origin": "finding", "ref": f"finding:{f.id}", "title": title or
                    f"Finding #{f.id}", "detail": rest, "recommendation": "",
                    "priority": f.severity if f.severity in PRIORITIES else "medium",
                    "first_raised": old.name})
    for p in old.prior_points:
        if p.status not in STILL_OPEN:
            continue
        out.append({"origin": "point", "ref": f"point:{p.id}", "title": p.title,
                    "detail": "\n\n".join(x for x in (p.detail, p.follow_up and
                                                      f"Last year: {p.follow_up}") if x),
                    "recommendation": p.recommendation, "priority": p.priority,
                    "first_raised": p.first_raised or old.name,
                    "management_response": p.management_response})
    return out


def carry(new: Engagement, old: Engagement, user: User) -> int:
    """Add last year's points not already brought in; how many were added."""
    have = {p.ref for p in new.prior_points if p.ref}
    added = 0
    for p in points_from(old):
        if p["ref"] in have or len(new.prior_points) >= MAX_POINTS:
            continue
        new.prior_points.append(PriorPoint(
            **p, from_engagement_id=old.id, from_name=old.name, created_by=user.name,
            updated_by=user.name))
        added += 1
    return added


# --- Showing it ----------------------------------------------------------------------

def point_out(p: PriorPoint) -> dict:
    return {
        "id": p.id, "engagement_id": p.engagement_id, "origin": p.origin,
        "origin_label": ORIGINS[p.origin], "from_name": p.from_name,
        "first_raised": p.first_raised, "title": p.title, "detail": p.detail,
        "recommendation": p.recommendation, "priority": p.priority,
        "management_response": p.management_response, "status": p.status,
        "status_label": STATUSES[p.status], "open": p.status == "to_follow_up",
        "follow_up": p.follow_up, "finding_id": p.finding_id,
        "repeated": p.origin == "point",  # raised at least two years running
        "checked_by": p.checked_by, "checked_at": p.checked_at.isoformat() if p.checked_at
        else None, "reviewed_by": p.reviewed_by,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "updated_by": p.updated_by, "updated_at": p.updated_at.isoformat()}


def summary(e: Engagement) -> dict:
    points = e.prior_points
    counts = {s: sum(1 for p in points if p.status == s) for s in STATUSES}
    return {"points": len(points), "statuses": counts,
            "not_reviewed": sum(1 for p in points if p.status != "to_follow_up"
                                and not p.reviewed_at),
            # Not resolved and not yet carried into this year's findings.
            "unresolved_without_finding": sum(1 for p in points if p.status in
                                              ("not_resolved", "partly")
                                              and p.finding_id is None)}


def sources(session: Session, user: User, e: Engagement) -> list[dict]:
    """The client's earlier engagements the user can see, latest first, to bring points from."""
    earlier = [x for x in e.client.engagements
               if x.id != e.id and x.period_end < e.period_end]
    seen = visible_engagements(session, user, earlier)
    return [{"id": x.id, "name": x.name, "period_end": x.period_end.isoformat()}
            for x in sorted(seen, key=lambda x: x.period_end, reverse=True)]


def list_out(session: Session, user: User, e: Engagement) -> dict:
    return {"points": [point_out(p) for p in e.prior_points], "summary": summary(e),
            "sources": sources(session, user, e)}


# --- Routes ----------------------------------------------------------------------------

@router.get("/engagements/{engagement_id}/prior-points")
def list_points(engagement_id: int, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict:
    return list_out(session, user, get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/prior-points/import")
def import_points(engagement_id: int, body: ImportIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"prior:{engagement_id}")
    old = get_or_404(session, Engagement, body.from_engagement_id, user)
    if old.client_id != e.client_id or old.period_end >= e.period_end:
        raise HTTPException(400, "Pick an earlier engagement of the same client.")
    added = carry(e, old, user)
    session.flush()
    log(session, user, "prior.import", f"Brought in {added} points from {old.name}", e,
        details={"from_engagement_id": old.id, "added": added})
    session.commit()
    return {**list_out(session, user, e), "added": added}


@router.post("/engagements/{engagement_id}/prior-points", status_code=201)
def add_point(engagement_id: int, body: PointIn, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"prior:{engagement_id}")
    if len(e.prior_points) >= MAX_POINTS:
        raise HTTPException(400, f"An engagement can have at most {MAX_POINTS} points from "
                                 "last year.")
    p = PriorPoint(engagement=e, origin="manual", title=body.title, detail=body.detail,
                   recommendation=body.recommendation, priority=body.priority,
                   created_by=user.name, updated_by=user.name)
    session.add(p)
    session.flush()
    log(session, user, "prior.create", f"Added last year's point {p.title}", e,
        details={"point_id": p.id})
    session.commit()
    return list_out(session, user, e)


def held(session: Session, user: User, point_id: int) -> PriorPoint:
    engagement_id = get_or_404(session, PriorPoint, point_id, user).engagement_id
    auth.hold(session, f"prior:{engagement_id}")
    session.expire_all()
    return get_or_404(session, PriorPoint, point_id, user)


@router.patch("/prior-points/{point_id}")
def update_point(point_id: int, body: PointPatch, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    p = held(session, user, point_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "reviewed" in changes and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can sign off a point.")
    edits = {k: v for k, v in changes.items() if k != "reviewed"}
    for field, v in edits.items():
        setattr(p, field, v)
    if p.status != "to_follow_up" and not p.follow_up.strip():
        raise HTTPException(400, "Say how the point was followed up.")
    if "status" in edits or "follow_up" in edits:
        p.checked_by, p.checked_at = user.name, utcnow()
    if edits and p.reviewed_at:
        p.reviewed_by, p.reviewed_at = "", None  # it changed since it was reviewed
    if changes.get("reviewed") is True:
        if p.status == "to_follow_up":
            raise HTTPException(400, "Follow the point up before signing it off.")
        p.reviewed_by, p.reviewed_at = user.name, utcnow()
    elif changes.get("reviewed") is False:
        p.reviewed_by, p.reviewed_at = "", None
    p.updated_by, p.updated_at = user.name, utcnow()
    log(session, user, "prior.update", f"Changed last year's point {p.title}", p.engagement,
        details={"point_id": p.id, "changed": sorted(changes), "status": p.status,
                 "reviewed": bool(p.reviewed_at)})
    session.commit()
    return list_out(session, user, p.engagement)


@router.post("/prior-points/{point_id}/finding")
def raise_finding(point_id: int, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    """Make a point that is still not resolved one of this year's findings."""
    p = held(session, user, point_id)
    e = p.engagement
    if p.status not in ("not_resolved", "partly"):
        raise HTTPException(400, "Only a point that is not resolved becomes a finding.")
    if p.finding_id is not None and session.get(Finding, p.finding_id) is not None:
        raise HTTPException(409, "This point is already one of this year's findings.")
    text = "\n\n".join(x for x in (
        p.title, p.detail, p.follow_up and f"Followed up: {p.follow_up}",
        p.recommendation and f"Recommendation: {p.recommendation}") if x)
    raised = p.first_raised or p.from_name or "an earlier year"
    f = Finding(engagement=e, source="manual", severity=p.priority, detail=text,
                description=f"Raised in {raised}, not resolved"[:300])
    f.notes.append(FindingNote(text="Repeated from last year's points", author=user.name))
    session.add(f)
    session.flush()
    p.finding_id, p.updated_by, p.updated_at = f.id, user.name, utcnow()
    log(session, user, "finding.create", f"Added finding #{f.id}: {p.title}", e,
        details={"finding_id": f.id, "severity": f.severity, "point_id": p.id})
    log(session, user, "prior.finding", f"Made last year's point {p.title} finding #{f.id}",
        e, details={"point_id": p.id, "finding_id": f.id})
    session.commit()
    return {**list_out(session, user, e), "finding_id": f.id}


@router.delete("/prior-points/{point_id}")
def delete_point(point_id: int, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    p = held(session, user, point_id)
    e = p.engagement
    if p.reviewed_at and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can remove a point that was signed off.")
    log(session, user, "prior.delete", f"Removed last year's point {p.title}", e,
        details={"point_id": p.id, "origin": p.origin, "ref": p.ref})
    e.prior_points.remove(p)
    session.commit()
    return list_out(session, user, e)


# --- Excel -----------------------------------------------------------------------------

def to_excel(e: Engagement) -> bytes:
    s = summary(e)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Points", s["points"]),
             *((STATUSES[k], v) for k, v in s["statuses"].items())]
    cols = ["Ref", "Point", "From", "First raised", "Priority", "Detail", "Recommendation",
            "Management's response", "Status", "How it was followed up", "This year's finding",
            "Checked by", "Reviewed by"]
    rows = [{"Ref": f"PY{n}", "Point": p.title, "From": ORIGINS[p.origin],
             "First raised": p.first_raised, "Priority": p.priority, "Detail": p.detail,
             "Recommendation": p.recommendation, "Management's response": p.management_response,
             "Status": STATUSES[p.status], "How it was followed up": p.follow_up,
             "This year's finding": f"#{p.finding_id}" if p.finding_id else "",
             "Checked by": p.checked_by, "Reviewed by": p.reviewed_by}
            for n, p in enumerate(e.prior_points, 1)]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows, columns=cols).to_excel(xl, sheet_name="Last year's points",
                                                  index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def workbook(e: Engagement) -> tuple[str, bytes]:
    stem = safe_stem(f"{e.client.name}-{e.name}-last-years-points")
    return f"{stem}.xlsx", to_excel(e)


@router.get("/engagements/{engagement_id}/prior-points/export")
def export_points(engagement_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(e)
    log(session, user, "prior.download", "Downloaded the follow-up of last year's points", e,
        details={"points": len(e.prior_points)})
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))

