"""The partner overview: every engagement the user is on (admins: every one), one row each,
with where it stands and what is holding it up, so a partner sees the whole portfolio at a
glance and opens the file that needs them. Nothing is stored; it is worked out on each look
and downloads to Excel.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, portal, review
from .auth import log, require
from .dashboard import _plural
from .db import Client, Engagement, Finding, User, get_session
from .engagements import assigned_ids, visible_engagements
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STAGES = {"planning": "Planning", "fieldwork": "Fieldwork", "completion": "Completion",
          "locked": "Locked"}


def stage(e: Engagement, findings: int, finishing: dict) -> str:
    """Planning until a ledger is checked or a finding raised; completion once the checklist
    is started; locked once the file is final."""
    if e.lock is not None:
        return "locked"
    if finishing["started"]:
        return "completion"
    if e.runs or findings:
        return "fieldwork"
    return "planning"


def row(session: Session, e: Engagement, counts: dict, today: date) -> dict:
    # Imported here, as these import engagements, which the app imports before this module.
    from .completion import summary as completion_summary
    from .workplan import summary as workplan_summary
    found = counts.get(e.id, {})
    rv = review.review_out(session, e)
    rq = portal.summary(list(e.document_requests))
    wp = workplan_summary(e)
    finishing = completion_summary(e)
    at = stage(e, sum(found.values()), finishing)
    days = (today - e.period_end).days
    partners = sorted(m.user.name for m in e.team if m.user.role == "partner")
    # What is holding the file up, most pressing first.
    waiting = [f"{_plural(n, what)} {how}" for n, what, how in (
        (found.get("high", 0), "high-severity finding", "open"),
        (rq["overdue"], "client request", "overdue"),
        (wp["overdue"], "task", "overdue"),
        (rv["open_notes"], "review note", "open"),
    ) if n]
    return {
        "id": e.id, "client": e.client.name, "name": e.name,
        "period_end": e.period_end.isoformat(), "days_since_period_end": max(days, 0),
        "stage": at, "stage_label": STAGES[at],
        "archived": bool(e.lock and e.lock.archived),
        "partners": partners, "team": len(e.team),
        "findings_open": sum(found.values()), "findings_high": found.get("high", 0),
        "review_done": rv["reviewed"], "review_total": rv["total"],
        "open_notes": rv["open_notes"],
        "requests_open": rq["open"] + rq["returned"], "requests_overdue": rq["overdue"],
        "tasks_overdue": wp["overdue"], "hours_budget": wp["budget"],
        "hours_actual": wp["actual"],
        "completion_signed": finishing["signed"],
        "waiting": waiting,
        # How much needs attention, to sort the files that need someone first.
        "attention": 0 if at == "locked" else
        3 * found.get("high", 0) + 2 * rq["overdue"] + wp["overdue"] + rv["open_notes"],
    }


def rows_for(session: Session, user: User, archived: bool) -> list[dict]:
    engagements = list(session.scalars(select(Engagement).join(Client)
                                       .order_by(Client.name, Engagement.period_end)))
    seen = [e for e in visible_engagements(session, user, engagements, assigned_ids(session, user))
            if archived or not (e.lock and e.lock.archived)]
    ids = [e.id for e in seen]
    counts: dict[int, dict[str, int]] = {}
    if ids:
        for eid, severity, n in session.execute(
                select(Finding.engagement_id, Finding.severity, func.count(Finding.id))
                .where(Finding.engagement_id.in_(ids), Finding.status == "open")
                .group_by(Finding.engagement_id, Finding.severity)):
            counts.setdefault(eid, {})[severity] = n
    today = date.today()
    out = [row(session, e, counts, today) for e in seen]
    return sorted(out, key=lambda r: (-r["attention"], r["period_end"], r["client"].lower()))


def totals(rows: list[dict]) -> dict:
    live = [r for r in rows if r["stage"] != "locked"]
    return {"engagements": len(rows), "open": len(live),
            "stages": {k: sum(1 for r in rows if r["stage"] == k) for k in STAGES},
            "findings_high": sum(r["findings_high"] for r in live),
            "requests_overdue": sum(r["requests_overdue"] for r in live),
            "tasks_overdue": sum(r["tasks_overdue"] for r in live),
            "open_notes": sum(r["open_notes"] for r in live),
            "needs_attention": sum(1 for r in live if r["attention"])}


@router.get("/overview")
def overview(archived: bool = False, user: User = Depends(require("review")),
             session: Session = Depends(get_session)) -> dict:
    rows = rows_for(session, user, archived)
    return {"rows": rows, "totals": totals(rows), "all": auth.sees_all_engagements(user)}


@router.get("/overview/export")
def export_overview(archived: bool = False, user: User = Depends(require("review")),
                    session: Session = Depends(get_session)) -> Response:
    rows = rows_for(session, user, archived)
    log(session, user, "overview.download", "Downloaded the engagement overview",
        details={"engagements": len(rows)})
    session.commit()
    t = totals(rows)
    about = pd.DataFrame([
        ("Prepared by", user.name), ("Date", date.today().isoformat()),
        ("Engagements", t["engagements"]), ("Not yet locked", t["open"]),
        ("Needing attention", t["needs_attention"]),
        ("Archived engagements", "Included" if archived else "Left out")],
        columns=["Item", "Value"])
    table = pd.DataFrame([{
        "Client": r["client"], "Engagement": r["name"], "Period end": r["period_end"],
        "Days since period end": r["days_since_period_end"], "Stage": r["stage_label"],
        "Partners": ", ".join(r["partners"]), "Open findings": r["findings_open"],
        "High-severity open": r["findings_high"],
        "Sections reviewed": f"{r['review_done']}/{r['review_total']}",
        "Open review notes": r["open_notes"], "Client requests open": r["requests_open"],
        "Client requests overdue": r["requests_overdue"], "Tasks overdue": r["tasks_overdue"],
        "Hours budget": r["hours_budget"], "Hours spent": r["hours_actual"],
        "Completion signed off": "Yes" if r["completion_signed"] else "No",
        "Holding it up": "; ".join(r["waiting"])} for r in rows],
        columns=["Client", "Engagement", "Period end", "Days since period end", "Stage",
                 "Partners", "Open findings", "High-severity open", "Sections reviewed",
                 "Open review notes", "Client requests open", "Client requests overdue",
                 "Tasks overdue", "Hours budget", "Hours spent", "Completion signed off",
                 "Holding it up"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        about.to_excel(xl, sheet_name="About", index=False)
        table.to_excel(xl, sheet_name="Engagements", index=False)
        finish_workbook(xl.book)
    return Response(buf.getvalue(), media_type=XLSX,
                    headers=attachment(f"Engagement overview {date.today().isoformat()}.xlsx"))
