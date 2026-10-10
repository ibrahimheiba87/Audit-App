"""Findings register endpoints: review status, notes and evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, register
from .auth import current_user, log, require
from .db import Engagement, Evidence, Finding, FindingNote, User, get_session
from .engagements import get_or_404, safe_stem
from .exports import attachment
from .register import STATUSES

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024

Status = Literal["open", "reviewed", "cleared"]
Severity = Literal["high", "medium", "low"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
MAX_PAGE = 1000


class FindingIn(BaseModel):
    severity: Severity = "medium"
    detail: Text
    account: str | None = None
    description: str | None = None
    entry_id: str | None = None
    date: dt.date | None = None
    amount: float | None = None


class FindingPatch(BaseModel):
    status: Status | None = None
    severity: Severity | None = None
    note: str = Field(default="", max_length=5000)


class NoteIn(BaseModel):
    text: Text


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    status: Status
    note: str = Field(default="", max_length=5000)


# --- Serialization ----------------------------------------------------------


def finding_brief(f: Finding) -> dict:
    return {
        "id": f.id, "source": f.source, "check": f.check, "check_name": f.check_name,
        "severity": f.severity, "status": f.status, "status_label": STATUSES[f.status],
        "row": f.row, "entry_id": f.entry_id, "date": f.date, "account": f.account,
        "description": f.description, "amount": f.amount, "detail": f.detail,
        "upload_id": f.upload_id, "filename": f.upload.filename if f.upload else None,
        "run_id": f.run_id, "notes": len([n for n in f.notes if n.text]),
        "evidence": len(f.evidence), "updated_at": f.updated_at.isoformat(),
    }


def note_out(n: FindingNote) -> dict:
    return {"id": n.id, "text": n.text, "author": n.author, "status_from": n.status_from,
            "status_to": n.status_to, "created_at": n.created_at.isoformat()}


def evidence_out(ev: Evidence) -> dict:
    return {"id": ev.id, "filename": ev.filename, "content_type": ev.content_type,
            "size": ev.size, "sha256": ev.sha256, "author": ev.author,
            "uploaded_at": ev.uploaded_at.isoformat()}


def finding_out(f: Finding) -> dict:
    return {**finding_brief(f), "engagement_id": f.engagement_id,
            "created_at": f.created_at.isoformat(),
            "trail": [note_out(n) for n in f.notes],
            "attachments": [evidence_out(ev) for ev in f.evidence]}


# --- Helpers ----------------------------------------------------------------


def set_status(f: Finding, status: str, note: str, user: User) -> bool:
    """Move a finding to a new status, recording who and why. False if nothing changed."""
    if status == f.status:
        return False
    if "cleared" in (status, f.status):
        auth.ensure(user, "review")
    if status == "cleared" and not note.strip():
        raise HTTPException(400, "Add a note explaining why the finding is cleared.")
    f.notes.append(FindingNote(text=note.strip(), author=user.name,
                               status_from=f.status, status_to=status))
    f.status = status
    register.touch(f)
    return True


def engagement_findings(session: Session, e: Engagement) -> list[Finding]:
    register.sync_engagement(session, e)
    q = select(Finding).where(Finding.engagement_id == e.id).order_by(Finding.id)
    return list(session.scalars(register.with_details(q)))


# --- Endpoints --------------------------------------------------------------


@router.get("/findings/options")
def options() -> dict:
    return {"statuses": [{"id": k, "label": v} for k, v in STATUSES.items()],
            "severities": register.SEVERITIES}


@router.get("/engagements/{engagement_id}/findings")
def list_findings(engagement_id: int, status: str = "", severity: str = "", check: str = "",
                  offset: int = 0, limit: int = 200,
                  session: Session = Depends(get_session)) -> dict:
    """One page of the register (filtered), plus totals for the whole engagement."""
    e = get_or_404(session, Engagement, engagement_id)
    register.sync_engagement(session, e)
    offset, limit = max(offset, 0), min(max(limit, 1), MAX_PAGE)
    where = [Finding.engagement_id == e.id]
    if status:
        where.append(Finding.status == status)
    if severity:
        where.append(Finding.severity == severity)
    if check:
        where.append(Finding.check == check)
    matching = session.scalar(select(func.count(Finding.id)).where(*where))
    page = session.scalars(register.with_details(
        select(Finding).where(*where).order_by(Finding.id).offset(offset).limit(limit)))
    checks = session.execute(select(Finding.check, Finding.check_name)
                             .where(Finding.engagement_id == e.id).distinct())
    return {"summary": register.summary_counts(session, e.id),
            "checks": [{"id": k, "name": v} for k, v in checks],
            "matching": matching, "offset": offset, "limit": limit,
            "findings": [finding_brief(f) for f in page]}


@router.post("/engagements/{engagement_id}/findings", status_code=201)
def create_finding(engagement_id: int, body: FindingIn, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    f = Finding(engagement=e, source="manual", severity=body.severity, detail=body.detail,
                account=body.account or None, description=body.description or None,
                entry_id=body.entry_id or None, amount=body.amount,
                date=body.date.isoformat() if body.date else None)
    f.notes.append(FindingNote(text="Added by hand", author=user.name))
    session.add(f)
    session.flush()
    log(session, user, "finding.create", f"Added finding #{f.id}: {f.detail}", e,
        details={"finding_id": f.id, "severity": f.severity})
    session.commit()
    return finding_out(f)


@router.post("/engagements/{engagement_id}/findings/bulk")
def bulk_update(engagement_id: int, body: BulkIn, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    found = list(session.scalars(select(Finding).where(Finding.id.in_(body.ids))))
    if len(found) != len(set(body.ids)) or any(f.engagement_id != e.id for f in found):
        raise HTTPException(400, "Some findings were not found in this engagement.")
    changed = [f.id for f in found if set_status(f, body.status, body.note, user)]
    if changed:
        log(session, user, "finding.status", f"Marked {len(changed)} findings "
            f"{STATUSES[body.status].lower()}" + (f": {body.note.strip()}" if body.note.strip() else ""),
            e, details={"finding_ids": changed, "status": body.status})
    session.commit()
    return {"changed": len(changed), "summary": register.summary_counts(session, e.id)}


@router.get("/engagements/{engagement_id}/findings/export")
def export_findings(engagement_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id)
    findings = engagement_findings(session, e)
    log(session, user, "finding.download", "Downloaded the findings register", e)
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(), "Framework": e.framework}
    stem = safe_stem(f"{e.client.name}-{e.name}-findings")
    return Response(register.to_excel(findings, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))


@router.get("/findings/{finding_id}")
def get_finding(finding_id: int, session: Session = Depends(get_session)) -> dict:
    return finding_out(get_or_404(session, Finding, finding_id))


@router.patch("/findings/{finding_id}")
def update_finding(finding_id: int, body: FindingPatch, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    f = get_or_404(session, Finding, finding_id)
    note = body.note.strip()
    if body.severity and body.severity != f.severity:
        auth.ensure(user, "review")
        text = f"Severity changed from {f.severity} to {body.severity}"
        f.notes.append(FindingNote(text=text + (f": {note}" if note else ""), author=user.name))
        log(session, user, "finding.severity", f"Finding #{f.id}: {text}", f.engagement,
            details={"finding_id": f.id})
        f.severity = body.severity
        register.touch(f)
    if body.status:
        old = f.status
        if set_status(f, body.status, body.note, user):
            log(session, user, "finding.status", f"Finding #{f.id}: {STATUSES[old]} to "
                f"{STATUSES[f.status]}" + (f": {note}" if note else ""), f.engagement,
                details={"finding_id": f.id, "status": f.status})
    session.commit()
    return finding_out(f)


@router.delete("/findings/{finding_id}", status_code=204)
def delete_finding(finding_id: int, user: User = Depends(require("review")),
                   session: Session = Depends(get_session)) -> Response:
    f = get_or_404(session, Finding, finding_id)
    if f.source != "manual":
        raise HTTPException(400, "Findings from a check run can't be deleted; clear them instead.")
    log(session, user, "finding.delete", f"Deleted finding #{f.id}: {f.detail}", f.engagement,
        details={"finding_id": f.id})
    session.delete(f)
    session.commit()
    return Response(status_code=204)


@router.post("/findings/{finding_id}/notes", status_code=201)
def add_note(finding_id: int, body: NoteIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    f = get_or_404(session, Finding, finding_id)
    f.notes.append(FindingNote(text=body.text, author=user.name))
    log(session, user, "finding.note", f"Note on finding #{f.id}: {body.text}",
        f.engagement, details={"finding_id": f.id})
    register.touch(f)
    session.commit()
    return finding_out(f)


@router.post("/findings/{finding_id}/evidence", status_code=201)
async def add_evidence(finding_id: int, file: UploadFile = File(...),
                       user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> dict:
    f = get_or_404(session, Finding, finding_id)
    content = await file.read()
    if not content:
        raise HTTPException(400, "The file is empty.")
    if len(content) > MAX_EVIDENCE_BYTES:
        raise HTTPException(413, "Evidence files can be up to 20 MB.")
    ev = Evidence(
        filename=(file.filename or "evidence")[:500],
        content_type=(file.content_type or "application/octet-stream")[:200],
        content=content, size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        author=user.name,
    )
    f.evidence.append(ev)
    log(session, user, "evidence.add", f"Attached {ev.filename} to finding #{f.id}",
        f.engagement, details={"finding_id": f.id, "sha256": ev.sha256, "size": ev.size})
    register.touch(f)
    session.commit()
    return finding_out(f)


@router.get("/evidence/{evidence_id}")
def download_evidence(evidence_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    ev = get_or_404(session, Evidence, evidence_id)
    log(session, user, "evidence.download", f"Downloaded {ev.filename} from finding "
        f"#{ev.finding_id}", ev.finding.engagement, details={"evidence_id": ev.id})
    session.commit()
    return Response(ev.content, media_type=ev.content_type,
                    headers={**attachment(ev.filename), "X-Content-Type-Options": "nosniff"})


@router.delete("/evidence/{evidence_id}", status_code=204)
def delete_evidence(evidence_id: int, user: User = Depends(require("review")),
                    session: Session = Depends(get_session)) -> Response:
    ev = get_or_404(session, Evidence, evidence_id)
    f = ev.finding
    if f.status == "cleared":
        raise HTTPException(400, "Reopen the finding before removing its evidence.")
    text = f"Removed evidence {ev.filename} (SHA-256 {ev.sha256})"
    f.notes.append(FindingNote(text=text, author=user.name))
    log(session, user, "evidence.remove", f"Finding #{f.id}: {text}", f.engagement,
        details={"finding_id": f.id, "sha256": ev.sha256})
    register.touch(f)
    session.delete(ev)
    session.commit()
    return Response(status_code=204)
