"""Stratification, summaries and field statistics for an engagement's files, and the aging
of receivables and payables listings."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import aging, stratify
from .auth import current_user, log
from .db import Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem
from .exports import attachment
from .leadsheets import compute as compute_statements
from .statements import LINE

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class StratifyIn(BaseModel):
    bands: list[float] | None = Field(default=None, max_length=stratify.MAX_BANDS)


def run(upload: Upload, bands: list[float] | None) -> dict:
    e = upload.engagement
    materiality = {"trivial_threshold": e.trivial_threshold,
                   "performance_materiality": e.performance_materiality,
                   "overall_materiality": e.overall_materiality}
    try:
        return stratify.analyze(load_upload(upload).df, bands, materiality)
    except stratify.StratifyError as exc:
        raise HTTPException(400, str(exc)) from exc


def parse_bands(text: str) -> list[float] | None:
    if not text.strip():
        return None
    try:
        return [float(b) for b in text.split(",") if b.strip()]
    except ValueError as exc:
        raise HTTPException(400, "Bands must be amounts separated by commas.") from exc


@router.post("/uploads/{upload_id}/stratify")
def stratify_upload(upload_id: int, body: StratifyIn, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    result = run(upload, body.bands)
    log(session, user, "analysis.stratify", f"Stratified and summarised {upload.filename}",
        upload.engagement, details={"upload_id": upload.id, "sha256": upload.sha256,
                                    "bands": result["bands"]})
    session.commit()
    return {"upload_id": upload.id, "filename": upload.filename, **stratify.trimmed(result)}


@router.get("/uploads/{upload_id}/stratify/export")
def export_stratify(upload_id: int, bands: str = "", user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    result = run(upload, parse_bands(bands))
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the stratification workpaper for {upload.filename}", e,
        details={"upload_id": upload.id, "sha256": upload.sha256, "bands": result["bands"]})
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(),
               "File": f"{upload.filename} (SHA-256 {upload.sha256})"}
    stem = safe_stem(f"{e.client.name}-{e.name}-stratification")
    return Response(stratify.to_excel(result, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))


# --- Aging of receivables and payables ---------------------------------------

LEDGER_LINE = {"ar": "receivables", "ap": "payables"}


class AgingIn(BaseModel):
    terms_days: int = Field(default=30, ge=0, le=aging.MAX_TERMS)
    as_of: date | None = None


def ledger_balance(session: Session, upload: Upload) -> dict | None:
    """The adjusted lead sheet total the listing supports, from the latest trial balance."""
    line = LEDGER_LINE[upload.kind]
    try:
        data, tb, _ = compute_statements(session, upload.engagement, None, None)
    except HTTPException:
        return None  # a trial balance that can't be read: the aging still stands on its own
    for sheet in data.get("lead_sheets", []):
        if sheet["line"] == line:
            return {"line": line, "label": LINE[line]["label"], "tb": tb.filename,
                    "balance": sheet["totals"]["adjusted"]}
    return None


def run_aging(session: Session, upload: Upload, body: AgingIn) -> dict:
    if upload.kind not in aging.SIDES:
        raise HTTPException(400, "Pick a receivables or payables listing. Upload one as "
                                 "\"Receivables listing\" or \"Payables listing\".")
    as_of = body.as_of or upload.engagement.period_end
    try:
        result = aging.age(load_upload(upload).df, upload.kind, as_of, body.terms_days)
    except aging.AgingError as exc:
        raise HTTPException(400, str(exc)) from exc
    return aging.with_ledger(result, ledger_balance(session, upload))


@router.post("/uploads/{upload_id}/aging")
def age_upload(upload_id: int, body: AgingIn, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    result = run_aging(session, upload, body)
    log(session, user, "analysis.aging", f"Aged {upload.filename}", upload.engagement,
        details={"upload_id": upload.id, "sha256": upload.sha256, "as_of": result["as_of"],
                 "terms_days": result["terms_days"], "balance": result["totals"]["balance"]})
    session.commit()
    return {"upload_id": upload.id, "filename": upload.filename, **aging.trimmed(result)}


@router.get("/uploads/{upload_id}/aging/export")
def export_aging(upload_id: int, terms_days: int = 30, as_of: date | None = None,
                 user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    try:
        body = AgingIn(terms_days=terms_days, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(400, f"Credit terms must be between 0 and {aging.MAX_TERMS} "
                                 "days.") from exc
    result = run_aging(session, upload, body)
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the aging workpaper for {upload.filename}", e,
        details={"upload_id": upload.id, "sha256": upload.sha256, "as_of": result["as_of"],
                 "terms_days": result["terms_days"]})
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(),
               "File": f"{upload.filename} (SHA-256 {upload.sha256})"}
    stem = safe_stem(f"{e.client.name}-{e.name}-{aging.SIDES[upload.kind].lower()}-aging")
    return Response(aging.to_excel(result, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))
