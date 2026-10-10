"""The findings register: findings raised by check runs or by hand, with their review trail."""

from __future__ import annotations

import io

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Engagement, Finding, Run, utcnow

STATUSES = {"open": "Open", "reviewed": "Reviewed", "cleared": "Cleared"}
SEVERITIES = ["high", "medium", "low"]


def finding_key(upload_id, check, row, entry_id) -> tuple:
    return (upload_id, check, row, entry_id)


def sync_run_findings(session: Session, run: Run) -> int:
    """Add a run's findings to the register, skipping ones already there. Returns new count."""
    names = {c["id"]: c["name"] for c in run.result.get("checks", [])}
    existing = {
        finding_key(f.upload_id, f.check, f.row, f.entry_id)
        for f in session.scalars(select(Finding).where(Finding.upload_id == run.upload_id))
    }
    added = 0
    for f in run.result.get("findings", []):
        key = finding_key(run.upload_id, f["check"], f.get("row"), f.get("entry_id"))
        if key in existing:
            continue
        existing.add(key)
        session.add(Finding(
            engagement_id=run.engagement_id, upload_id=run.upload_id, run_id=run.id,
            source="run", check=f["check"], check_name=names.get(f["check"], f["check"]),
            severity=f["severity"], row=f.get("row"), entry_id=f.get("entry_id"),
            date=f.get("date"), account=f.get("account"), description=f.get("description"),
            amount=f.get("amount"), detail=f.get("detail") or "",
        ))
        added += 1
    return added


def sync_engagement(session: Session, e: Engagement) -> None:
    """Bring runs saved before the register existed onto it (idempotent)."""
    synced = set(session.scalars(
        select(Finding.run_id).where(Finding.engagement_id == e.id, Finding.run_id.is_not(None))
    ))
    if any(sync_run_findings(session, r) for r in e.runs if r.id not in synced):
        session.commit()


def summary(findings: list[Finding]) -> dict:
    out = {s: 0 for s in STATUSES}
    for f in findings:
        out[f.status] += 1
    out["total"] = len(findings)
    return out


def is_worked(f: Finding) -> bool:
    """True once someone has reviewed, commented on or attached evidence to a finding."""
    return f.status != "open" or bool(f.notes) or bool(f.evidence)


def touch(f: Finding) -> None:
    f.updated_at = utcnow()


def to_excel(findings: list[Finding], context: dict) -> bytes:
    register = pd.DataFrame([{
        "ID": f.id, "Status": STATUSES[f.status], "Severity": f.severity, "Check": f.check_name,
        "Row": f.row, "Entry": f.entry_id, "Date": f.date, "Account": f.account,
        "Description": f.description, "Amount": f.amount, "Detail": f.detail,
        "File": f.upload.filename if f.upload else "",
        "Notes": len([n for n in f.notes if n.text]),
        "Evidence": ", ".join(ev.filename for ev in f.evidence),
        "Last updated (UTC)": f"{f.updated_at:%Y-%m-%d %H:%M}",
    } for f in findings], columns=[
        "ID", "Status", "Severity", "Check", "Row", "Entry", "Date", "Account", "Description",
        "Amount", "Detail", "File", "Notes", "Evidence", "Last updated (UTC)",
    ])
    trail = pd.DataFrame([{
        "Finding": f.id, "When (UTC)": f"{n.created_at:%Y-%m-%d %H:%M}", "Who": n.author,
        "Status change": (f"{STATUSES[n.status_from]} to {STATUSES[n.status_to]}"
                          if n.status_to else ""),
        "Note": n.text,
    } for f in findings for n in f.notes], columns=[
        "Finding", "When (UTC)", "Who", "Status change", "Note",
    ])
    evidence = pd.DataFrame([{
        "Finding": f.id, "File": ev.filename, "Size (bytes)": ev.size, "SHA-256": ev.sha256,
        "Added by": ev.author, "Added (UTC)": f"{ev.uploaded_at:%Y-%m-%d %H:%M}",
    } for f in findings for ev in f.evidence], columns=[
        "Finding", "File", "Size (bytes)", "SHA-256", "Added by", "Added (UTC)",
    ])
    counts = summary(findings)
    rows = list(context.items()) + [(STATUSES[s], counts[s]) for s in STATUSES] + [
        ("Total", counts["total"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(rows, columns=["Item", "Value"]).to_excel(xl, sheet_name="Summary",
                                                               index=False)
        register.to_excel(xl, sheet_name="Findings", index=False)
        trail.to_excel(xl, sheet_name="Review trail", index=False)
        evidence.to_excel(xl, sheet_name="Evidence", index=False)
        for ws in xl.book.worksheets:
            for column in ws.columns:
                width = max(len(str(c.value or "")) for c in column)
                ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)
    return buf.getvalue()
