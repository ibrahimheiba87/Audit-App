"""One engagement at a glance: where the file stands, and what to do next."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import portal, register, review
from .auth import current_user
from .db import Engagement, Finding, User, get_session
from .engagements import get_or_404, materiality_out
from .leadsheets import compute

router = APIRouter(prefix="/api")

# How statements.uncorrected words its conclusion when the misstatements are material.
MATERIAL_UNCORRECTED = "Uncorrected misstatements are material"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def statements_state(session: Session, e: Engagement) -> dict | None:
    """The parts of the draft statements that need attention, or None without a trial balance."""
    try:
        data, tb, _ = compute(session, e, None, None)
    except HTTPException as exc:  # a trial balance the statements can't be built from
        return {"error": str(exc.detail)}
    if tb is None:
        return None
    return {
        "unmapped": len(data["unmapped"]),
        "difference": data["statements"]["difference"]["current"],
        "profit": data["statements"]["profit"]["current"],
        "uncorrected": data["uncorrected"]["conclusion"],
        "uncorrected_entries": len(data["uncorrected"]["entries"]),
        "ratio_flags": data["ratios"]["flagged"],
        "warnings": data["ratios"]["warnings"],
    }


def next_steps(e: Engagement, mat: dict | None, kinds: set[str], runs: int, findings: dict,
               open_high: int, rv: dict, rq: dict, fs: dict | None) -> list[dict]:
    """The to-do list, most pressing first. Each step names the panel that deals with it."""
    steps: list[dict] = []

    def add(priority: str, text: str, panel: str) -> None:
        steps.append({"priority": priority, "text": text, "panel": panel})

    if mat is None:
        add("high", "Set materiality.", "mat-panel")
    if "tb" not in kinds:
        add("high", "Upload this year's trial balance.", "files-panel")
    if "gl" not in kinds:
        add("medium", "Upload the general ledger to run the journal entry checks.", "files-panel")
    elif not runs:
        add("medium", "Run the audit checks on the general ledger.", "files-panel")
    if "prior_tb" not in kinds:
        add("low", "Upload last year's trial balance for comparatives and ratio trends.",
            "files-panel")
    if fs:
        if fs.get("error"):
            add("high", "Fix the trial balance: the statements can't be built from it.",
                "fs-panel")
        else:
            if fs["unmapped"]:
                add("high", f"Map {_plural(fs['unmapped'], 'account')} to a statement line.",
                    "fs-panel")
            if abs(fs["difference"]) >= 0.01:
                add("high", "The balance sheet doesn't balance; check the trial balance.",
                    "fs-panel")
            for w in fs["warnings"]:
                if w["severity"] == "high":
                    add("high", w["text"], "fs-panel")
            if fs["ratio_flags"]:
                add("medium", f"Explain {_plural(fs['ratio_flags'], 'ratio')} that moved a lot "
                              "on last year.", "fs-panel")
            if (fs["uncorrected"] or "").startswith(MATERIAL_UNCORRECTED):
                add("high", fs["uncorrected"], "fs-panel")
    if open_high:
        add("high", f"Work through {_plural(open_high, 'open high-severity finding')}.", "reg-panel")
    elif findings["open"]:
        add("medium", f"Work through {_plural(findings['open'], 'open finding')}.", "reg-panel")
    if rq["overdue"]:
        add("high", f"Chase {_plural(rq['overdue'], 'overdue client request')}.", "rq-panel")
    if rv["open_notes"]:
        add("medium", f"Answer or clear {_plural(rv['open_notes'], 'open review note')}.",
            "rv-panel")
    if rv["reviewed"] < rv["total"]:
        add("low", f"Sign off the remaining {_plural(rv['total'] - rv['reviewed'], 'section')}.",
            "rv-panel")
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(steps, key=lambda s: order[s["priority"]])


@router.get("/engagements/{engagement_id}/dashboard")
def get_dashboard(engagement_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    mat = materiality_out(e)
    kinds = {u.kind for u in e.uploads}
    register.sync_engagement(session, e)
    findings = register.summary_counts(session, e.id)
    open_by_severity = dict(session.execute(
        select(Finding.severity, func.count(Finding.id))
        .where(Finding.engagement_id == e.id, Finding.status == "open")
        .group_by(Finding.severity)).all())
    rv = review.review_out(session, e)
    rq = portal.summary(list(e.document_requests))
    fs = statements_state(session, e)
    last = e.runs[-1] if e.runs else None
    days_left = (e.period_end - date.today()).days
    return {
        "period_end": e.period_end.isoformat(),
        "days_since_period_end": -days_left if days_left < 0 else 0,
        "materiality": mat,
        "uploads": len(e.uploads),
        "upload_kinds": sorted(kinds),
        "last_run": {"id": last.id, "created_at": last.created_at.isoformat(),
                     "filename": last.upload.filename,
                     "findings_by_severity": last.result["summary"]["findings_by_severity"]}
                    if last else None,
        "findings": {**findings,
                     "open_by_severity": {s: open_by_severity.get(s, 0)
                                          for s in register.SEVERITIES}},
        "review": {"reviewed": rv["reviewed"], "total": rv["total"],
                   "open_notes": rv["open_notes"],
                   "sections": [{"id": s["id"], "label": s["label"], "status": s["status"],
                                 "done": s["done"]} for s in rv["sections"]]},
        "requests": {"total": rq["total"], "overdue": rq["overdue"],
                     "outstanding": sum(rq[s] for s in portal.OUTSTANDING),
                     "to_check": rq.get("submitted", 0)},
        "statements": fs,
        "next_steps": next_steps(e, mat, kinds, len(e.runs), findings,
                                 open_by_severity.get("high", 0), rv, rq, fs),
    }
