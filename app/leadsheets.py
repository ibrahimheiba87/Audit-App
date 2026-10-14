"""Endpoints for account mapping, adjusting entries, lead sheets and draft statements."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, ratios, statements
from .auth import current_user, log, require
from .db import AccountMapping, AdjustingEntry, AdjustingLine, Engagement, Guard, Upload, User, get_session
from .engagements import get_or_404, held_engagement, load_upload, materiality_out, safe_stem, upload_out
from .exports import attachment
from .statements import ENTRY_KINDS, LINE

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

Account = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Kind = Literal["adjusting", "reclassification", "proposed"]
MAX_AMOUNT = 1e13  # far above any real balance, small enough that sums stay exact to the cent


class MappingIn(BaseModel):
    mappings: dict[Account, str | None] = Field(max_length=5000)


class EntryLineIn(BaseModel):
    account: Account
    debit: float = Field(default=0.0, ge=0, le=MAX_AMOUNT, allow_inf_nan=False)
    credit: float = Field(default=0.0, ge=0, le=MAX_AMOUNT, allow_inf_nan=False)
    memo: str = Field(default="", max_length=500)


class EntryIn(BaseModel):
    kind: Kind = "adjusting"
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                  max_length=2000)]
    date: dt.date | None = None
    lines: list[EntryLineIn] = Field(min_length=2, max_length=200)


class EntryPatch(BaseModel):
    kind: Kind


def entry_ref(e: AdjustingEntry) -> str:
    return f"{ENTRY_KINDS[e.kind]['prefix']}-{e.number}"


def entry_out(e: AdjustingEntry) -> dict:
    lines = [{"account": ln.account, "debit": ln.debit, "credit": ln.credit, "memo": ln.memo}
             for ln in e.lines]
    return {"id": e.id, "kind": e.kind, "kind_label": ENTRY_KINDS[e.kind]["label"],
            "booked": ENTRY_KINDS[e.kind]["booked"], "number": e.number, "ref": entry_ref(e),
            "description": e.description, "date": e.date, "author": e.author,
            "created_at": e.created_at.isoformat(), "lines": lines,
            "total": round(sum(ln["debit"] for ln in lines), 2)}


def pick_upload(e: Engagement, kind: str, upload_id: int | None) -> Upload | None:
    choices = [u for u in e.uploads if u.kind == kind]
    if upload_id is None:
        return choices[-1] if choices else None
    for u in choices:
        if u.id == upload_id:
            return u
    raise HTTPException(400, "That file isn't a trial balance of the right type for this "
                             "engagement.")


def compute(session: Session, e: Engagement, upload_id: int | None,
            prior_upload_id: int | None) -> tuple[dict, Upload | None, Upload | None]:
    tb = pick_upload(e, "tb", upload_id)
    prior = pick_upload(e, "prior_tb", prior_upload_id)
    if tb is None:
        return {}, None, prior
    mapping = {m.account: m.line for m in e.account_mappings}
    entries = [entry_out(x) for x in e.adjusting_entries]
    try:
        data = statements.build(load_upload(tb).df,
                                load_upload(prior).df if prior else None,
                                mapping, entries, materiality_out(e))
    except statements.StatementsError as exc:
        raise HTTPException(400, str(exc)) from exc
    data["ratios"] = ratios.compute(data)
    return data, tb, prior


@router.get("/engagements/{engagement_id}/statements")
def get_statements(engagement_id: int, upload_id: int | None = None,
                   prior_upload_id: int | None = None, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    data, tb, prior = compute(session, e, upload_id, prior_upload_id)
    return {
        **data,
        "tb": upload_out(tb) if tb else None,
        "prior_tb": upload_out(prior) if prior else None,
        "tb_choices": [upload_out(u) for u in e.uploads if u.kind == "tb"],
        "prior_choices": [upload_out(u) for u in e.uploads if u.kind == "prior_tb"],
        "entries": [entry_out(x) for x in reversed(e.adjusting_entries)],
        "entry_kinds": [{"id": k, **v} for k, v in ENTRY_KINDS.items()],
        "all_lines": list(LINE.values()),
    }


@router.put("/engagements/{engagement_id}/mapping")
def save_mapping(engagement_id: int, body: MappingIn, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    """Map accounts to FS lines; a null line removes the mapping."""
    # So two saves can't both add an account.
    e = held_engagement(session, user, engagement_id, f"mapping:{engagement_id}")
    bad = sorted({v for v in body.mappings.values() if v is not None and v not in LINE})
    if bad:
        raise HTTPException(400, f"Unknown financial statement line: {', '.join(bad)}.")
    existing = {m.account: m for m in e.account_mappings}
    changed = {}
    for account, line in body.mappings.items():
        m = existing.get(account)
        if line is None:
            if m is not None:
                session.delete(m)
                changed[account] = None
        elif m is None:
            e.account_mappings.append(AccountMapping(account=account, line=line,
                                                     updated_by=user.name))
            changed[account] = line
        elif m.line != line:
            m.line, m.updated_by = line, user.name
            changed[account] = line
    if changed:
        sample = ", ".join(f"{a} to {LINE[v]['label'] if v else 'unmapped'}"
                           for a, v in list(changed.items())[:5])
        more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
        log(session, user, "mapping.update",
            f"Mapped {len(changed)} account{'s' if len(changed) != 1 else ''}: {sample}{more}",
            e, details={"mappings": changed})
    session.commit()
    return {"changed": len(changed)}


@router.post("/engagements/{engagement_id}/adjusting-entries", status_code=201)
def create_entry(engagement_id: int, body: EntryIn, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    if ENTRY_KINDS[body.kind]["booked"]:
        auth.ensure(user, "review")  # as for marking a proposed entry booked
    lines = [{**ln.model_dump(), "debit": round(ln.debit, 2), "credit": round(ln.credit, 2)}
             for ln in body.lines]
    try:
        statements.check_entry_lines(lines)
    except statements.StatementsError as exc:
        raise HTTPException(400, str(exc)) from exc
    # So two new entries can't take the same number.
    e = held_engagement(session, user, engagement_id, f"entries:{engagement_id}")
    entry = AdjustingEntry(
        kind=body.kind, number=next_number(session, e.id, body.kind),
        description=body.description, date=body.date.isoformat() if body.date else None,
        author=user.name, lines=[AdjustingLine(account=ln["account"], debit=ln["debit"],
                                               credit=ln["credit"], memo=ln["memo"].strip())
                                 for ln in lines])
    e.adjusting_entries.append(entry)
    session.flush()
    out = entry_out(entry)
    log(session, user, "entry.create",
        f"Posted {out['ref']}: {entry.description} ({out['total']:,.2f})", e,
        details={"entry_id": entry.id, "ref": out["ref"], "lines": out["lines"]})
    session.commit()
    return out


def next_number(session: Session, engagement_id: int, kind: str) -> int:
    """The next entry number for a type. Numbers only go up, so a deleted entry's number isn't
    given to a later one. Call with the engagement's entries guard held."""
    highest = session.scalar(select(func.max(AdjustingEntry.number)).where(
        AdjustingEntry.engagement_id == engagement_id, AdjustingEntry.kind == kind)) or 0
    return _advance(session, engagement_id, kind, highest)


def _counter(session: Session, engagement_id: int, kind: str) -> Guard:
    key = f"entry-number:{engagement_id}:{kind}"
    counter = session.get(Guard, key)
    if counter is None:  # the first entry of this type, or one from before the counter existed
        counter = Guard(key=key, n=0)
        session.add(counter)
    return counter


def _advance(session: Session, engagement_id: int, kind: str, highest: int) -> int:
    counter = _counter(session, engagement_id, kind)
    counter.n = max(counter.n, highest) + 1
    session.flush()
    return counter.n


def use_number(session: Session, entry: AdjustingEntry) -> None:
    """Record an entry's number as used before it's deleted or retyped, so it isn't reused."""
    counter = _counter(session, entry.engagement_id, entry.kind)
    counter.n = max(counter.n, entry.number)


@router.patch("/adjusting-entries/{entry_id}")
def update_entry(entry_id: int, body: EntryPatch, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> dict:
    """Change an entry's type, e.g. a proposed entry the client has agreed to book."""
    engagement_id = get_or_404(session, AdjustingEntry, entry_id, user).engagement_id
    auth.hold(session, f"entries:{engagement_id}")
    session.expire_all()  # another request may have changed it before we held the guard
    entry = get_or_404(session, AdjustingEntry, entry_id, user)
    if body.kind != entry.kind:
        old = entry_ref(entry)
        use_number(session, entry)
        number = next_number(session, entry.engagement_id, body.kind)
        entry.kind, entry.number = body.kind, number
        log(session, user, "entry.update", f"Changed {old} to {entry_ref(entry)} "
            f"({ENTRY_KINDS[body.kind]['label'].lower()})", entry.engagement,
            details={"entry_id": entry.id, "from": old, "to": entry_ref(entry)})
        session.commit()
    return entry_out(entry)


@router.delete("/adjusting-entries/{entry_id}", status_code=204)
def delete_entry(entry_id: int, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> Response:
    engagement_id = get_or_404(session, AdjustingEntry, entry_id, user).engagement_id
    auth.hold(session, f"entries:{engagement_id}")
    session.expire_all()  # another request may have changed it before we held the guard
    entry = get_or_404(session, AdjustingEntry, entry_id, user)
    use_number(session, entry)
    out = entry_out(entry)
    log(session, user, "entry.delete", f"Deleted {out['ref']}: {entry.description}",
        entry.engagement, details={"entry_id": entry.id, "ref": out["ref"],
                                   "lines": out["lines"]})
    session.delete(entry)
    session.commit()
    return Response(status_code=204)


@router.get("/engagements/{engagement_id}/statements/export")
def export_statements(engagement_id: int, upload_id: int | None = None,
                      prior_upload_id: int | None = None, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    data, tb, prior = compute(session, e, upload_id, prior_upload_id)
    if tb is None:
        raise HTTPException(400, "Upload a trial balance first.")
    entries = [entry_out(x) for x in e.adjusting_entries]
    log(session, user, "statements.download", "Downloaded lead sheets and draft statements", e,
        details={"upload_id": tb.id, "sha256": tb.sha256,
                 "prior_sha256": prior.sha256 if prior else None,
                 "entries": [x["ref"] for x in entries]})
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(), "Framework": e.framework,
               "Trial balance": f"{tb.filename} (SHA-256 {tb.sha256})",
               "Prior-year trial balance":
                   f"{prior.filename} (SHA-256 {prior.sha256})" if prior else "None",
               "Status": "Draft, not audited"}
    stem = safe_stem(f"{e.client.name}-{e.name}-lead-sheets")
    return Response(statements.to_excel(data, entries, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))
