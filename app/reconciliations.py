"""Bank reconciliation endpoints: match a bank statement upload to the cash account in the GL."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import bankrec
from .auth import current_user, log, require
from .db import BankReconciliation, Engagement, Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem, upload_out
from .exports import attachment

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class BankRecIn(BaseModel):
    bank_upload_id: int
    gl_upload_id: int
    accounts: list[str] = Field(min_length=1, max_length=50)
    window_days: int = Field(default=5, ge=0, le=60)
    bank_balance: float | None = None
    book_balance: float | None = None
    flip_bank: bool = False


def rec_brief(r: BankReconciliation) -> dict:
    return {
        "id": r.id, "created_at": r.created_at.isoformat(), "created_by": r.created_by,
        "bank_upload_id": r.bank_upload_id, "gl_upload_id": r.gl_upload_id,
        "bank_filename": r.bank_upload.filename, "gl_filename": r.gl_upload.filename,
        "settings": r.settings, "summary": r.result["summary"],
        "difference": r.result["statement"]["difference"],
    }


def latest(e: Engagement, kind: str) -> Upload | None:
    choices = [u for u in e.uploads if u.kind == kind]
    return max(choices, key=lambda u: (u.uploaded_at, u.id)) if choices else None


def tb_balances(e: Engagement, codes: set[str]) -> dict[str, float] | None:
    """This year's trial balance amount for each account code, or None without one."""
    tb = latest(e, "tb")
    if tb is None:
        return None
    df = load_upload(tb).df
    if "account" not in df:
        return None
    out: dict[str, float] = {}
    for account, amount in zip(df["account"].fillna(""), df["amount"], strict=True):
        code = bankrec.account_code(str(account))
        if code in codes:
            out[code] = round(out.get(code, 0.0) + float(amount), 2)
    return out


def own_upload(session: Session, user: User, e: Engagement, upload_id: int, kind: str,
               label: str) -> Upload:
    u = get_or_404(session, Upload, upload_id, user)
    if u.engagement_id != e.id or u.kind != kind:
        raise HTTPException(400, f"Pick a {label} uploaded to this engagement.")
    return u


@router.get("/engagements/{engagement_id}/bank-reconciliations/options")
def options(engagement_id: int, gl_upload_id: int | None = None,
            user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    """The files to choose from, and the ledger accounts that look like cash."""
    e = get_or_404(session, Engagement, engagement_id, user)
    gl = (own_upload(session, user, e, gl_upload_id, "gl", "general ledger")
          if gl_upload_id is not None else latest(e, "gl"))
    accounts: list[dict] = []
    balances = None
    if gl is not None:
        df = load_upload(gl).df
        names = sorted(df["account"].dropna().astype(str).unique()) if "account" in df else []
        mapped = {m.account for m in e.account_mappings if m.line == "cash"}
        cash = set(bankrec.cash_accounts(names, mapped))
        counts = df["account"].astype(str).value_counts() if "account" in df else {}
        accounts = [{"account": a, "code": bankrec.account_code(a), "cash": a in cash,
                     "lines": int(counts.get(a, 0))} for a in names]
        balances = tb_balances(e, {a["code"] for a in accounts})
    return {
        "period_end": e.period_end.isoformat(),
        "bank_uploads": [upload_out(u) for u in e.uploads if u.kind == "bank"],
        "gl_uploads": [upload_out(u) for u in e.uploads if u.kind == "gl"],
        "gl_upload_id": gl.id if gl else None,
        "accounts": accounts,
        "tb_balances": balances,  # code -> amount in this year's trial balance
        "reconciliations": [rec_brief(r) for r in reversed(e.bank_reconciliations)],
    }


@router.post("/engagements/{engagement_id}/bank-reconciliations", status_code=201)
def create_reconciliation(engagement_id: int, body: BankRecIn,
                          user: User = Depends(require("prepare")),
                          session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    bank_upload = own_upload(session, user, e, body.bank_upload_id, "bank", "bank statement")
    gl_upload = own_upload(session, user, e, body.gl_upload_id, "gl", "general ledger")
    accounts = list(dict.fromkeys(a for a in body.accounts if a))
    gl = load_upload(gl_upload).df
    known = set(gl["account"].dropna().astype(str)) if "account" in gl else set()
    missing = [a for a in accounts if a not in known]
    if missing:
        raise HTTPException(400, f"Not in the general ledger: {', '.join(missing[:5])}.")
    book_balance, book_source = body.book_balance, "entered"
    if book_balance is None:
        balances = tb_balances(e, {bankrec.account_code(a) for a in accounts})
        if balances:
            book_balance, book_source = round(sum(balances.values()), 2), "trial balance"
    bank_balance, bank_source = body.bank_balance, "entered"
    read = (bank_upload.import_report or {}).get("read_by_ai") or {}
    if (bank_balance is None and read.get("period_end") == e.period_end.isoformat()
            and read.get("balance_at_period_end") is not None and not read.get("problems")):
        bank_balance, bank_source = read["balance_at_period_end"], "statement"
    settings = bankrec.Settings(e.period_end, accounts, body.window_days, bank_balance,
                                book_balance, body.flip_bank, e.period_start)
    bank = load_upload(bank_upload)
    try:
        result = bankrec.reconcile(
            bankrec.bank_frame(bank.df, "amount" in (bank_upload.mapping or {}), body.flip_bank),
            bankrec.ledger_frame(gl, accounts), settings)
    except bankrec.BankRecError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["settings"]["book_balance_source"] = book_source if book_balance is not None else None
    result["settings"]["bank_balance_source"] = bank_source if bank_balance is not None else None
    r = BankReconciliation(engagement=e, bank_upload=bank_upload, gl_upload=gl_upload,
                           settings=result["settings"], result=result, created_by=user.name)
    session.add(r)
    session.flush()
    s, st = result["summary"], result["statement"]
    diff = "" if st["difference"] is None else f", unexplained difference {st['difference']:,.2f}"
    log(session, user, "bankrec.create",
        f"Reconciled {bank_upload.filename} with {', '.join(accounts)}: {s['matched']} matched, "
        f"{s['outstanding']} outstanding, {s['bank_only']} not in the books{diff}", e,
        details={"reconciliation_id": r.id, "bank_sha256": bank_upload.sha256,
                 "gl_sha256": gl_upload.sha256, "settings": result["settings"]})
    session.commit()
    return {**rec_brief(r), "result": result}


@router.get("/bank-reconciliations/{rec_id}")
def get_reconciliation(rec_id: int, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> dict:
    r = get_or_404(session, BankReconciliation, rec_id, user)
    return {**rec_brief(r), "engagement_id": r.engagement_id, "result": r.result}


@router.get("/bank-reconciliations/{rec_id}/report")
def reconciliation_report(rec_id: int, user: User = Depends(current_user),
                          session: Session = Depends(get_session)) -> Response:
    r = get_or_404(session, BankReconciliation, rec_id, user)
    e = r.engagement
    log(session, user, "bankrec.download", f"Downloaded bank reconciliation #{r.id}", e,
        details={"reconciliation_id": r.id, "bank_sha256": r.bank_upload.sha256,
                 "gl_sha256": r.gl_upload.sha256})
    session.commit()
    name, content = workbook(r)
    return Response(content, media_type=XLSX, headers=attachment(name))


def workbook(r: BankReconciliation) -> tuple[str, bytes]:
    """A bank reconciliation as an Excel file, and its name."""
    e = r.engagement
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": r.settings["period_end"],
               "Bank statement": f"{r.bank_upload.filename} ({r.bank_upload.sha256[:12]})",
               "General ledger": f"{r.gl_upload.filename} ({r.gl_upload.sha256[:12]})",
               "Reconciled": f"{r.created_at:%Y-%m-%d %H:%M} UTC by {r.created_by}"}
    stem = safe_stem(f"{e.client.name}-{e.name}-bank-reconciliation{r.id}")
    return f"{stem}.xlsx", bankrec.to_excel(r.result, context)
