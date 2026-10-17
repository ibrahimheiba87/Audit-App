"""Tick marks and cross-references on the lead sheets, as in Caseware Working Papers and
DataSnipper: the auditor puts a tick from the firm's legend on an account ("agreed to the
bank confirmation") with a reference to where the work is, either a workpaper number typed
in or a link to a workpaper in the app (a sample, a confirmation, a bank reconciliation...).

The legend is the firm's; partners and admins change it. A tick keeps the meaning it had when
it was made. Ticks go in the lead sheets download, with the legend, and count as work on the
lead sheets for review sign-off.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user, log, require
from .db import (
    BankReconciliation,
    Confirmation,
    Control,
    DocumentRequest,
    Engagement,
    Expectation,
    Finding,
    Sample,
    TickMark,
    User,
    WorkpaperTick,
    get_session,
)
from .engagements import get_or_404

router = APIRouter(prefix="/api")

MAX_LEGEND = 50
MAX_TICKS = 5000  # per engagement
DEFAULTS = [
    ("✓", "Agreed to the supporting document"),
    ("F", "Footed (added up and agreed)"),
    ("PY", "Agreed to last year's audited figures"),
    ("TB", "Agreed to the trial balance"),
    ("B", "Agreed to the bank statement or confirmation"),
    ("C", "Agreed to a third-party confirmation"),
    ("R", "Recalculated without exception"),
    ("X", "Exception found; see the reference"),
]
# What a tick can link to: (label, model, how each record is named).
LINKS = {
    "sample": ("Sample", Sample, lambda x: f"Sample #{x.id} ({x.upload.filename})"),
    "confirmation": ("Confirmation", Confirmation, lambda x: f"Confirmation #{x.id} ({x.party})"),
    "bankrec": ("Bank reconciliation", BankReconciliation,
                lambda x: f"Bank reconciliation #{x.id}"),
    "finding": ("Finding", Finding,
                lambda x: f"Finding #{x.id} ({(x.detail or x.check_name)[:80]})"),
    "request": ("Document request", DocumentRequest,
                lambda x: f"Request {x.number} ({x.title})"),
    "expectation": ("Analytical procedure", Expectation,
                    lambda x: f"Analytical procedure #{x.id} ({x.title or x.kind})"),
    "control": ("Control", Control, lambda x: f"Control #{x.id} ({x.title})"),
}
MAX_LINKS = 200  # per kind, in the picker

Symbol = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8)]
Meaning = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Account = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class TickMarkIn(BaseModel):
    symbol: Symbol
    meaning: Meaning


class TickIn(BaseModel):
    account: Account
    symbol: Symbol
    reference: str = Field(default="", max_length=200)
    link: str = Field(default="", max_length=50)  # "<kind>:<id>", from refs
    note: str = Field(default="", max_length=2000)


def legend(session: Session) -> list[TickMark]:
    """The firm's legend, set up with the usual ticks the first time it is asked for."""
    marks = list(session.scalars(select(TickMark).order_by(TickMark.id)))
    if marks:
        return marks
    auth.hold(session, "tick-marks")
    session.expire_all()
    if not session.scalar(select(func.count()).select_from(TickMark)):
        session.add_all(TickMark(symbol=s, meaning=m, created_by="") for s, m in DEFAULTS)
    session.commit()
    return list(session.scalars(select(TickMark).order_by(TickMark.id)))


def mark_out(m: TickMark) -> dict:
    return {"id": m.id, "symbol": m.symbol, "meaning": m.meaning}


def tick_out(t: WorkpaperTick) -> dict:
    return {"id": t.id, "account": t.account, "symbol": t.symbol, "meaning": t.meaning,
            "reference": t.reference, "link_kind": t.link_kind, "link_id": t.link_id,
            "link_label": t.link_label, "note": t.note, "created_by": t.created_by,
            "created_at": t.created_at.isoformat()}


def refs(session: Session, e: Engagement) -> list[dict]:
    """The workpapers in the engagement a tick can link to."""
    out = []
    for kind, (group, model, name) in LINKS.items():
        for x in session.scalars(select(model).where(model.engagement_id == e.id)
                                 .order_by(model.id).limit(MAX_LINKS)):
            out.append({"link": f"{kind}:{x.id}", "group": group, "label": name(x)})
    return out


def resolve(session: Session, e: Engagement, link: str) -> tuple[str, int | None, str]:
    if not link:
        return "", None, ""
    kind, _, id_ = link.partition(":")
    if kind not in LINKS or not (id_.isascii() and id_.isdigit() and len(id_) <= 9):
        raise HTTPException(400, "Pick a workpaper to link to from the list.")
    _, model, name = LINKS[kind]
    x = session.get(model, int(id_))
    if x is None or x.engagement_id != e.id:
        raise HTTPException(400, "That workpaper isn't in this engagement.")
    return kind, x.id, name(x)[:300]


@router.get("/tick-marks")
def list_marks(_: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> list[dict]:
    return [mark_out(m) for m in legend(session)]


@router.post("/tick-marks", status_code=201)
def add_mark(body: TickMarkIn, user: User = Depends(require("manage")),
             session: Session = Depends(get_session)) -> list[dict]:
    legend(session)
    auth.hold(session, "tick-marks")
    session.expire_all()
    marks = list(session.scalars(select(TickMark)))
    if any(m.symbol == body.symbol for m in marks):
        raise HTTPException(409, "That symbol is already in the legend.")
    if len(marks) >= MAX_LEGEND:
        raise HTTPException(400, f"The legend can have up to {MAX_LEGEND} tick marks.")
    session.add(TickMark(symbol=body.symbol, meaning=body.meaning, created_by=user.name))
    log(session, user, "tick.legend", f"Added the tick mark {body.symbol} ({body.meaning}) "
        "to the firm's legend", details={"symbol": body.symbol, "meaning": body.meaning})
    session.commit()
    return [mark_out(m) for m in legend(session)]


@router.delete("/tick-marks/{tick_mark_id}")
def delete_mark(tick_mark_id: int, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> list[dict]:
    auth.hold(session, "tick-marks")
    session.expire_all()
    m = get_or_404(session, TickMark, tick_mark_id)
    if session.scalar(select(func.count()).select_from(TickMark)) <= 1:
        raise HTTPException(400, "Keep at least one tick mark in the legend.")
    log(session, user, "tick.legend", f"Took the tick mark {m.symbol} ({m.meaning}) out of the "
        "firm's legend", details={"symbol": m.symbol, "meaning": m.meaning})
    session.delete(m)
    session.commit()
    return [mark_out(x) for x in legend(session)]


@router.get("/engagements/{engagement_id}/ticks")
def list_ticks(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    marks = legend(session)
    e = get_or_404(session, Engagement, engagement_id, user)
    return {"ticks": [tick_out(t) for t in e.ticks], "legend": [mark_out(m) for m in marks],
            "refs": refs(session, e)}


@router.post("/engagements/{engagement_id}/ticks", status_code=201)
def add_tick(engagement_id: int, body: TickIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    get_or_404(session, Engagement, engagement_id, user)
    legend(session)  # set up the usual ticks the first time
    auth.hold(session, f"ticks:{engagement_id}")
    session.expire_all()
    e = get_or_404(session, Engagement, engagement_id, user)
    mark = session.scalar(select(TickMark).where(TickMark.symbol == body.symbol))
    if mark is None:
        raise HTTPException(400, "Pick a tick mark from the firm's legend.")
    if len(e.ticks) >= MAX_TICKS:
        raise HTTPException(400, f"An engagement can have up to {MAX_TICKS} tick marks.")
    kind, link_id, link_label = resolve(session, e, body.link)
    t = WorkpaperTick(engagement=e, account=body.account, symbol=mark.symbol,
                      meaning=mark.meaning, reference=body.reference.strip(), link_kind=kind,
                      link_id=link_id, link_label=link_label, note=body.note.strip(),
                      created_by=user.name)
    session.add(t)
    session.flush()
    where = ", ".join(x for x in (t.reference, t.link_label) if x)
    log(session, user, "tick.add", f"Ticked account {t.account} {t.symbol}"
        + (f" ({where})" if where else ""), e,
        details={"tick_id": t.id, "account": t.account, "symbol": t.symbol,
                 "meaning": t.meaning, "reference": t.reference, "link": body.link})
    session.commit()
    return tick_out(t)


@router.delete("/ticks/{tick_id}", status_code=204)
def delete_tick(tick_id: int, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> Response:
    t = get_or_404(session, WorkpaperTick, tick_id, user)
    log(session, user, "tick.delete", f"Took the tick {t.symbol} off account {t.account}",
        t.engagement, details={"tick_id": t.id, "account": t.account, "symbol": t.symbol})
    session.delete(t)
    session.commit()
    return Response(status_code=204)


def sheets(e: Engagement, names: dict[str, str]) -> dict[str, list[dict]]:
    """The ticks and the legend, for the lead sheets workbook. `names` gives each account's
    name."""
    if not e.ticks:
        return {}
    used = dict.fromkeys((t.symbol, t.meaning) for t in e.ticks)  # a symbol can change meaning
    return {
        "Tick marks": [{"Account": t.account, "Name": names.get(t.account, ""),
                        "Tick": t.symbol, "Meaning": t.meaning, "Reference": t.reference,
                        "Linked workpaper": t.link_label, "Note": t.note,
                        "By": t.created_by, "When (UTC)": f"{t.created_at:%Y-%m-%d %H:%M}"}
                       for t in e.ticks],
        "Tick legend": [{"Tick": s, "Meaning": m} for s, m in used],
    }
