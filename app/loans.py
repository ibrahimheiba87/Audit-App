"""Loans and covenants, as auditors keep them on a borrowings lead schedule (Caseware's
loan schedules, TeamMate's debt work).

Each loan is entered with its balance at the start of the year, what was drawn and repaid,
the closing balance the client shows (or the lender confirmed), the rate, the interest the
client booked, the part shown as current and the maturity date. The app
- rolls the balance forward and compares it with the client's closing balance;
- works out the interest the loan should carry (the average balance at the rate) and
  compares it with the interest booked;
- flags a loan due within a year that isn't shown as all current;
- compares the loans in total with the borrowings in the trial balance.

Covenants are tested on the draft statements: current ratio, debt to equity, gearing,
interest cover, debt to EBITDA and net worth, or any other figure the team works out and
types in. A breach is flagged with what it means for the audit (classification of the loan
and going concern). Nothing is posted; every change is logged and the work downloads to
Excel.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import ratios
from .auth import current_user, log, require
from .db import Covenant, Engagement, Loan, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook
from .leases import add_months, threshold

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MAX_LOANS = 100
MAX_COVENANTS = 100
INTEREST_TOLERANCE_PCT = 10  # booked interest this far from the expected is flagged

METRICS = {
    "current_ratio": ("Current ratio", "x"),
    "debt_to_equity": ("Debt to equity", "x"),
    "gearing": ("Gearing (debt ÷ debt and equity)", "%"),
    "interest_cover": ("Interest cover", "x"),
    "debt_to_ebitda": ("Debt to EBITDA", "x"),
    "net_worth": ("Net worth (total equity)", "amount"),
    "other": ("Other (worked out by the team)", ""),
}
OPERATORS = {"min": "At least", "max": "At most"}

Money = Annotated[float, Field(ge=-1e13, le=1e13, allow_inf_nan=False)]
Positive = Annotated[float, Field(ge=0, le=1e13, allow_inf_nan=False)]


class LoanIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    lender: str = Field(default="", max_length=200)
    opening: Money = 0.0
    drawn: Positive = 0.0
    repaid: Positive = 0.0
    client_closing: Money | None = None
    rate_pct: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)
    client_interest: Money | None = None
    client_current: Money | None = None
    maturity: date | None = None
    security: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)


class CovenantIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    loan_id: int | None = None
    metric: str
    operator: str = "min"
    limit: Money
    actual: Money | None = None  # only for "other"
    note: str = Field(default="", max_length=2000)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def loan_out(x: Loan, e: Engagement) -> dict:
    limit = threshold(e)
    closing = x.opening + x.drawn - x.repaid
    expected_interest = (x.opening + closing) / 2 * x.rate_pct / 100
    flags, notes = [], []
    diff_closing = diff_interest = None
    if x.client_closing is not None:
        diff_closing = _r(x.client_closing - closing)
        if abs(diff_closing) > limit:
            flags.append("closing")
    if x.client_interest is not None:
        diff_interest = _r(x.client_interest - expected_interest)
        off = abs(diff_interest)
        if off > limit and off > abs(expected_interest) * INTEREST_TOLERANCE_PCT / 100:
            flags.append("interest")
    balance = x.client_closing if x.client_closing is not None else closing
    due_soon = x.maturity is not None and x.maturity <= add_months(e.period_end, 12)
    if due_soon and (x.client_current or 0.0) < balance - limit:
        flags.append("current")
        notes.append("It is due within a year of the period end, so all of it is current.")
    if x.client_current is not None and x.client_current > balance + limit:
        flags.append("current")
        notes.append("More is shown as current than the balance.")
    if x.maturity is not None and x.maturity <= e.period_end and abs(balance) > limit:
        notes.append("It was due by the period end but is still outstanding.")
    return {"id": x.id, "name": x.name, "lender": x.lender, "opening": x.opening,
            "drawn": x.drawn, "repaid": x.repaid, "closing": _r(closing),
            "client_closing": x.client_closing, "diff_closing": diff_closing,
            "rate_pct": x.rate_pct, "expected_interest": _r(expected_interest),
            "client_interest": x.client_interest, "diff_interest": diff_interest,
            "client_current": x.client_current,
            "maturity": x.maturity.isoformat() if x.maturity else None,
            "due_within_year": due_soon, "security": x.security, "note": x.note,
            "flags": flags, "notes": notes, "created_by": x.created_by,
            "updated_by": x.updated_by}


def figures(session: Session, e: Engagement) -> dict | None:
    """The draft statements' totals the covenants and the borrowings tie-in use."""
    from .leadsheets import compute  # noqa: PLC0415 (leadsheets imports much of the app)
    try:
        data, tb, _ = compute(session, e, None, None)
    except HTTPException:
        return None
    if tb is None or not data.get("lead_sheets"):
        return None
    return ratios._figures(data, "adjusted")


def metric_value(metric: str, f: dict | None) -> float | None:
    if f is None:
        return None
    debt, equity = f["debt"], f["total_equity"]
    ebitda = f["operating_profit"] + f.get("depreciation", 0.0)
    value = {
        "current_ratio": lambda: ratios._div(f.get("current_assets", 0),
                                             f.get("current_liabilities", 0)),
        "debt_to_equity": lambda: ratios._div(debt, equity) if equity > 0 else None,
        "gearing": lambda: ratios._div(debt, debt + equity, 100) if debt + equity > 0 else None,
        "interest_cover": lambda: ratios._div(f["operating_profit"], f.get("finance_costs", 0)),
        "debt_to_ebitda": lambda: ratios._div(debt, ebitda) if ebitda > 0 else None,
        "net_worth": lambda: equity,
    }.get(metric, lambda: None)()
    return None if value is None else round(value, 4)


def covenant_out(c: Covenant, f: dict | None) -> dict:
    actual = c.actual if c.metric == "other" else metric_value(c.metric, f)
    status = "unknown"
    headroom = None
    if actual is not None:
        ok = actual >= c.limit if c.operator == "min" else actual <= c.limit
        status = "met" if ok else "breached"
        headroom = round(actual - c.limit if c.operator == "min" else c.limit - actual, 4)
        if ok and c.limit and abs(headroom) <= abs(c.limit) * 0.1:
            status = "close"
    label, unit = METRICS[c.metric]
    return {"id": c.id, "title": c.title, "loan_id": c.loan_id,
            "loan": c.loan.name if c.loan else None, "metric": c.metric,
            "metric_label": label, "unit": unit, "operator": c.operator,
            "operator_label": OPERATORS[c.operator], "limit": c.limit,
            "actual": actual, "headroom": headroom, "status": status, "note": c.note,
            "created_by": c.created_by}


STATUS_LABELS = {"met": "Met", "close": "Met, with little headroom", "breached": "Breached",
                 "unknown": "Not worked out yet"}


def loans_out(session: Session, e: Engagement) -> dict:
    loans = [loan_out(x, e) for x in e.loans]
    f = figures(session, e)
    covenants = [covenant_out(c, f) for c in e.covenants]
    totals = {k: _r(sum(x[k] for x in loans)) for k in (
        "opening", "drawn", "repaid", "closing", "expected_interest")}
    with_client = [x for x in loans if x["client_closing"] is not None]
    totals["client_closing"] = (_r(sum(x["client_closing"] for x in with_client))
                                if with_client else None)
    booked = [x for x in loans if x["client_interest"] is not None]
    totals["client_interest"] = (_r(sum(x["client_interest"] for x in booked))
                                 if booked else None)
    tb = None
    if f is not None:
        balance = totals["client_closing"] if totals["client_closing"] is not None \
            else totals["closing"]
        tb = {"borrowings": _r(f["debt"]),
              "difference": _r(balance - f["debt"]) if loans else None,
              "finance_costs": _r(f.get("finance_costs", 0.0))}
    notes = []
    if any(c["status"] == "breached" for c in covenants):
        notes.append("A covenant is breached. Check whether the lender can ask for the loan "
                     "back: if so, it is current at the period end (IAS 1), and the breach "
                     "bears on going concern.")
    if tb and tb["difference"] is not None and abs(tb["difference"]) > threshold(e):
        notes.append("The loans don't add up to the borrowings in the trial balance.")
    return {"loans": loans, "covenants": covenants, "totals": totals, "tb": tb,
            "notes": notes, "flagged": sum(1 for x in loans if x["flags"]),
            "breached": sum(1 for c in covenants if c["status"] == "breached"),
            "metrics": [{"id": k, "label": v[0], "unit": v[1]} for k, v in METRICS.items()],
            "operators": [{"id": k, "label": v} for k, v in OPERATORS.items()],
            "status_labels": STATUS_LABELS}


@router.get("/engagements/{engagement_id}/loans")
def get_loans(engagement_id: int, user: User = Depends(current_user),
              session: Session = Depends(get_session)) -> dict:
    return loans_out(session, get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/loans", status_code=201)
def add_loan(engagement_id: int, body: LoanIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"loans:{engagement_id}")
    if len(e.loans) >= MAX_LOANS:
        raise HTTPException(400, f"An engagement can have up to {MAX_LOANS} loans.")
    x = Loan(engagement=e, created_by=user.name, updated_by=user.name,
             **{**body.model_dump(), "name": body.name.strip()})
    session.add(x)
    session.flush()
    log(session, user, "loan.create", f"Added the loan {x.name}", e,
        details={"loan_id": x.id, **body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return loans_out(session, e)


def held(session: Session, user: User, model, id_: int):
    x = get_or_404(session, model, id_, user)
    e = held_engagement(session, user, x.engagement_id, f"loans:{x.engagement_id}")
    return get_or_404(session, model, id_, user), e


@router.put("/loans/{loan_id}")
def update_loan(loan_id: int, body: LoanIn, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    x, e = held(session, user, Loan, loan_id)
    before = {k: getattr(x, k) for k in LoanIn.model_fields}
    for k, v in body.model_dump().items():
        setattr(x, k, v.strip() if k == "name" else v)
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "loan.update", f"Changed the loan {x.name}", e,
        details={"loan_id": x.id, "before": {k: v.isoformat() if isinstance(v, date) else v
                                             for k, v in before.items()},
                 "after": body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return loans_out(session, e)


@router.delete("/loans/{loan_id}")
def delete_loan(loan_id: int, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    x, e = held(session, user, Loan, loan_id)
    log(session, user, "loan.delete", f"Deleted the loan {x.name}", e,
        details={"loan_id": x.id, "name": x.name, "opening": x.opening})
    for c in list(e.covenants):  # its covenants stay, tested on the whole company
        if c.loan_id == x.id:
            c.loan_id = None
    session.delete(x)
    session.commit()
    session.refresh(e)
    return loans_out(session, e)


def check_covenant(body: CovenantIn, e: Engagement) -> None:
    if body.metric not in METRICS:
        raise HTTPException(400, "Pick what the covenant measures.")
    if body.operator not in OPERATORS:
        raise HTTPException(400, "Pick whether the figure must be at least or at most the "
                                 "limit.")
    if body.loan_id is not None and not any(x.id == body.loan_id for x in e.loans):
        raise HTTPException(400, "Pick a loan of this engagement.")


@router.post("/engagements/{engagement_id}/covenants", status_code=201)
def add_covenant(engagement_id: int, body: CovenantIn, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"loans:{engagement_id}")
    check_covenant(body, e)
    if len(e.covenants) >= MAX_COVENANTS:
        raise HTTPException(400, f"An engagement can have up to {MAX_COVENANTS} covenants.")
    c = Covenant(engagement=e, created_by=user.name,
                 **{**body.model_dump(), "title": body.title.strip(),
                    "actual": body.actual if body.metric == "other" else None})
    session.add(c)
    session.flush()
    log(session, user, "covenant.create", f"Added the covenant {c.title}", e,
        details={"covenant_id": c.id, **body.model_dump()})
    session.commit()
    session.refresh(e)
    return loans_out(session, e)


@router.put("/covenants/{covenant_id}")
def update_covenant(covenant_id: int, body: CovenantIn, user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    c, e = held(session, user, Covenant, covenant_id)
    check_covenant(body, e)
    for k, v in body.model_dump().items():
        setattr(c, k, v.strip() if k == "title" else v)
    if body.metric != "other":
        c.actual = None
    log(session, user, "covenant.update", f"Changed the covenant {c.title}", e,
        details={"covenant_id": c.id, **body.model_dump()})
    session.commit()
    session.refresh(e)
    return loans_out(session, e)


@router.delete("/covenants/{covenant_id}")
def delete_covenant(covenant_id: int, user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    c, e = held(session, user, Covenant, covenant_id)
    log(session, user, "covenant.delete", f"Deleted the covenant {c.title}", e,
        details={"covenant_id": c.id, "title": c.title})
    session.delete(c)
    session.commit()
    session.refresh(e)
    return loans_out(session, e)


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    out = loans_out(session, e)
    t, tb = out["totals"], out["tb"] or {}
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Loans", len(out["loans"])),
             ("Closing balance (rolled forward)", t["closing"]),
             ("Closing balance (client)", t["client_closing"]),
             ("Borrowings in the trial balance", tb.get("borrowings")),
             ("Difference", tb.get("difference")),
             ("Expected interest", t["expected_interest"]),
             ("Interest booked", t["client_interest"]),
             ("Finance costs in the trial balance", tb.get("finance_costs")),
             *[("To note", n) for n in out["notes"]]]
    loans = [{"Loan": x["name"], "Lender": x["lender"], "Opening": x["opening"],
              "Drawn": x["drawn"], "Repaid": x["repaid"], "Closing (rolled forward)": x["closing"],
              "Closing (client)": x["client_closing"], "Difference": x["diff_closing"],
              "Rate %": x["rate_pct"], "Expected interest": x["expected_interest"],
              "Interest booked": x["client_interest"], "Interest difference": x["diff_interest"],
              "Shown as current": x["client_current"], "Matures": x["maturity"],
              "Security": x["security"], "To note": " ".join(x["notes"]), "Note": x["note"]}
             for x in out["loans"]]
    covenants = [{"Covenant": c["title"], "Loan": c["loan"] or "", "Measure": c["metric_label"],
                  "Rule": c["operator_label"], "Limit": c["limit"], "Actual": c["actual"],
                  "Headroom": c["headroom"], "Result": STATUS_LABELS[c["status"]],
                  "Note": c["note"]} for c in out["covenants"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(loans).to_excel(xl, sheet_name="Loans", index=False)
        pd.DataFrame(covenants).to_excel(xl, sheet_name="Covenants", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-loans-and-covenants')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/loans/export")
def export_loans(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "loan.download", "Downloaded the loans and covenants", e)
    session.commit()
    name, content = workbook(session, e)
    return Response(content, media_type=XLSX, headers=attachment(name))
