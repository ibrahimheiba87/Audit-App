"""FastAPI app: upload a ledger, run audit checks, download the findings."""

from __future__ import annotations

import hashlib
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import checks, db, loader, portal, report
from .accounts import router as accounts_router
from .ai_tools import router as ai_tools_router
from .assistant import router as assistant_router
from .auth import current_user, log
from .comparisons import router as comparisons_router
from .dashboard import router as dashboard_router
from .engagements import router as engagements_router
from .exports import attachment
from .findings import router as findings_router
from .leadsheets import router as leadsheets_router
from .portal import router as portal_router
from .reconciliations import router as reconciliations_router
from .review import router as review_router
from .rollforward import router as rollforward_router
from .samples import router as samples_router
from .uploads import FORM_OVERHEAD, MAX_UPLOAD_BYTES, BodyLimit, parse_mapping, read_upload

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Audit App", lifespan=lifespan)
# Refuse oversized bodies as they arrive; client users get the portal's smaller limit.
app.add_middleware(BodyLimit, default=lambda: MAX_UPLOAD_BYTES + FORM_OVERHEAD, limits=[
    (r"^/api/requests/\d+/files$", lambda: portal.MAX_FILE_BYTES + FORM_OVERHEAD)])
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(accounts_router)
# Everything else needs a signed-in user; endpoints check the role they need.
for router in (engagements_router, comparisons_router, samples_router, findings_router,
               leadsheets_router, review_router, assistant_router, dashboard_router,
               reconciliations_router, rollforward_router, ai_tools_router):
    app.include_router(router, dependencies=[Depends(current_user)])
# The document request portal is also open to client users; each endpoint checks access.
app.include_router(portal_router)


@dataclass
class AuditForm:
    file: UploadFile
    period_end: str
    cutoff_days: int
    round_threshold: float
    mapping: str
    sheet: str
    performance_materiality: float | None
    weekend: str
    keywords: str | None


def audit_form(
    file: UploadFile = File(...),
    period_end: str = Form(""),
    cutoff_days: int = Form(5),
    round_threshold: float = Form(1000.0),
    mapping: str = Form(""),
    sheet: str = Form(""),
    performance_materiality: float | None = Form(None),
    weekend: str = Form("sat-sun"),
    keywords: str | None = Form(None),
) -> AuditForm:
    # Same limits as a saved engagement run (RunIn): a negative cutoff, a zero threshold or a
    # negative performance materiality would flag nonsense.
    if cutoff_days < 0:
        raise HTTPException(400, "Cutoff days must be zero or more.")
    if not math.isfinite(round_threshold) or round_threshold <= 0:
        raise HTTPException(400, "The round-amount threshold must be a number above zero.")
    if performance_materiality is not None and (not math.isfinite(performance_materiality)
                                                or performance_materiality < 0):
        raise HTTPException(400, "Performance materiality must be a number of zero or more.")
    if weekend not in checks.WEEKENDS:
        raise HTTPException(400, "Unknown weekend choice.")
    return AuditForm(file, period_end, cutoff_days, round_threshold, mapping, sheet,
                     performance_materiality, weekend, keywords)


async def run_audit(form: AuditForm, user: db.User, session: Session, fmt: str) -> dict:
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
    keywords = checks.parse_keywords(form.keywords)
    settings = checks.Settings(
        period_end=end, cutoff_days=form.cutoff_days, round_threshold=form.round_threshold,
        performance_materiality=form.performance_materiality or None,
        weekend=form.weekend, keywords=keywords,
    )
    result = checks.run_all(loaded.df, settings, loaded.report.to_dict())
    log(session, user, "quick.audit", f"Ran the audit checks on {form.file.filename} without saving",
        details={"sha256": hashlib.sha256(content).hexdigest(), "format": fmt,
                 "settings": {"period_end": form.period_end, "cutoff_days": form.cutoff_days,
                              "round_threshold": form.round_threshold,
                              "performance_materiality": form.performance_materiality,
                              "weekend": form.weekend, "keywords": list(keywords)}})
    session.commit()
    return result


def page() -> str:
    """The page, with each script and stylesheet tagged by its content, so a browser that
    cached an older copy (Cloudflare tells browsers to keep them a week) fetches the new one."""
    def versioned(m: re.Match) -> str:
        digest = hashlib.sha256((STATIC / m[1]).read_bytes()).hexdigest()[:12]
        return f'"/static/{m[1]}?v={digest}"'
    return re.sub(r'"/static/([\w.-]+\.(?:js|css))"', versioned, (STATIC / "index.html").read_text("utf-8"))


PAGE = page()


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/checks/options")
def check_options(_: db.User = Depends(current_user)) -> dict:
    """Choices for the audit check settings."""
    return {
        "weekends": [{"id": k, "label": v["label"]} for k, v in checks.WEEKENDS.items()],
        "keywords": list(checks.DEFAULT_KEYWORDS),
    }


@app.post("/api/preview")
async def preview(file: UploadFile = File(...), sheet: str = Form(""),
                  _: db.User = Depends(current_user)) -> dict:
    """Columns, sample rows and a suggested column mapping for an uploaded file."""
    content = await read_upload(file)
    try:
        return loader.preview(file.filename or "", content, sheet or None)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/audit")
async def audit(form: AuditForm = Depends(audit_form), user: db.User = Depends(current_user),
                session: Session = Depends(db.get_session)) -> dict:
    return await run_audit(form, user, session, "json")


@app.post("/api/report")
async def download_report(
    form: AuditForm = Depends(audit_form), format: str = Form("xlsx"),
    user: db.User = Depends(current_user), session: Session = Depends(db.get_session),
) -> Response:
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "Format must be csv or xlsx.")
    result = await run_audit(form, user, session, format)
    stem = Path(form.file.filename or "ledger").stem
    if format == "csv":
        return Response(
            report.to_csv(result), media_type="text/csv",
            headers=attachment(f"{stem}-findings.csv"),
        )
    return Response(
        report.to_excel(result, form.file.filename or ""),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=attachment(f"{stem}-audit-report.xlsx"),
    )
