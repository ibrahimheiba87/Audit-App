"""Expected credit losses on trade receivables (IFRS 9), by a provision matrix, as auditors
build it from the receivables aging (Caseware's and Inflo's receivables work).

The team picks the receivables listing and sets a loss rate for each age band (from the
client's history of bad debts) and, if needed, a forward-looking adjustment. The app ages the
listing at the period end, applies the rates to the debit balances in each band and compares
the allowance this gives with the allowance the client booked. It suggests the booked
allowance from trial balance accounts whose names mention an allowance or bad debts.
Nothing is posted; every change is logged and the matrix downloads to Excel.
"""

from __future__ import annotations

import io
import re
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import aging
from .auth import current_user, log, require
from .db import EclSetup, Engagement, Upload, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, load_upload, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Starting rates only, to be replaced with rates from the client's own loss history.
DEFAULT_RATES = {"current": 1.0, "d1_30": 3.0, "d31_60": 8.0, "d61_90": 15.0,
                 "d91_180": 35.0, "over_180": 70.0, "no_date": 50.0}
ALLOWANCE_WORDS = re.compile(
    r"allowance|doubtful|bad debt|impairment|expected credit|\becl\b|provision|مخصص", re.I)

Pct = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class SetupIn(BaseModel):
    upload_id: int | None = None
    terms_days: int = Field(default=30, ge=0, le=aging.MAX_TERMS)
    rates: dict[str, Pct] = Field(default_factory=lambda: dict(DEFAULT_RATES))
    forward_pct: float = Field(default=0.0, ge=-100, le=500, allow_inf_nan=False)
    booked: float | None = Field(default=None, ge=-1e13, le=1e13, allow_inf_nan=False)
    note: str = Field(default="", max_length=5000)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def listing_of(e: Engagement, s: EclSetup | None) -> Upload | None:
    if s is None or s.upload_id is None:
        return None
    return next((u for u in e.uploads if u.id == s.upload_id and u.kind == "ar"), None)


def setup_out(s: EclSetup | None) -> dict | None:
    if s is None:
        return None
    return {"upload_id": s.upload_id, "terms_days": s.terms_days,
            "rates": {**DEFAULT_RATES, **(s.rates or {})}, "forward_pct": s.forward_pct,
            "booked": s.booked, "note": s.note, "updated_by": s.updated_by,
            "updated_at": s.updated_at.isoformat()}


def booked_from_tb(session: Session, e: Engagement) -> dict | None:
    """Trial balance accounts that look like the allowance for bad debts, and their total
    as a positive allowance (they are credit balances)."""
    from .leadsheets import compute  # noqa: PLC0415 (leadsheets imports much of the app)
    try:
        data, tb, _ = compute(session, e, None, None)
    except HTTPException:
        return None
    if tb is None:
        return None
    accounts = [a for a in data.get("accounts", [])
                if a["line"] in ("receivables", None)
                and ALLOWANCE_WORDS.search(f"{a['account']} {a['name'] or ''}")]
    if not accounts:
        return None
    return {"tb": tb.filename, "amount": _r(-sum(a["adjusted"] for a in accounts)),
            "accounts": [{"account": a["account"], "name": a["name"],
                          "balance": a["adjusted"]} for a in accounts]}


def matrix(df: pd.DataFrame, e: Engagement, s: EclSetup) -> dict:
    result = aging.age(df, "ar", e.period_end, s.terms_days, with_lines=True)
    rates = {**DEFAULT_RATES, **(s.rates or {})}
    uplift = 1 + s.forward_pct / 100
    lines = result["lines"]
    rows = []
    for key, label, _, _ in aging.BUCKETS:
        items = [x for x in lines if x["bucket"] == key]
        debit = sum(x["balance"] for x in items if x["balance"] > 0)
        rate = min(100.0, rates[key] * uplift)
        rows.append({"id": key, "label": label, "items": len(items), "debit": _r(debit),
                     "credit": _r(sum(x["balance"] for x in items if x["balance"] < 0)),
                     "base_rate": rates[key], "rate": round(rate, 4),
                     "allowance": _r(debit * rate / 100)})
    expected = _r(sum(r["allowance"] for r in rows))
    debit_total = _r(sum(r["debit"] for r in rows))
    out = {"rows": rows, "expected": expected, "debit_total": debit_total,
           "coverage_pct": round(100 * expected / debit_total, 2) if debit_total else None,
           "as_of": result["as_of"], "basis": result["basis"],
           "difference": None, "level": None, "notes": []}
    if s.booked is not None:
        diff = _r(s.booked - expected)
        out["difference"] = diff
        overall = e.overall_materiality
        trivial = max(1.0, e.trivial_threshold or 0.0)
        if overall and abs(diff) >= overall:
            out["level"] = "material"
        elif abs(diff) > trivial:
            out["level"] = "above_trivial"
        else:
            out["level"] = "fine"
    if rates == DEFAULT_RATES:
        out["notes"].append("These are the app's starting rates. Replace them with rates from "
                            "the client's own history of bad debts.")
    if any(r["credit"] for r in rows):
        out["notes"].append("Credit balances are left out of the allowance.")
    return out


def ecl_out(session: Session, e: Engagement) -> dict:
    s = e.ecl_setup
    listing = listing_of(e, s)
    out = {"setup": setup_out(s),
           "listings": [{"id": u.id, "filename": u.filename} for u in e.uploads
                        if u.kind == "ar"],
           "buckets": [{"id": b[0], "label": b[1]} for b in aging.BUCKETS],
           "defaults": DEFAULT_RATES, "suggested_booked": booked_from_tb(session, e),
           "result": None, "problem": None}
    if listing is not None:
        try:
            out["result"] = matrix(load_upload(listing).df, e, s)
        except aging.AgingError as exc:
            out["problem"] = str(exc)
    return out


@router.get("/engagements/{engagement_id}/ecl")
def get_ecl(engagement_id: int, user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    return ecl_out(session, get_or_404(session, Engagement, engagement_id, user))


@router.put("/engagements/{engagement_id}/ecl")
def save_ecl(engagement_id: int, body: SetupIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    unknown = set(body.rates) - set(DEFAULT_RATES)
    if unknown:
        raise HTTPException(400, "Give a loss rate for the app's age bands only.")
    e = held_engagement(session, user, engagement_id, f"ecl:{engagement_id}")
    if body.upload_id is not None and not any(u.id == body.upload_id and u.kind == "ar"
                                              for u in e.uploads):
        raise HTTPException(400, "Pick a receivables listing of this engagement.")
    before = setup_out(e.ecl_setup)
    s = e.ecl_setup or EclSetup(engagement=e)
    s.upload_id, s.terms_days = body.upload_id, body.terms_days
    s.rates = {**DEFAULT_RATES, **body.rates}
    s.forward_pct, s.booked, s.note = body.forward_pct, body.booked, body.note
    s.updated_by, s.updated_at = user.name, utcnow()
    session.add(s)
    log(session, user, "ecl.setup", "Set up the expected credit loss matrix", e,
        details={"before": before, "after": body.model_dump()})
    session.commit()
    return ecl_out(session, e)


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    out = ecl_out(session, e)
    s, res = out["setup"] or {}, out["result"]
    listing = listing_of(e, e.ecl_setup)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Receivables listing",
              f"{listing.filename} (SHA-256 {listing.sha256})" if listing else "None picked"),
             ("Aged at", res["as_of"] if res else e.period_end.isoformat()),
             ("Credit terms (days)", s.get("terms_days")),
             ("Forward-looking adjustment %", s.get("forward_pct")),
             ("Expected allowance", res["expected"] if res else None),
             ("Allowance booked", s.get("booked")),
             ("Difference (booked less expected)", res["difference"] if res else None),
             ("Note", s.get("note", "")),
             *[("To note", n) for n in (res["notes"] if res else [])]]
    rows = [{"Age band": r["label"], "Items": r["items"], "Debit balances": r["debit"],
             "Credit balances (left out)": r["credit"], "Loss rate %": r["base_rate"],
             "Rate used %": r["rate"], "Allowance": r["allowance"]}
            for r in (res["rows"] if res else [])]
    tb = out["suggested_booked"]
    accounts = [{"Account": a["account"], "Name": a["name"], "Balance": a["balance"]}
                for a in (tb["accounts"] if tb else [])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows).to_excel(xl, sheet_name="Provision matrix", index=False)
        pd.DataFrame(accounts, columns=["Account", "Name", "Balance"]).to_excel(
            xl, sheet_name="Allowance accounts", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-expected-credit-losses')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/ecl/export")
def export_ecl(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "ecl.download", "Downloaded the expected credit loss matrix", e)
    session.commit()
    name, content = workbook(session, e)
    return Response(content, media_type=XLSX, headers=attachment(name))
