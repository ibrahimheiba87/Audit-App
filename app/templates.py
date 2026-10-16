"""Firm templates for the journal entry checks: a named set of settings (cutoff window,
round amount threshold, weekend, words to look for) that partners set up once and teams pick
on any engagement. One can be the firm default, which new engagements start from.

A run made with a template's settings unchanged records the template's name, so the report
says which firm settings were used.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import auth, checks
from .auth import log, require
from .db import TestTemplate, User, get_session, utcnow

router = APIRouter(prefix="/api")
MAX_TEMPLATES = 100
GUARD = "test-templates"


class TemplateIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    cutoff_days: int = Field(default=5, ge=0, le=366)
    round_threshold: float = Field(default=1000.0, gt=0, allow_inf_nan=False)
    weekend: Literal["sat-sun", "fri-sat", "fri"] = "sat-sun"
    keywords: list[str] = Field(default_factory=list, max_length=1000)
    is_default: bool = False


def settings_of(body: TemplateIn) -> dict:
    return {"cutoff_days": body.cutoff_days, "round_threshold": body.round_threshold,
            "weekend": body.weekend, "keywords": list(checks.parse_keywords(body.keywords))}


def template_out(t: TestTemplate) -> dict:
    return {"id": t.id, "name": t.name, **t.settings, "is_default": t.is_default,
            "updated_by": t.updated_by, "updated_at": t.updated_at.isoformat()}


def matching(session: Session, template_id: int | None, settings: dict) -> TestTemplate | None:
    """The template, if a run's settings are exactly its settings."""
    if template_id is None:
        return None
    t = session.get(TestTemplate, template_id)
    if t is None:
        return None
    mine = {k: settings[k] for k in ("cutoff_days", "round_threshold", "weekend", "keywords")}
    return t if mine == {**t.settings, "keywords": list(t.settings["keywords"])} else None


def held(session: Session) -> None:
    """Take turns, so two partners can't both make a default or the same name."""
    auth.hold(session, GUARD)
    session.expire_all()


def check_name(session: Session, name: str, template_id: int | None = None) -> None:
    clash = session.scalar(select(TestTemplate).where(TestTemplate.name == name))
    if clash is not None and clash.id != template_id:
        raise HTTPException(409, f"There's already a template called {name}.")


def make_default(session: Session, t: TestTemplate, is_default: bool) -> None:
    if is_default:
        session.execute(update(TestTemplate).where(TestTemplate.id != t.id)
                        .values(is_default=False))
    t.is_default = is_default


@router.get("/test-templates")
def list_templates(_: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> list[dict]:
    return [template_out(t) for t in session.scalars(select(TestTemplate)
                                                     .order_by(TestTemplate.name))]


@router.post("/test-templates", status_code=201)
def create_template(body: TemplateIn, user: User = Depends(require("manage")),
                    session: Session = Depends(get_session)) -> dict:
    held(session)
    check_name(session, body.name)
    if len(session.scalars(select(TestTemplate.id)).all()) >= MAX_TEMPLATES:
        raise HTTPException(400, f"There can be at most {MAX_TEMPLATES} templates.")
    t = TestTemplate(name=body.name, settings=settings_of(body), updated_by=user.name,
                     updated_at=utcnow())
    session.add(t)
    session.flush()
    make_default(session, t, body.is_default)
    log(session, user, "template.create", f"Added the test template {t.name}"
        + (" as the firm default" if t.is_default else ""),
        details={"template_id": t.id, "settings": t.settings, "is_default": t.is_default})
    session.commit()
    return template_out(t)


@router.put("/test-templates/{template_id}")
def update_template(template_id: int, body: TemplateIn, user: User = Depends(require("manage")),
                    session: Session = Depends(get_session)) -> dict:
    held(session)
    t = session.get(TestTemplate, template_id)
    if t is None:
        raise HTTPException(404, "Template not found.")
    check_name(session, body.name, t.id)
    before = template_out(t)
    t.name, t.settings = body.name, settings_of(body)
    make_default(session, t, body.is_default)
    after = template_out(t)
    if {k: v for k, v in before.items() if k not in ("updated_by", "updated_at")} != \
            {k: v for k, v in after.items() if k not in ("updated_by", "updated_at")}:
        t.updated_by, t.updated_at = user.name, utcnow()
        log(session, user, "template.update", f"Changed the test template {t.name}"
            + (" and made it the firm default" if t.is_default and not before["is_default"]
               else ""),
            details={"template_id": t.id, "before": before, "settings": t.settings,
                     "is_default": t.is_default})
    session.commit()
    return template_out(t)


@router.delete("/test-templates/{template_id}", status_code=204)
def delete_template(template_id: int, user: User = Depends(require("manage")),
                    session: Session = Depends(get_session)) -> Response:
    held(session)
    t = session.get(TestTemplate, template_id)
    if t is None:
        raise HTTPException(404, "Template not found.")
    log(session, user, "template.delete", f"Deleted the test template {t.name}",
        details={"template_id": t.id, "settings": t.settings})
    session.delete(t)
    session.commit()
    return Response(status_code=204)
