"""Fees, billing and recoverability, as practice management tools beside Caseware and
TeamMate show them: what each engagement's time is worth at the firm's hourly rates, what has
been billed and paid, and how much of the time the fee recovers. A firm-wide WIP page lists
every engagement the partner can see.

The firm sets an hourly rate for each staff role. Time is valued at the rate for the role
each person has now. The agreed fee and whether it is fixed or hourly come from the
engagement letter (planning.py). Invoices are recorded by hand with their date, amount and
when they were paid. Fees are the firm's business, not the audit file's, so only partners
and admins see them, they stay out of the audit file pack, and invoices can be recorded on a
locked engagement.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth
from .auth import log, require
from .db import (
    ChargeRate,
    Client,
    Engagement,
    FeeInvoice,
    User,
    get_session,
    utcnow,
)
from .engagements import assigned_ids, get_or_404, held_engagement, safe_stem, visible_engagements
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RATE_ROLES = [r for r in auth.STAFF_ROLES]
MAX_INVOICES = 200
OVERDUE_DAYS = 30
LOW_RECOVERY_PCT = 80.0

Money = Annotated[float, Field(gt=-1e13, lt=1e13, allow_inf_nan=False)]


class RatesIn(BaseModel):
    # A blank rate (null) clears it.
    rates: dict[str, Annotated[float, Field(ge=0, le=1e7, allow_inf_nan=False)] | None]


class InvoiceIn(BaseModel):
    number: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] = ""
    issued_on: date
    amount: Money
    paid_on: date | None = None
    note: str = Field(default="", max_length=2000)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def rates_of(session: Session) -> dict[str, float]:
    return {r.role: r.rate for r in session.scalars(select(ChargeRate))}


def invoice_out(x: FeeInvoice, today: date) -> dict:
    overdue = x.paid_on is None and x.amount > 0 and x.issued_on + timedelta(days=OVERDUE_DAYS) < today
    return {"id": x.id, "number": x.number, "issued_on": x.issued_on.isoformat(),
            "amount": x.amount, "paid_on": x.paid_on.isoformat() if x.paid_on else None,
            "note": x.note, "overdue": overdue, "created_by": x.created_by}


def summary(session: Session, e: Engagement, rates: dict[str, float],
            roles: dict[int, str], today: date) -> dict:
    hours = value = unpriced = 0.0
    for t in e.time_entries:
        role = roles.get(t.user_id) if t.user_id is not None else None
        hours += t.hours
        if role in rates:
            value += t.hours * rates[role]
        else:
            unpriced += t.hours
    plan = e.plan
    fee = plan.fee if plan else None
    basis = plan.fee_basis if plan else "fixed"
    invoices = [invoice_out(x, today) for x in e.invoices]
    billed = sum(x["amount"] for x in invoices)
    paid = sum(x["amount"] for x in invoices if x["paid_on"])
    budget = sum(b.hours for b in e.time_budgets)
    # What's still to bill: the rest of a fixed fee, or time not yet billed on an hourly fee.
    # On an hourly fee, recovery means anything until billing starts: time not yet billed
    # isn't lost, so it's shown once there's an invoice.
    if basis == "hourly":
        to_bill = max(0.0, value - billed)
        recovered = billed if invoices else None
    else:
        to_bill = max(0.0, (fee or 0.0) - billed)
        recovered = fee if fee is not None else billed
    recovery = round(100 * recovered / value, 1) if value and recovered is not None else None
    flags = []
    if recovery is not None and recovery < LOW_RECOVERY_PCT:
        flags.append("low_recovery")
    if budget and hours > budget:
        flags.append("over_budget")
    if any(x["overdue"] for x in invoices):
        flags.append("overdue")
    return {"engagement_id": e.id, "client": e.client.name, "engagement": e.name,
            "period_end": e.period_end.isoformat(), "fee": fee, "fee_basis": basis,
            "currency": plan.currency if plan else "", "hours": round(hours, 2),
            "budget_hours": round(budget, 2), "unpriced_hours": round(unpriced, 2),
            "time_value": _r(value), "billed": _r(billed), "paid": _r(paid),
            "outstanding": _r(billed - paid), "to_bill": _r(to_bill),
            "recovery_pct": recovery, "flags": flags, "invoices": invoices}


def roles_of(session: Session, engagements: list[Engagement]) -> dict[int, str]:
    ids = {t.user_id for e in engagements for t in e.time_entries if t.user_id is not None}
    if not ids:
        return {}
    return {u.id: u.role for u in session.scalars(select(User).where(User.id.in_(ids)))}


def fees_out(session: Session, e: Engagement) -> dict:
    rates = rates_of(session)
    out = summary(session, e, rates, roles_of(session, [e]), date.today())
    notes = []
    if not rates:
        notes.append("The firm has no hourly rates yet, so time has no value. Set them on the "
                     "WIP page.")
    elif out["unpriced_hours"]:
        notes.append("Some hours have no rate: the person has no role with a rate, or has "
                     "been removed.")
    if out["fee"] is None:
        notes.append("No fee is recorded. Enter it in the engagement letter.")
    return {**out, "notes": notes, "rates": rates}


@router.get("/engagements/{engagement_id}/fees")
def get_fees(engagement_id: int, user: User = Depends(require("manage")),
             session: Session = Depends(get_session)) -> dict:
    return fees_out(session, get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/invoices", status_code=201)
def add_invoice(engagement_id: int, body: InvoiceIn, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"invoices:{engagement_id}")
    if len(e.invoices) >= MAX_INVOICES:
        raise HTTPException(400, f"An engagement can have up to {MAX_INVOICES} invoices.")
    if body.paid_on is not None and body.paid_on < body.issued_on:
        raise HTTPException(400, "An invoice can't be paid before it was issued.")
    x = FeeInvoice(engagement=e, created_by=user.name, **body.model_dump())
    session.add(x)
    session.flush()
    log(session, user, "invoice.create", f"Recorded the invoice {x.number or x.id} for "
        f"{x.amount:,.2f} on {e.client.name} · {e.name}",
        details={"engagement_id": e.id, "invoice_id": x.id, **body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return fees_out(session, e)


def held_invoice(session: Session, user: User, invoice_id: int):
    x = get_or_404(session, FeeInvoice, invoice_id, user)
    e = held_engagement(session, user, x.engagement_id, f"invoices:{x.engagement_id}")
    return get_or_404(session, FeeInvoice, invoice_id, user), e


@router.put("/invoices/{invoice_id}")
def update_invoice(invoice_id: int, body: InvoiceIn, user: User = Depends(require("manage")),
                   session: Session = Depends(get_session)) -> dict:
    if body.paid_on is not None and body.paid_on < body.issued_on:
        raise HTTPException(400, "An invoice can't be paid before it was issued.")
    x, e = held_invoice(session, user, invoice_id)
    before = invoice_out(x, date.today())
    for k, v in body.model_dump().items():
        setattr(x, k, v)
    log(session, user, "invoice.update", f"Changed the invoice {x.number or x.id} on "
        f"{e.client.name} · {e.name}",
        details={"engagement_id": e.id, "invoice_id": x.id, "before": before,
                 "after": body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return fees_out(session, e)


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, user: User = Depends(require("manage")),
                   session: Session = Depends(get_session)) -> dict:
    x, e = held_invoice(session, user, invoice_id)
    log(session, user, "invoice.delete", f"Deleted the invoice {x.number or x.id} on "
        f"{e.client.name} · {e.name}",
        details={"engagement_id": e.id, **invoice_out(x, date.today()), "invoice_id": x.id})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return fees_out(session, e)


# --- The firm ------------------------------------------------------------------------


@router.get("/fees/rates")
def get_rates(_: User = Depends(require("manage")),
              session: Session = Depends(get_session)) -> dict:
    rates = rates_of(session)
    return {"rates": [{"role": r, "label": auth.ROLES[r], "rate": rates.get(r)}
                      for r in RATE_ROLES]}


@router.put("/fees/rates")
def set_rates(body: RatesIn, user: User = Depends(require("manage")),
              session: Session = Depends(get_session)) -> dict:
    if set(body.rates) - set(RATE_ROLES):
        raise HTTPException(400, "Give rates for staff roles only.")
    auth.hold(session, "charge-rates")
    before = rates_of(session)
    for role, rate in body.rates.items():
        row = session.get(ChargeRate, role)
        if rate is None:
            if row is not None:
                session.delete(row)
            continue
        row = row or ChargeRate(role=role)
        row.rate, row.updated_by, row.updated_at = rate, user.name, utcnow()
        session.add(row)
    log(session, user, "fees.rates", "Set the firm's hourly rates",
        details={"before": before, "after": body.rates})
    session.commit()
    return get_rates(user, session)


def wip_rows(session: Session, user: User, archived: bool) -> list[dict]:
    engagements = list(session.scalars(select(Engagement).join(Client)
                                       .order_by(Client.name, Engagement.period_end)))
    seen = [e for e in visible_engagements(session, user, engagements,
                                           assigned_ids(session, user))
            if archived or not (e.lock and e.lock.archived)]
    rates, today = rates_of(session), date.today()
    roles = roles_of(session, seen)
    return [summary(session, e, rates, roles, today) for e in seen]


def wip_out(rows: list[dict]) -> dict:
    totals = {k: _r(sum(r[k] for r in rows))
              for k in ("time_value", "billed", "paid", "outstanding", "to_bill")}
    totals["hours"] = round(sum(r["hours"] for r in rows), 2)
    return {"rows": [{k: v for k, v in r.items() if k != "invoices"} for r in rows],
            "totals": totals, "low_recovery": sum(1 for r in rows if "low_recovery" in r["flags"]),
            "overdue": sum(1 for r in rows if "overdue" in r["flags"]),
            "currencies": sorted({r["currency"] for r in rows if r["currency"]})}


@router.get("/wip")
def wip(archived: bool = False, user: User = Depends(require("manage")),
        session: Session = Depends(get_session)) -> dict:
    return wip_out(wip_rows(session, user, archived))


@router.get("/wip/export")
def export_wip(archived: bool = False, user: User = Depends(require("manage")),
               session: Session = Depends(get_session)) -> Response:
    out = wip_out(wip_rows(session, user, archived))
    log(session, user, "fees.download", "Downloaded the firm's WIP and fees")
    session.commit()
    rows = [{"Client": r["client"], "Engagement": r["engagement"], "Period end": r["period_end"],
             "Fee": r["fee"], "Basis": "Hourly" if r["fee_basis"] == "hourly" else "Fixed",
             "Currency": r["currency"], "Hours": r["hours"], "Budget hours": r["budget_hours"],
             "Time value": r["time_value"], "Billed": r["billed"], "Paid": r["paid"],
             "Outstanding": r["outstanding"], "Still to bill": r["to_bill"],
             "Recovery %": r["recovery_pct"]} for r in out["rows"]]
    cols = ["Client", "Engagement", "Period end", "Fee", "Basis", "Currency", "Hours",
            "Budget hours", "Time value", "Billed", "Paid", "Outstanding", "Still to bill",
            "Recovery %"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame([("As of", date.today().isoformat()),
                      ("Archived engagements", "Included" if archived else "Left out"),
                      ("Hours", out["totals"]["hours"]),
                      ("Time value", out["totals"]["time_value"]),
                      ("Billed", out["totals"]["billed"]),
                      ("Outstanding", out["totals"]["outstanding"]),
                      ("Still to bill", out["totals"]["to_bill"])],
                     columns=["Item", "Value"]).to_excel(xl, sheet_name="About", index=False)
        pd.DataFrame(rows, columns=cols).to_excel(xl, sheet_name="Engagements", index=False)
        finish_workbook(xl.book)
    name = safe_stem(f"wip-and-fees-{date.today().isoformat()}")
    return Response(buf.getvalue(), media_type=XLSX, headers=attachment(f"{name}.xlsx"))
