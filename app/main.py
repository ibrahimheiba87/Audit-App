"""FastAPI app: upload a ledger, run audit checks, download the findings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import checks, loader, report

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Audit App")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 50 MB.")
    return content


def parse_mapping(mapping: str) -> dict[str, str] | None:
    """The mapping form field is JSON like {"date": "Txn Date", ...}; empty means guess."""
    if not mapping:
        return None
    try:
        value = json.loads(mapping)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Mapping must be a JSON object.") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str | None) for k, v in value.items()
    ):
        raise HTTPException(400, "Mapping must be a JSON object of field to column name.")
    return value


@dataclass
class AuditForm:
    file: UploadFile
    period_end: str
    cutoff_days: int
    round_threshold: float
    mapping: str
    sheet: str


def audit_form(
    file: UploadFile = File(...),
    period_end: str = Form(""),
    cutoff_days: int = Form(5),
    round_threshold: float = Form(1000.0),
    mapping: str = Form(""),
    sheet: str = Form(""),
) -> AuditForm:
    return AuditForm(file, period_end, cutoff_days, round_threshold, mapping, sheet)


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
