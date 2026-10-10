"""FastAPI app: upload a ledger, run audit checks, download the findings."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import checks, db, loader, report
from .comparisons import router as comparisons_router
from .engagements import router as engagements_router
from .findings import router as findings_router
from .samples import router as samples_router
from .uploads import parse_mapping, read_upload

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Audit App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(engagements_router)
app.include_router(comparisons_router)
app.include_router(samples_router)
app.include_router(findings_router)


@dataclass
class AuditForm:
    file: UploadFile
    period_end: str
    cutoff_days: int
    round_threshold: float
    mapping: str
    sheet: str
    performance_materiality: float | None


def audit_form(
    file: UploadFile = File(...),
    period_end: str = Form(""),
    cutoff_days: int = Form(5),
    round_threshold: float = Form(1000.0),
    mapping: str = Form(""),
    sheet: str = Form(""),
    performance_materiality: float | None = Form(None),
) -> AuditForm:
    return AuditForm(file, period_end, cutoff_days, round_threshold, mapping, sheet,
                     performance_materiality)


async def run_audit(form: AuditForm) -> dict:
    content = await read_upload(form.file)
    try:
        loaded = loader.load(form.file.filename or "", content,
                             parse_mapping(form.mapping), form.sheet or None)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        end = date.fromisoformat(form.period_end) if form.period_end else None
    except ValueError as exc:
        raise HTTPException(400, "Period end must be a date like 2026-12-31.") from exc
    settings = checks.Settings(
        period_end=end, cutoff_days=form.cutoff_days, round_threshold=form.round_threshold,
        performance_materiality=form.performance_materiality or None,
    )
    return checks.run_all(loaded.df, settings, loaded.report.to_dict())


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/preview")
async def preview(file: UploadFile = File(...), sheet: str = Form("")) -> dict:
    """Columns, sample rows and a suggested column mapping for an uploaded file."""
    content = await read_upload(file)
    try:
        return loader.preview(file.filename or "", content, sheet or None)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/audit")
async def audit(form: AuditForm = Depends(audit_form)) -> dict:
    return await run_audit(form)


@app.post("/api/report")
async def download_report(
    form: AuditForm = Depends(audit_form), format: str = Form("xlsx"),
) -> Response:
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "Format must be csv or xlsx.")
    result = await run_audit(form)
    stem = Path(form.file.filename or "ledger").stem
    if format == "csv":
        return Response(
            report.to_csv(result), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{stem}-findings.csv"'},
        )
    return Response(
        report.to_excel(result, form.file.filename or ""),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{stem}-audit-report.xlsx"'},
    )
