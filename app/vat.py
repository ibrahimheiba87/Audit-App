"""VAT reconciliation, as in Inflo and the tax tools of CCH: the VAT returns the client filed
are compared, period by period, with the general ledger.

For each return the app works out from the ledger
- the sales: the net credits to accounts on the revenue line;
- the output VAT: the net credits to the output VAT accounts;
- the input VAT: the net debits to the input VAT accounts;
and compares them with what was declared. It also checks the output VAT declared against
the sales declared at the VAT rate, and shows the ledger's sales that no return covers.

The team enters each return's figures and picks the ledger, the rate and the VAT accounts
(the app suggests accounts whose names mention VAT, GST or sales tax). Nothing is posted;
every change is logged and the reconciliation downloads to Excel.
"""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import statements
from .auth import current_user, log, require
from .db import Engagement, Upload, User, VatReturn, VatSetup, get_session, utcnow
from .engagements import get_or_404, held_engagement, load_upload, safe_stem
from .exports import attachment, finish_workbook
from .posters import lines_of, text

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MAX_RETURNS = 60
MAX_ACCOUNTS = 50
VAT_WORDS = re.compile(r"\b(vat|gst|sales tax|value added tax)\b|ضريبة القيمة المضافة", re.I)
INPUT_WORDS = re.compile(r"\b(input|recoverable|receivable|reclaim)", re.I)
TOLERANCE = 1.0  # differences under one unit are rounding

Money = Annotated[float, Field(ge=-1e13, le=1e13, allow_inf_nan=False)]
Account = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SetupIn(BaseModel):
    upload_id: int | None = None
    rate_pct: float = Field(default=14.0, ge=0, le=100, allow_inf_nan=False)
    output_accounts: list[Account] = Field(default_factory=list, max_length=MAX_ACCOUNTS)
    input_accounts: list[Account] = Field(default_factory=list, max_length=MAX_ACCOUNTS)


class ReturnIn(BaseModel):
    period_start: date
    period_end: date
    taxable_sales: Money = 0.0
    output_vat: Money = 0.0
    input_vat: Money = 0.0
    reference: str = Field(default="", max_length=200)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def ledger_of(e: Engagement, s: VatSetup | None) -> Upload | None:
    if s is None or s.upload_id is None:
        return None
    return next((u for u in e.uploads if u.id == s.upload_id and u.kind == "gl"), None)


def setup_out(s: VatSetup | None) -> dict | None:
    if s is None:
        return None
    return {"upload_id": s.upload_id, "rate_pct": s.rate_pct,
            "output_accounts": s.output_accounts, "input_accounts": s.input_accounts,
            "updated_by": s.updated_by, "updated_at": s.updated_at.isoformat()}


def return_out(r: VatReturn) -> dict:
    return {"id": r.id, "period_start": r.period_start.isoformat(),
            "period_end": r.period_end.isoformat(), "taxable_sales": r.taxable_sales,
            "output_vat": r.output_vat, "input_vat": r.input_vat, "reference": r.reference,
            "created_by": r.created_by}


def suggestions(df: pd.DataFrame) -> dict:
    """The ledger's accounts, and the ones that look like output and input VAT."""
    accounts = sorted(set(text(df, "account")) - {""})
    vat = [a for a in accounts if VAT_WORDS.search(a)]
    return {"accounts": accounts[:2000],
            "output": [a for a in vat if not INPUT_WORDS.search(a)],
            "input": [a for a in vat if INPUT_WORDS.search(a)]}


NO_DATES = ("This ledger has no dates, so it can't be split into VAT periods. Map the Date "
            "column of the file.")


def reconcile(df: pd.DataFrame, e: Engagement, s: VatSetup) -> dict | None:
    if "date" not in df:
        return None
    account = text(df, "account")
    revenue_lines = {i for i, _, sec in statements.LINES if sec == "revenue"}
    is_sales = lines_of(df, e).isin(revenue_lines)
    out_acc, in_acc = account.isin(set(s.output_accounts)), account.isin(set(s.input_accounts))
    day = df["date"].dt.date
    amount = df["amount"].astype(float)
    rate = s.rate_pct / 100
    rows, covered = [], pd.Series(False, index=df.index)
    for r in e.vat_returns:
        within = (day >= r.period_start) & (day <= r.period_end)
        covered |= within
        sales = -amount[within & is_sales].sum()
        output = -amount[within & out_acc].sum()
        input_ = amount[within & in_acc].sum()
        diff = {"sales": r.taxable_sales - sales, "output": r.output_vat - output,
                "input": r.input_vat - input_, "rate": r.output_vat - r.taxable_sales * rate}
        rows.append({**return_out(r), "ledger_sales": _r(sales), "ledger_output": _r(output),
                     "ledger_input": _r(input_), "expected_output": _r(r.taxable_sales * rate),
                     **{f"diff_{k}": _r(v) for k, v in diff.items()},
                     "net_declared": _r(r.output_vat - r.input_vat),
                     "flags": [k for k, v in diff.items() if abs(v) >= TOLERANCE]})
    returns = sorted(e.vat_returns, key=lambda r: r.period_start)
    overlaps = [f"{a.period_start} to {a.period_end}"
                for a, b in zip(returns, returns[1:], strict=False)
                if b.period_start <= a.period_end]
    uncovered = -amount[is_sales & ~covered & day.notna()].sum()
    totals = {k: _r(sum(x[k] for x in rows)) for k in (
        "taxable_sales", "ledger_sales", "output_vat", "ledger_output", "input_vat",
        "ledger_input", "diff_sales", "diff_output", "diff_input", "diff_rate")}
    notes = []
    if not s.output_accounts and not s.input_accounts:
        notes.append("Pick the ledger's VAT accounts to compare the VAT itself.")
    if abs(uncovered) >= TOLERANCE:
        notes.append(f"Sales of {uncovered:,.2f} in the ledger are in no return's period.")
    if overlaps:
        notes.append("Some returns overlap: " + ", ".join(overlaps) + ".")
    return {"rows": rows, "totals": totals, "uncovered_sales": _r(uncovered), "notes": notes,
            "flagged": sum(1 for x in rows if x["flags"])}


def vat_out(session: Session, e: Engagement) -> dict:
    s = e.vat_setup
    gl = ledger_of(e, s)
    out = {"setup": setup_out(s), "returns": [return_out(r) for r in e.vat_returns],
           "ledgers": [{"id": u.id, "filename": u.filename} for u in e.uploads if u.kind == "gl"],
           "suggested": None, "result": None, "problem": None}
    if gl is not None:
        df = load_upload(gl).df
        out["suggested"] = suggestions(df)
        out["result"] = reconcile(df, e, s)
        if out["result"] is None:
            out["problem"] = NO_DATES
    return out


@router.get("/engagements/{engagement_id}/vat")
def get_vat(engagement_id: int, user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    return vat_out(session, get_or_404(session, Engagement, engagement_id, user))


@router.put("/engagements/{engagement_id}/vat")
def save_setup(engagement_id: int, body: SetupIn, user: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"vat:{engagement_id}")
    if body.upload_id is not None and not any(u.id == body.upload_id and u.kind == "gl"
                                              for u in e.uploads):
        raise HTTPException(400, "Pick a general ledger of this engagement.")
    if body.upload_id is not None:
        gl = next(u for u in e.uploads if u.id == body.upload_id)
        if "date" not in load_upload(gl).df:
            raise HTTPException(400, NO_DATES)
    s = e.vat_setup or VatSetup(engagement=e)
    s.upload_id, s.rate_pct = body.upload_id, body.rate_pct
    s.output_accounts = list(dict.fromkeys(body.output_accounts))
    s.input_accounts = list(dict.fromkeys(body.input_accounts))
    s.updated_by, s.updated_at = user.name, utcnow()
    session.add(s)
    log(session, user, "vat.setup", f"Set up the VAT reconciliation (rate {body.rate_pct:g}%)",
        e, details=body.model_dump())
    session.commit()
    return vat_out(session, e)


@router.post("/engagements/{engagement_id}/vat/returns", status_code=201)
def add_return(engagement_id: int, body: ReturnIn, user: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> dict:
    if body.period_end < body.period_start:
        raise HTTPException(400, "The period ends before it starts.")
    e = held_engagement(session, user, engagement_id, f"vat:{engagement_id}")
    if len(e.vat_returns) >= MAX_RETURNS:
        raise HTTPException(400, f"An engagement can have up to {MAX_RETURNS} VAT returns.")
    r = VatReturn(engagement=e, created_by=user.name, **body.model_dump())
    session.add(r)
    session.flush()
    log(session, user, "vat.return_add",
        f"Added the VAT return for {body.period_start} to {body.period_end}", e,
        details={"vat_return_id": r.id, **body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return vat_out(session, e)


@router.delete("/vat-returns/{vat_return_id}")
def delete_return(vat_return_id: int, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    r = get_or_404(session, VatReturn, vat_return_id, user)
    e = held_engagement(session, user, r.engagement_id, f"vat:{r.engagement_id}")
    r = get_or_404(session, VatReturn, vat_return_id, user)
    log(session, user, "vat.return_delete",
        f"Deleted the VAT return for {r.period_start} to {r.period_end}", e,
        details=return_out(r))
    session.delete(r)
    session.commit()
    session.refresh(e)
    return vat_out(session, e)


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    out = vat_out(session, e)
    res = out["result"]
    rows = [{"Period": f"{x['period_start']} to {x['period_end']}", "Reference": x["reference"],
             "Sales declared": x["taxable_sales"], "Sales in the ledger": x["ledger_sales"],
             "Sales difference": x["diff_sales"], "Output VAT declared": x["output_vat"],
             "Output VAT in the ledger": x["ledger_output"],
             "Output VAT difference": x["diff_output"],
             "Output VAT at the rate": x["expected_output"],
             "Declared less at the rate": x["diff_rate"], "Input VAT declared": x["input_vat"],
             "Input VAT in the ledger": x["ledger_input"],
             "Input VAT difference": x["diff_input"], "Net VAT declared": x["net_declared"]}
            for x in (res["rows"] if res else [])]
    s = out["setup"] or {}
    gl = ledger_of(e, e.vat_setup)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Ledger", f"{gl.filename} (SHA-256 {gl.sha256})" if gl else "None picked"),
             ("VAT rate %", s.get("rate_pct")),
             ("Output VAT accounts", ", ".join(s.get("output_accounts", []))),
             ("Input VAT accounts", ", ".join(s.get("input_accounts", []))),
             *([("Sales in no return's period", res["uncovered_sales"])] if res else []),
             *[("Note", n) for n in (res["notes"] if res else [])]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows).to_excel(xl, sheet_name="VAT reconciliation", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-vat-reconciliation')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/vat/export")
def export_vat(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "vat.download", "Downloaded the VAT reconciliation", e)
    session.commit()
    name, content = workbook(session, e)
    return Response(content, media_type=XLSX, headers=attachment(name))
