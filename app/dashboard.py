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
# Report draft warnings the completion steps already cover.
COMPLETION_WARNINGS = {"Some subsequent events still need a decision.",
                       "Conclude on going concern in the completion checklist.",
                       "The completion procedures aren't signed off yet."}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def statements_state(session: Session, e: Engagement, upload_id: int | None = None,
                     prior_upload_id: int | None = None) -> dict | None:
    """The parts of the draft statements that need attention, or None without a trial balance.
    Uses the same files as the statements panel when given their ids, else the latest."""
    try:
        data, tb, _ = compute(session, e, upload_id, prior_upload_id)
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


def bank_state(e: Engagement) -> dict | None:
    """Where the bank reconciliation stands, or None without a bank statement. It counts as
    done once the latest bank statement has been reconciled; the latest such run speaks."""
    banks = [u for u in e.uploads if u.kind == "bank"]
    if not banks:
        return None
    latest_bank = max(banks, key=lambda u: (u.uploaded_at, u.id))
    recs = [r for r in e.bank_reconciliations if r.bank_upload_id == latest_bank.id]
    if not recs:
        return {"reconciled": False, "difference": None, "stale": 0, "outstanding": 0}
    r = recs[-1]
    return {"reconciled": True, "id": r.id, "difference": r.result["statement"]["difference"],
            "stale": r.result["summary"]["stale"],
            "outstanding": r.result["summary"]["outstanding"]}


def next_steps(e: Engagement, mat: dict | None, kinds: set[str], gl_runs: int, findings: dict,
               open_high: int, rv: dict, rq: dict, fs: dict | None,
               bank: dict | None = None, risks: dict | None = None,
               acceptance: dict | None = None,
               disclosures: dict | None = None, controls: dict | None = None,
               confirmations: dict | None = None, workplan: dict | None = None,
               finishing: dict | None = None, related: dict | None = None,
               documents: dict | None = None, analytics: dict | None = None,
               stock: dict | None = None) -> list[dict]:
    """The to-do list, most pressing first. Each step names the panel (and tab) that deals
    with it."""
    steps: list[dict] = []

    def add(priority: str, text: str, panel: str, tab: str | None = None) -> None:
        steps.append({"priority": priority, "text": text, "panel": panel, "tab": tab})

    if mat is None:
        add("high", "Set materiality.", "mat-panel")
    if "tb" not in kinds:
        add("high", "Upload this year's trial balance.", "files-panel")
    if "gl" not in kinds:
        add("medium", "Upload the general ledger to run the journal entry checks.", "files-panel")
    elif not gl_runs:
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
                    "fs-panel", "mapping")
            if abs(fs["difference"]) >= 0.01:
                add("high", "The balance sheet doesn't balance; check the trial balance.",
                    "fs-panel")
            for w in fs["warnings"]:
                if w["severity"] == "high":
                    add("high", w["text"], "fs-panel", "ratios")
            if fs["ratio_flags"]:
                add("medium", f"Explain {_plural(fs['ratio_flags'], 'ratio')} that moved a lot "
                              "on last year.", "fs-panel", "ratios")
            if (fs["uncorrected"] or "").startswith(MATERIAL_UNCORRECTED):
                add("high", fs["uncorrected"], "fs-panel", "uncorrected")
    if bank:
        if not bank["reconciled"]:
            add("medium", "Reconcile the bank statement with the general ledger.", "bank-panel")
        elif bank["difference"] is not None and abs(bank["difference"]) >= 0.01:
            add("high", "The bank reconciliation has an unexplained difference; look into it.",
                "bank-panel")
        elif bank["stale"]:
            add("medium", f"Follow up {_plural(bank['stale'], 'old reconciling item')} on the "
                          "bank reconciliation.", "bank-panel")
    if acceptance is not None:
        if acceptance["started"] and not acceptance["decision"]:
            add("medium", "Record the acceptance decision.", "ac-panel")
        if acceptance["decision"] == "decline":
            add("high", "The engagement was declined at acceptance.", "ac-panel")
        if acceptance["threats"]:
            add("high", f"Consider the threats to independence declared by "
                        f"{_plural(acceptance['threats'], 'team member')}.", "ac-panel")
        missing = acceptance["team"] - acceptance["declared"]
        if acceptance["any_declared"] and missing > 0:
            add("medium", f"{_plural(missing, 'team member')} still to confirm independence.",
                "ac-panel")
    if disclosures is not None and disclosures["started"]:
        if disclosures["missing"]:
            add("high", f"Have the client add {_plural(disclosures['missing'], 'missing disclosure')}"
                        " to the statements.", "dc-panel")
        left = disclosures["items"] - disclosures["answered"]
        if left:
            add("medium", f"Finish the disclosure checklist ({left} left).", "dc-panel")
    if risks is not None:
        if not risks["risks"] and risks.get("ai_draft"):
            add("low", "Add the risks from the AI risk assessment to the risk register.",
                "rr-panel")
        if risks["without_procedures"]:
            add("medium", f"Plan procedures for {_plural(risks['without_procedures'], 'risk')} "
                          "with none yet.", "rr-panel")
        if risks["planned"]:
            add("medium", f"Finish {_plural(risks['planned'], 'planned audit procedure')}.",
                "rr-panel")
    if controls and controls["controls"]:
        if controls["deficient"]:
            found = _plural(controls["deficient"], "control deficiency", "control deficiencies")
            add("high", f"Consider {found}: report significant ones to those charged with "
                        "governance.", "ct-panel")
        if controls["key_to_test"]:
            add("medium", f"Test {_plural(controls['key_to_test'], 'key control')} you plan "
                          "to rely on.", "ct-panel")
        if controls["design"]["not_assessed"]:
            add("low", f"Assess the design of "
                       f"{_plural(controls['design']['not_assessed'], 'control')}.", "ct-panel")
    if confirmations and confirmations["requests"]:
        out = confirmations["outcomes"]
        if out["overdue"]:
            add("high", f"Send a reminder for {_plural(out['overdue'], 'confirmation request')}"
                        " with no reply.", "cf-panel")
        if out["difference"]:
            add("high", f"Explain {_plural(out['difference'], 'difference')} in confirmation "
                        "replies.", "cf-panel")
        if out["to_send"]:
            add("medium", f"Send {_plural(out['to_send'], 'confirmation request')}.", "cf-panel")
        if out["alternative_needed"]:
            add("medium", f"Do alternative procedures for "
                          f"{_plural(out['alternative_needed'], 'confirmation')} with no reply.",
                "cf-panel")
    if analytics and analytics["expectations"]:
        out = analytics["outcomes"]
        if out["to_explain"]:
            add("high", f"Explain {_plural(out['to_explain'], 'difference')} over the threshold "
                        "in the analytical procedures.", "ap-panel")
        if out["misstatement"]:
            add("medium", f"Post the {_plural(out['misstatement'], 'misstatement')} the "
                          "analytical procedures found as proposed adjustments.", "ap-panel")
        missing = out["no_expectation"] + out["no_threshold"]
        if missing:
            add("low", f"Finish the inputs of {_plural(missing, 'analytical procedure')}.",
                "ap-panel")
    if stock and stock["items"]:
        st = stock["statuses"]
        if st["difference"] + st["not_listed"]:
            add("high", f"Explain {_plural(st['difference'] + st['not_listed'], 'difference')} "
                        "found at the inventory count.", "sc-panel")
        if stock["outcome"] == "over":
            add("high", "The inventory count's projected misstatement is over performance "
                        "materiality: extend the testing or propose an adjustment.", "sc-panel")
        if st["to_count"]:
            add("medium", f"Record the count of {_plural(st['to_count'], 'inventory item')}.",
                "sc-panel")
    if finishing and finishing["started"]:
        if finishing["doubt"] and not finishing["going_concern"]:
            add("high", "Conclude on going concern: the checklist notes doubt about it.",
                "cp-panel")
        if finishing["pending_events"]:
            add("medium", f"Decide how to deal with "
                          f"{_plural(finishing['pending_events'], 'subsequent event')}.",
                "cp-panel")
    if finishing and (not finishing["signed"] if finishing["started"] else
                      "The completion procedures aren't signed off yet."
                      in finishing["report_warnings"]):  # a dated report, nothing done yet
        add("low", "Finish the completion checklist and have a partner sign it off.",
            "cp-panel")
    if finishing:
        for w in finishing["report_warnings"]:
            if w not in COMPLETION_WARNINGS:  # already a step above
                add("medium", w, "rp-panel")
    if workplan:
        if workplan["overdue"]:
            add("medium", f"Catch up on {_plural(workplan['overdue'], 'overdue task')}.",
                "wp-panel")
        if workplan["unassigned"]:
            add("low", f"Give {_plural(workplan['unassigned'], 'task')} an owner.", "wp-panel")
        for area in workplan["over_areas"]:
            add("low", f"Hours on {area} are over budget.", "wp-panel")
    if related:
        parties = {k: _plural(related[k], "related party", "related parties")
                   for k in ("missing_disclosure", "to_review")}
        if related["missing_disclosure"]:
            add("high", f"Disclose {parties['missing_disclosure']} the statements leave out.",
                "rpt-panel")
        if related["to_review"]:
            add("medium", f"Review the transactions with {parties['to_review']}.", "rpt-panel")
        if related["parties"] and related["has_ledger"] and not related["scanned"]:
            add("low", "Scan the general ledger for related party names.", "rpt-panel")
    if documents and documents["to_review"]:
        n = _plural(documents["to_review"], "AI document summary", "AI document summaries")
        add("low", f"Check {n} against the documents.", "ds-panel")
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
def get_dashboard(engagement_id: int, upload_id: int | None = None,
                  prior_upload_id: int | None = None, user: User = Depends(current_user),
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
    fs = statements_state(session, e, upload_id, prior_upload_id)
    # The checks count as run once the latest general ledger has been checked.
    gls = [u for u in e.uploads if u.kind == "gl"]
    latest_gl = max(gls, key=lambda u: (u.uploaded_at, u.id)) if gls else None
    gl_runs = sum(1 for r in e.runs if latest_gl is not None and r.upload_id == latest_gl.id)
    last = e.runs[-1] if e.runs else None
    bank = bank_state(e)
    # Imported here, as both import ai_writer or engagements, which import this module.
    from .acceptance import summary as acceptance_summary
    from .completion import summary as completion_summary
    from .confirmations import summary as confirmation_summary
    from .controls import summary as control_summary
    from .disclosures import summary as disclosure_summary
    from .expectations import summary as analytics_summary
    from .related_parties import summary as related_summary
    from .report_draft import state as report_state
    from .risks import summary
    from .stock_count import summary as stock_summary
    from .summaries import summary as documents_summary
    from .workplan import summary as workplan_summary
    # Teams may keep their risk assessment elsewhere: only suggest the register to those who
    # started one here, or asked the AI for a risk assessment.
    risk_summary = {**summary(list(e.risks)),
                    "ai_draft": any(d.kind == "risk" for d in e.ai_drafts)}
    controls = control_summary(list(e.controls))
    confirmations = confirmation_summary(e)
    workplan = workplan_summary(e)
    related, documents = related_summary(e), documents_summary(e)
    finishing = completion_summary(e)
    analytics = analytics_summary(session, e)
    stock = stock_summary(e)
    finishing = {**finishing, "reps_dated": None, "signed_at": None,
                 "report_warnings": report_state(session, e)["warnings"]
                 if e.audit_report else []}
    days_left = (e.period_end - date.today()).days
    return {
        "period_end": e.period_end.isoformat(),
        "days_since_period_end": -days_left if days_left < 0 else 0,
        "materiality": mat,
        "uploads": len(e.uploads),
        "upload_kinds": sorted(kinds),
        "last_run": {"id": last.id, "created_at": last.created_at.isoformat(),
                     "filename": last.upload.filename,
                     "findings_by_severity": (last.summary.findings_by_severity if last.summary
                                              else last.result["summary"]["findings_by_severity"])}
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
        "bank": bank,
        "risks": risk_summary,
        "controls": controls,
        "confirmations": confirmations,
        "workplan": workplan,
        "related": related,
        "documents": documents,
        "analytics": analytics,
        "stock": stock,
        "completion": {k: finishing[k] for k in ("started", "signed", "going_concern",
                                                 "pending_events")},
        "next_steps": next_steps(e, mat, kinds, gl_runs, findings,
                                 open_by_severity.get("high", 0), rv, rq, fs, bank,
                                 risk_summary, acceptance_summary(e), disclosure_summary(e),
                                 controls, confirmations, workplan, finishing, related,
                                 documents, analytics, stock)
        if e.lock is None else [],
    }
