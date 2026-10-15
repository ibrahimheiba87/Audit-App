"""Stratification, summaries and field statistics for an engagement's files."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import stratify
from .auth import current_user, log
from .db import Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem
from .exports import attachment

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
