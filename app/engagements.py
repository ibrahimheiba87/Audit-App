"""Clients, engagements, materiality, uploads and saved audit runs."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from . import checks, loader, materiality, register, report
from .db import Client, Comparison, Engagement, Finding, Run, Sample, Upload, get_session
from .exports import attachment
from .uploads import parse_mapping, read_upload

router = APIRouter(prefix="/api")

UPLOAD_KINDS = {"gl": "General ledger", "tb": "Trial balance", "prior_tb": "Prior-year trial balance"}
FRAMEWORKS = ["IFRS", "US GAAP", "IFRS for SMEs", "Local GAAP", "Other"]


# --- Schemas ----------------------------------------------------------------

class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industry: str = ""
    notes: str = ""


class ClientPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    industry: str | None = None
    notes: str | None = None


class EngagementIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    period_end: date
    period_start: date | None = None
    framework: str = "IFRS"


class EngagementPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    period_end: date | None = None
    period_start: date | None = None
    framework: str | None = None


class MaterialityIn(BaseModel):
    benchmark: str
    benchmark_amount: float
    benchmark_pct: float | None = None
    performance_pct: float | None = None
    trivial_pct: float | None = None
    rationale: str = ""


class RunIn(BaseModel):
    upload_id: int
    cutoff_days: int = Field(default=5, ge=0)
    round_threshold: float = Field(default=1000.0, gt=0)


# --- Serialization ----------------------------------------------------------

def client_out(c: Client, with_engagements: bool = True) -> dict:
    out = {"id": c.id, "name": c.name, "industry": c.industry, "notes": c.notes,
           "created_at": c.created_at.isoformat()}
    if with_engagements:
        out["engagements"] = [engagement_brief(e) for e in c.engagements]
    return out


def engagement_brief(e: Engagement) -> dict:
    return {"id": e.id, "client_id": e.client_id, "name": e.name,
            "period_end": e.period_end.isoformat()}


def materiality_out(e: Engagement) -> dict | None:
    if e.overall_materiality is None:
        return None
    return {
        "benchmark": e.benchmark,
        "benchmark_label": materiality.BENCHMARKS.get(e.benchmark or "", {}).get("label"),
        "benchmark_amount": e.benchmark_amount,
        "benchmark_pct": e.benchmark_pct,
        "performance_pct": e.performance_pct,
        "trivial_pct": e.trivial_pct,
        "overall_materiality": e.overall_materiality,
        "performance_materiality": e.performance_materiality,
        "trivial_threshold": e.trivial_threshold,
        "rationale": e.materiality_rationale,
    }


def upload_out(u: Upload) -> dict:
    return {"id": u.id, "kind": u.kind, "kind_label": UPLOAD_KINDS[u.kind],
            "filename": u.filename, "sha256": u.sha256, "size": u.size, "sheet": u.sheet,
            "mapping": u.mapping, "import": u.import_report,
            "uploaded_at": u.uploaded_at.isoformat()}


def run_brief(r: Run) -> dict:
    s = r.result["summary"]
    return {"id": r.id, "upload_id": r.upload_id, "filename": r.upload.filename,
            "created_at": r.created_at.isoformat(), "settings": r.settings,
            "rows": s["rows"], "findings_by_severity": s["findings_by_severity"]}


def engagement_out(e: Engagement) -> dict:
    return {
        "id": e.id, "client": client_out(e.client, with_engagements=False), "name": e.name,
        "period_start": e.period_start.isoformat() if e.period_start else None,
        "period_end": e.period_end.isoformat(), "framework": e.framework,
        "created_at": e.created_at.isoformat(), "materiality": materiality_out(e),
        "uploads": [upload_out(u) for u in e.uploads],
        "runs": [run_brief(r) for r in reversed(e.runs)],
        "comparisons": [comparison_brief(c) for c in reversed(e.comparisons)],
        "samples": [sample_brief(x) for x in reversed(e.samples)],
        "findings_summary": findings_summary(e),
    }


def findings_summary(e: Engagement) -> dict:
    session = object_session(e)
    register.sync_engagement(session, e)  # runs saved before the register existed
    return register.summary_counts(session, e.id)


def sample_brief(x: Sample) -> dict:
    r = x.result
    return {
        "id": x.id, "created_at": x.created_at.isoformat(), "upload_id": x.upload_id,
        "filename": x.upload.filename, "method": x.method, "method_label": r["method_label"],
        "seed": x.seed, "population": r["population"], "sample_size": r["sample_size"],
        "sample_value": r["sample_value"],
    }


def comparison_brief(c: Comparison) -> dict:
    return {
        "id": c.id, "created_at": c.created_at.isoformat(), "settings": c.settings,
        "current_upload_id": c.current_upload_id, "prior_upload_id": c.prior_upload_id,
        "current_filename": c.current_upload.filename, "prior_filename": c.prior_upload.filename,
        "summary": c.result["summary"],
    }


def safe_stem(text: str) -> str:
    return "".join(ch for ch in text.replace(" ", "_") if ch.isalnum() or ch in "-_.")


# --- Lookups ----------------------------------------------------------------

def get_or_404(session: Session, model, id_: int):
    obj = session.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {id_} not found.")
    return obj


def check_period(start: date | None, end: date) -> None:
    if start and start > end:
        raise HTTPException(400, "Period start must be on or before period end.")


def check_framework(framework: str) -> None:
    if framework not in FRAMEWORKS:
        raise HTTPException(400, f"Framework must be one of: {', '.join(FRAMEWORKS)}.")


# --- Materiality ------------------------------------------------------------

@router.get("/materiality/benchmarks")
def benchmarks() -> dict:
    return {
        "benchmarks": [{"id": k, **v} for k, v in materiality.BENCHMARKS.items()],
        "default_performance_pct": materiality.DEFAULT_PERFORMANCE_PCT,
        "default_trivial_pct": materiality.DEFAULT_TRIVIAL_PCT,
        "frameworks": FRAMEWORKS,
        "upload_kinds": [{"id": k, "label": v} for k, v in UPLOAD_KINDS.items()],
    }


def calc(body: MaterialityIn) -> materiality.Materiality:
    try:
        return materiality.calculate(body.benchmark, body.benchmark_amount, body.benchmark_pct,
                                     body.performance_pct, body.trivial_pct)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/materiality/calculate")
def calculate(body: MaterialityIn) -> dict:
    return calc(body).to_dict()


# --- Clients ----------------------------------------------------------------

@router.get("/clients")
def list_clients(session: Session = Depends(get_session)) -> list[dict]:
    return [client_out(c) for c in session.scalars(select(Client).order_by(Client.name))]


@router.post("/clients", status_code=201)
def create_client(body: ClientIn, session: Session = Depends(get_session)) -> dict:
    client = Client(name=body.name.strip(), industry=body.industry, notes=body.notes)
    session.add(client)
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(409, f"A client named {body.name!r} already exists.") from exc
    return client_out(client)


@router.get("/clients/{client_id}")
def get_client(client_id: int, session: Session = Depends(get_session)) -> dict:
    return client_out(get_or_404(session, Client, client_id))


@router.patch("/clients/{client_id}")
def update_client(client_id: int, body: ClientPatch,
                  session: Session = Depends(get_session)) -> dict:
    client = get_or_404(session, Client, client_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(client, key, value.strip() if key == "name" else value)
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(409, f"A client named {body.name!r} already exists.") from exc
    return client_out(client)


@router.delete("/clients/{client_id}", status_code=204)
def delete_client(client_id: int, session: Session = Depends(get_session)) -> Response:
    session.delete(get_or_404(session, Client, client_id))
    session.commit()
    return Response(status_code=204)


# --- Engagements ------------------------------------------------------------

@router.post("/clients/{client_id}/engagements", status_code=201)
def create_engagement(client_id: int, body: EngagementIn,
                      session: Session = Depends(get_session)) -> dict:
    client = get_or_404(session, Client, client_id)
    check_period(body.period_start, body.period_end)
    check_framework(body.framework)
    engagement = Engagement(client=client, name=body.name.strip(), period_end=body.period_end,
                            period_start=body.period_start, framework=body.framework)
    session.add(engagement)
    session.commit()
    return engagement_out(engagement)


@router.get("/engagements/{engagement_id}")
def get_engagement(engagement_id: int, session: Session = Depends(get_session)) -> dict:
    return engagement_out(get_or_404(session, Engagement, engagement_id))


@router.patch("/engagements/{engagement_id}")
def update_engagement(engagement_id: int, body: EngagementPatch,
                      session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("framework") is not None:
        check_framework(changes["framework"])
    for key, value in changes.items():
        if value is not None or key == "period_start":
            setattr(e, key, value)
    check_period(e.period_start, e.period_end)
    session.commit()
    return engagement_out(e)


@router.delete("/engagements/{engagement_id}", status_code=204)
def delete_engagement(engagement_id: int, session: Session = Depends(get_session)) -> Response:
    session.delete(get_or_404(session, Engagement, engagement_id))
    session.commit()
    return Response(status_code=204)


@router.put("/engagements/{engagement_id}/materiality")
def set_materiality(engagement_id: int, body: MaterialityIn,
                    session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    m = calc(body)
    for key, value in m.to_dict().items():
        setattr(e, key, value)
    e.materiality_rationale = body.rationale
    session.commit()
    return engagement_out(e)


# --- Uploads and runs -------------------------------------------------------

@router.post("/engagements/{engagement_id}/uploads", status_code=201)
async def create_upload(
    engagement_id: int,
    file: UploadFile = File(...),
    kind: str = Form("gl"),
    sheet: str = Form(""),
    mapping: str = Form(""),
    session: Session = Depends(get_session),
) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"Kind must be one of: {', '.join(UPLOAD_KINDS)}.")
    content = await read_upload(file)
    filename = file.filename or "upload"
    try:
        loaded = loader.load(filename, content, parse_mapping(mapping), sheet or None)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    upload = Upload(
        engagement=e, kind=kind, filename=filename, content=content, size=len(content),
        sha256=hashlib.sha256(content).hexdigest(), sheet=sheet or None,
        mapping=loaded.report.mapping, import_report=loaded.report.to_dict(),
    )
    session.add(upload)
    session.commit()
    return upload_out(upload)


@router.delete("/uploads/{upload_id}", status_code=204)
def delete_upload(upload_id: int, session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id)
    findings = list(session.scalars(select(Finding).where(Finding.upload_id == upload_id)))
    worked = [f for f in findings if register.is_worked(f)]
    if worked:
        raise HTTPException(409, f"{len(worked)} findings from this file have been reviewed or "
                                 "have notes or evidence, so the file is kept as support.")
    for f in findings:
        session.delete(f)
    for run in session.scalars(select(Run).where(Run.upload_id == upload_id)):
        session.delete(run)
    for x in session.scalars(select(Sample).where(Sample.upload_id == upload_id)):
        session.delete(x)
    for c in session.scalars(select(Comparison).where(
            (Comparison.current_upload_id == upload_id) | (Comparison.prior_upload_id == upload_id))):
        session.delete(c)
    session.delete(upload)
    session.commit()
    return Response(status_code=204)


def load_upload(upload: Upload) -> loader.Loaded:
    try:
        return loader.load(upload.filename, upload.content, upload.mapping, upload.sheet)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc


def engagement_settings(e: Engagement, cutoff_days: int, round_threshold: float) -> dict:
    return {
        "period_end": e.period_end.isoformat(),
        "cutoff_days": cutoff_days,
        "round_threshold": round_threshold,
        "performance_materiality": e.performance_materiality,
        "trivial_threshold": e.trivial_threshold,
    }


@router.post("/engagements/{engagement_id}/runs", status_code=201)
def create_run(engagement_id: int, body: RunIn,
               session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    upload = get_or_404(session, Upload, body.upload_id)
    if upload.engagement_id != e.id:
        raise HTTPException(400, "That upload belongs to another engagement.")
    loaded = load_upload(upload)
    settings = engagement_settings(e, body.cutoff_days, body.round_threshold)
    result = checks.run_all(
        loaded.df,
        checks.Settings(**{**settings, "period_end": e.period_end}),
        loaded.report.to_dict(),
    )
    run = Run(engagement=e, upload=upload, settings=settings, result=result)
    session.add(run)
    session.flush()
    register.sync_run_findings(session, run)
    session.commit()
    return {**run_brief(run), "result": result}


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    run = get_or_404(session, Run, run_id)
    return {**run_brief(run), "engagement_id": run.engagement_id, "result": run.result}


@router.get("/runs/{run_id}/report")
def run_report(run_id: int, format: str = "xlsx",
               session: Session = Depends(get_session)) -> Response:
    run = get_or_404(session, Run, run_id)
    e = run.engagement
    stem = safe_stem(f"{e.client.name}-{e.name}-run{run.id}")
    if format == "csv":
        return Response(
            report.to_csv(run.result), media_type="text/csv",
            headers=attachment(f"{stem}-findings.csv"),
        )
    if format != "xlsx":
        raise HTTPException(400, "Format must be csv or xlsx.")
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(), "Framework": e.framework,
               "Run": f"#{run.id} at {run.created_at:%Y-%m-%d %H:%M} UTC",
               "File SHA-256": run.upload.sha256}
    return Response(
        report.to_excel(run.result, Path(run.upload.filename).name, context),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=attachment(f"{stem}-audit-report.xlsx"),
    )
