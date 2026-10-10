"""Prior-year comparison endpoints, for engagements and for two ad hoc files."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import compare, loader
from .auth import current_user, log, require
from .db import Comparison, Engagement, Upload, User, get_session
from .engagements import comparison_brief, get_or_404, load_upload, safe_stem
from .exports import attachment
from .uploads import parse_mapping, read_upload

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ComparisonIn(BaseModel):
    current_upload_id: int
    prior_upload_id: int
    threshold_amount: float | None = Field(default=None, gt=0)
    threshold_pct: float = Field(default=10.0, ge=0)


def run_compare(current, prior, settings: compare.CompareSettings) -> dict:
    try:
        return compare.compare(current, prior, settings)
    except compare.CompareError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/engagements/{engagement_id}/comparisons", status_code=201)
def create_comparison(engagement_id: int, body: ComparisonIn,
                      user: User = Depends(require("prepare")),
                      session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    current = get_or_404(session, Upload, body.current_upload_id)
    prior = get_or_404(session, Upload, body.prior_upload_id)
    if current.engagement_id != e.id or prior.engagement_id != e.id:
        raise HTTPException(400, "Both files must belong to this engagement.")
    if current.id == prior.id:
        raise HTTPException(400, "Pick two different files to compare.")
    threshold = body.threshold_amount or e.performance_materiality
    if not threshold:
        raise HTTPException(
            400, "Enter a threshold amount, or set materiality so performance materiality is used."
        )
    settings = compare.CompareSettings(threshold, body.threshold_pct)
    result = run_compare(load_upload(current).df, load_upload(prior).df, settings)
    c = Comparison(engagement=e, current_upload=current, prior_upload=prior,
                   settings=result["settings"], result=result)
    session.add(c)
    session.flush()
    log(session, user, "comparison.create",
        f"Compared {current.filename} with {prior.filename}: "
        f"{result['summary']['flagged']} accounts flagged", e,
        details={"comparison_id": c.id, "current_sha256": current.sha256,
                 "prior_sha256": prior.sha256, "settings": result["settings"]})
    session.commit()
    return {**comparison_brief(c), "result": result}


@router.get("/comparisons/{comparison_id}")
def get_comparison(comparison_id: int, session: Session = Depends(get_session)) -> dict:
    c = get_or_404(session, Comparison, comparison_id)
    return {**comparison_brief(c), "engagement_id": c.engagement_id, "result": c.result}


@router.get("/comparisons/{comparison_id}/report")
def comparison_report(comparison_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    c = get_or_404(session, Comparison, comparison_id)
    e = c.engagement
    log(session, user, "comparison.download", f"Downloaded comparison #{c.id}", e,
        details={"comparison_id": c.id, "current_sha256": c.current_upload.sha256,
                 "prior_sha256": c.prior_upload.sha256, "settings": c.settings})
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(),
               "This year's file": f"{c.current_upload.filename} ({c.current_upload.sha256[:12]})",
               "Prior year's file": f"{c.prior_upload.filename} ({c.prior_upload.sha256[:12]})",
               "Compared": f"{c.created_at:%Y-%m-%d %H:%M} UTC"}
    stem = safe_stem(f"{e.client.name}-{e.name}-comparison{c.id}")
    return Response(compare.to_excel(c.result, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))


@router.post("/compare")
async def compare_files(
    current: UploadFile = File(...),
    prior: UploadFile = File(...),
    current_mapping: str = Form(""),
    prior_mapping: str = Form(""),
    current_sheet: str = Form(""),
    prior_sheet: str = Form(""),
    threshold_amount: float = Form(...),
    threshold_pct: float = Form(10.0),
    format: str = Form("json"),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Compare two files without saving them. format=xlsx returns the Excel report."""
    frames, hashes = [], []
    for f, mapping, sheet in ((current, current_mapping, current_sheet),
                              (prior, prior_mapping, prior_sheet)):
        content = await read_upload(f)
        hashes.append(hashlib.sha256(content).hexdigest())
        try:
            frames.append(loader.load(f.filename or "", content, parse_mapping(mapping),
                                      sheet or None).df)
        except loader.LoadError as exc:
            raise HTTPException(400, f"{f.filename}: {exc}") from exc
    result = run_compare(*frames, compare.CompareSettings(threshold_amount, threshold_pct))
    log(session, user, "quick.compare", f"Compared {current.filename} with {prior.filename} "
        "without saving", details={"current_sha256": hashes[0], "prior_sha256": hashes[1],
                                   "settings": result["settings"], "format": format})
    session.commit()
    if format == "xlsx":
        context = {"This year's file": current.filename, "Prior year's file": prior.filename}
        return Response(compare.to_excel(result, context), media_type=XLSX,
                        headers=attachment("comparison.xlsx"))
    return result
