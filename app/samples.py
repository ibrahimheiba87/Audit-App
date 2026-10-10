"""Audit sampling endpoints."""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import loader, sampling
from .auth import current_user, log, require
from .db import Engagement, Sample, Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem, sample_brief
from .exports import attachment
from .uploads import parse_mapping, read_upload

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class SampleIn(BaseModel):
    upload_id: int
    method: Literal["random", "systematic", "mus"]
    sample_size: int | None = Field(default=None, ge=1, le=100_000)
    seed: int | None = Field(default=None, ge=0, lt=2**31)
    population: Literal["all", "debits", "credits"] = "all"
    min_amount: float = Field(default=0.0, ge=0)
    confidence: int = 95
    tolerable_misstatement: float | None = Field(default=None, gt=0)
    expected_misstatement: float = Field(default=0.0, ge=0)


def draw(df, params: sampling.SampleParams) -> dict:
    try:
        return sampling.draw(df, params)
    except sampling.SamplingError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/sampling/options")
def options() -> dict:
    return {
        "methods": [{"id": k, "label": v} for k, v in sampling.METHODS.items()],
        "populations": [{"id": k, "label": v} for k, v in sampling.POPULATIONS.items()],
        "confidence_levels": list(sampling.CONFIDENCE),
    }


@router.post("/engagements/{engagement_id}/samples", status_code=201)
def create_sample(engagement_id: int, body: SampleIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    upload = get_or_404(session, Upload, body.upload_id)
    if upload.engagement_id != e.id:
        raise HTTPException(400, "That file belongs to another engagement.")
    values = body.model_dump(exclude={"upload_id"})
    if body.method == "mus" and not body.sample_size and not body.tolerable_misstatement:
        values["tolerable_misstatement"] = e.performance_materiality
    result = draw(load_upload(upload).df, sampling.SampleParams(**values))
    sample = Sample(engagement=e, upload=upload, method=body.method, seed=result["seed"],
                    params=result["params"], result=result)
    session.add(sample)
    session.flush()
    log(session, user, "sample.create",
        f"Drew a {result['method_label']} sample of {result['sample_size']} from "
        f"{upload.filename} (seed {result['seed']})", e,
        details={"sample_id": sample.id, "upload_id": upload.id, "sha256": upload.sha256,
                 "params": result["params"]})
    session.commit()
    return {**sample_brief(sample), "result": result}


@router.get("/samples/{sample_id}")
def get_sample(sample_id: int, session: Session = Depends(get_session)) -> dict:
    x = get_or_404(session, Sample, sample_id)
    return {**sample_brief(x), "engagement_id": x.engagement_id, "result": x.result}


@router.get("/samples/{sample_id}/export")
def export_sample(sample_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> Response:
    x = get_or_404(session, Sample, sample_id)
    e = x.engagement
    log(session, user, "sample.download", f"Downloaded sample #{x.id} workpaper", e,
        details={"sample_id": x.id})
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(),
               "File": f"{x.upload.filename} (SHA-256 {x.upload.sha256})",
               "Drawn": f"{x.created_at:%Y-%m-%d %H:%M} UTC"}
    stem = safe_stem(f"{e.client.name}-{e.name}-sample{x.id}")
    return Response(sampling.to_excel(x.result, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))


@router.post("/sample")
async def sample_file(
    file: UploadFile = File(...),
    mapping: str = Form(""),
    sheet: str = Form(""),
    method: str = Form(...),
    sample_size: int | None = Form(None),
    seed: int | None = Form(None),
    population: str = Form("all"),
    min_amount: float = Form(0.0),
    confidence: int = Form(95),
    tolerable_misstatement: float | None = Form(None),
    expected_misstatement: float = Form(0.0),
    format: str = Form("json"),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Draw a sample from a file without saving it. format=xlsx returns the workpaper."""
    content = await read_upload(file)
    try:
        df = loader.load(file.filename or "", content, parse_mapping(mapping), sheet or None).df
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    params = sampling.SampleParams(method, sample_size, seed, population, min_amount, confidence,
                                   tolerable_misstatement, expected_misstatement)
    result = draw(df, params)
    log(session, user, "quick.sample", f"Drew a {result['method_label']} sample from "
        f"{file.filename} without saving",
        details={"sha256": hashlib.sha256(content).hexdigest(), "params": result["params"]})
    session.commit()
    if format == "xlsx":
        return Response(sampling.to_excel(result, {"File": file.filename}), media_type=XLSX,
                        headers=attachment("sample.xlsx"))
    return result
