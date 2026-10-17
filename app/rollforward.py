"""Roll an engagement forward: start next year's file from this year's.

The new engagement gets the next period, and (each optional) the team, the account mapping,
the materiality method without its amounts, the client request list (open again, without due
dates) and this year's adjusted trial balance as its prior-year trial balance.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, loader, prior_points
from .auth import log, require
from .db import (
    AccountMapping,
    Control,
    DocumentRequest,
    Engagement,
    EngagementMember,
    Expectation,
    RelatedParty,
    Task,
    TimeBudget,
    Upload,
    User,
    get_session,
)
from .engagements import engagement_out, get_or_404
from .leadsheets import compute

router = APIRouter(prefix="/api")


class RollForwardIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    team: bool = True
    mapping: bool = True
    materiality: bool = True
    requests: bool = True
    trial_balance: bool = True
    controls: bool = True
    workplan: bool = True
    related_parties: bool = True
    expectations: bool = True
    prior_points: bool = True


# The names and periods of an expectation's inputs; the amounts and rates are this year's.
CARRIED_INPUTS = ("base_label", "units_label", "price_label", "months", "periods")


def next_year(d: date) -> date:
    """The same date a year on; a month end stays a month end (28 Feb 2027 -> 29 Feb 2028)."""
    last = calendar.monthrange(d.year, d.month)[1]
    new_last = calendar.monthrange(d.year + 1, d.month)[1]
    return date(d.year + 1, d.month, new_last if d.day == last else min(d.day, new_last))


def next_name(name: str) -> str:
    """"FY2026" -> "FY2027"; a name without a year gets "(rolled forward)"."""
    bumped, n = re.subn(r"(?<!\d)(19|20)(\d\d)(?!\d)", lambda m: str(int(m[0]) + 1), name)
    return bumped if n else f"{name[:183]} (rolled forward)"


def adjusted_tb_csv(data: dict) -> tuple[bytes, int]:
    """This year's adjusted trial balance as a CSV the loader reads back (debit positive)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Account", "Account Name", "Amount"])
    rows = 0
    for r in data["accounts"]:
        if r["in_tb"] or abs(r["adjusting"]) >= 0.005 or abs(r["reclassification"]) >= 0.005:
            w.writerow([r["account"], r["name"] or "", f"{r['adjusted']:.2f}"])
            rows += 1
    return buf.getvalue().encode("utf-8"), rows


@router.post("/engagements/{engagement_id}/roll-forward", status_code=201)
def roll_forward(engagement_id: int, body: RollForwardIn, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> dict:
    old = get_or_404(session, Engagement, engagement_id, user)
    name = (body.name or "").strip() or next_name(old.name)
    period_end = next_year(old.period_end)
    if any(x.name == name and x.period_end == period_end for x in old.client.engagements):
        raise HTTPException(409, f"This client already has an engagement called {name} "
                                 f"for the period ending {period_end}.")
    new = Engagement(client=old.client, name=name, period_end=period_end,
                     # The day after this period (which may have been a short first one).
                     period_start=old.period_end + timedelta(days=1) if old.period_start else None,
                     framework=old.framework)
    session.add(new)
    carried: dict[str, int | bool | str] = {}
    if body.team:
        # Admins see every engagement, so they aren't put on teams (see set_team).
        members = [m.user for m in old.team
                   if m.user.active and m.user.role not in ("client", "admin")]
        for u in members:
            new.team.append(EngagementMember(user_id=u.id))
        carried["team"] = len(members)
    if not auth.sees_all_engagements(user) and all(m.user_id != user.id for m in new.team):
        new.team.append(EngagementMember(user_id=user.id))  # or they'd lose it at once
    if body.mapping:
        for m in old.account_mappings:
            new.account_mappings.append(AccountMapping(account=m.account, line=m.line,
                                                       updated_by=user.name))
        carried["mapping"] = len(old.account_mappings)
    if body.materiality and old.benchmark:
        # The method, not the amounts: this year's benchmark amount is entered afresh.
        new.benchmark, new.benchmark_pct = old.benchmark, old.benchmark_pct
        new.performance_pct, new.trivial_pct = old.performance_pct, old.trivial_pct
        new.materiality_rationale = old.materiality_rationale
        carried["materiality"] = True
    if body.requests:
        for number, r in enumerate(old.document_requests, start=1):
            new.document_requests.append(DocumentRequest(
                number=number, title=r.title, details=r.details, status="open",
                created_by=user.name))
        carried["requests"] = len(old.document_requests)
    if body.controls and old.controls:
        # What each control is, not this year's assessment: design and tests are done afresh,
        # and the risks they addressed are last year's.
        for c in old.controls:
            new.controls.append(Control(
                area=c.area, process=c.process, title=c.title, description=c.description,
                kind=c.kind, nature=c.nature, frequency=c.frequency, key=c.key, owner=c.owner,
                source=f"rolled:{c.id}", created_by=user.name))
        carried["controls"] = len(old.controls)
    if body.workplan and (old.tasks or old.time_budgets):
        # The list of work and the hours planned for it; owners and dates are set afresh.
        for t in old.tasks:
            new.tasks.append(Task(title=t.title, area=t.area, note=t.note, created_by=user.name,
                                  created_by_id=user.id, updated_by=user.name))
        for b in old.time_budgets:
            new.time_budgets.append(TimeBudget(area=b.area, hours=b.hours))
        carried["tasks"] = len(old.tasks)
        carried["budget"] = round(sum(b.hours for b in old.time_budgets), 2)
    if body.related_parties and old.related_parties:
        # Who the related parties are, not this year's scan or conclusions.
        for p in old.related_parties:
            new.related_parties.append(RelatedParty(
                name=p.name, relationship_kind=p.relationship_kind, aliases=list(p.aliases or []),
                note=p.note, source=f"rolled:{p.id}", created_by=user.name,
                updated_by=user.name))
        carried["related_parties"] = len(old.related_parties)
    if body.expectations and old.expectations:
        # How each expectation is built and where its inputs come from; the figures, the
        # threshold and the conclusions are this year's to work out.
        for x in old.expectations:
            new.expectations.append(Expectation(
                kind=x.kind, title=x.title, line=x.line, method=x.method,
                inputs={k: v for k, v in (x.inputs or {}).items() if k in CARRIED_INPUTS},
                basis=x.basis, source=f"rolled:{x.id}", created_by=user.name,
                updated_by=user.name))
        carried["expectations"] = len(old.expectations)
    if body.prior_points:
        # Last year's management letter points and open findings, to follow up this year.
        added = prior_points.carry(new, old, user)
        if added:
            carried["prior_points"] = added
    session.flush()

    upload = None
    if body.trial_balance:
        try:
            data, tb, _ = compute(session, old, None, None)
        except HTTPException as exc:  # a trial balance the statements can't be built from
            data, tb = None, None
            carried["trial_balance_error"] = str(exc.detail)
        if tb is not None:
            content, rows = adjusted_tb_csv(data)
            filename = f"{old.name} adjusted trial balance.csv"
            loaded = loader.load(filename, content)
            upload = Upload(engagement=new, kind="prior_tb", filename=filename, content=content,
                            size=len(content), sha256=hashlib.sha256(content).hexdigest(),
                            mapping=loaded.report.mapping, import_report=loaded.report.to_dict())
            session.add(upload)
            session.flush()
            carried["trial_balance"] = rows
    log(session, user, "engagement.roll_forward",
        f"Rolled forward to {new.name} (period end {period_end})", old,
        details={"to_engagement_id": new.id, "carried": carried})
    log(session, user, "engagement.create",
        f"Created engagement {new.name} (period end {period_end}) by rolling forward "
        f"{old.name}", new, details={"from_engagement_id": old.id, "carried": carried})
    if carried.get("controls"):  # so the controls section needs a review this year too
        log(session, user, "control.carry",
            f"Carried {carried['controls']} controls from {old.name}", new,
            details={"from_engagement_id": old.id, "controls": carried['controls']})
    if carried.get("expectations"):  # and the analytics section
        log(session, user, "expectation.carry",
            f"Carried {carried['expectations']} expectations from {old.name}", new,
            details={"from_engagement_id": old.id, "expectations": carried["expectations"]})
    if carried.get("related_parties"):  # and the related parties section
        log(session, user, "related.carry",
            f"Carried {carried['related_parties']} related parties from {old.name}", new,
            details={"from_engagement_id": old.id, "parties": carried["related_parties"]})
    if carried.get("prior_points"):  # and the last year's points section
        log(session, user, "prior.carry",
            f"Carried {carried['prior_points']} points from {old.name} to follow up", new,
            details={"from_engagement_id": old.id, "points": carried["prior_points"]})
    if upload is not None:
        log(session, user, "upload.create",
            f"Uploaded {upload.filename} (Prior-year trial balance) from {old.name}", new,
            details={"upload_id": upload.id, "filename": upload.filename, "kind": "prior_tb",
                     "sha256": upload.sha256, "size": upload.size, "sheet": None,
                     "mapping": upload.mapping, "rows_used": carried["trial_balance"],
                     "from_engagement_id": old.id})
    session.commit()
    return {**engagement_out(new), "carried": carried}
