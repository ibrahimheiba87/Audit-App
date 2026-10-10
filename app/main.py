"""FastAPI app: upload a ledger, run audit checks, download the findings."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from . import checks, loader, report

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

app = FastAPI(title="Audit App")


async def _run(file: UploadFile, period_end: str, cutoff_days: int,
               round_threshold: float) -> dict:
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 50 MB.")
    try:
        df = loader.load(file.filename or "", content)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        end = date.fromisoformat(period_end) if period_end else None
    except ValueError as exc:
        raise HTTPException(400, "Period end must be a date like 2026-12-31.") from exc
    settings = checks.Settings(
        period_end=end, cutoff_days=cutoff_days, round_threshold=round_threshold,
    )
    return checks.run_all(df, settings)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/audit")
async def audit(
    file: UploadFile = File(...),
    period_end: str = Form(""),
    cutoff_days: int = Form(5),
    round_threshold: float = Form(1000.0),
) -> dict:
    return await _run(file, period_end, cutoff_days, round_threshold)


@app.post("/api/report")
async def download_report(
    file: UploadFile = File(...),
    period_end: str = Form(""),
    cutoff_days: int = Form(5),
    round_threshold: float = Form(1000.0),
    format: str = Form("xlsx"),
) -> Response:
    result = await _run(file, period_end, cutoff_days, round_threshold)
    stem = Path(file.filename or "ledger").stem
    if format == "csv":
        return Response(
            report.to_csv(result), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{stem}-findings.csv"'},
        )
    if format == "xlsx":
        return Response(
            report.to_excel(result, file.filename or ""),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{stem}-audit-report.xlsx"'},
        )
    raise HTTPException(400, "Format must be csv or xlsx.")
