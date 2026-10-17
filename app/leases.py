"""Lease calculator (IFRS 16), as in Caseware's lease schedules and the auditors' own
recalculation spreadsheets.

For each lease the team enters the commencement date, the term, the payment and how often
it falls due (in advance or in arrears), the discount rate and any initial direct costs.
The app works out, at the period end:
- the lease liability: the payments left, discounted, plus the interest accrued to the day;
- the part of it due within a year (current) and the rest (non-current);
- the right-of-use asset: the first liability plus the direct costs, depreciated straight
  line over the term;
- the interest, depreciation and payments that fall in the year.
When the client's own liability and asset are entered, the differences are shown, and those
above the trivial threshold are flagged. Nothing is posted; every change is logged and the
calculation, with each lease's full schedule, downloads to Excel.
"""

from __future__ import annotations

import calendar
import io
from datetime import date, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import current_user, log, require
from .db import Engagement, Lease, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MAX_LEASES = 200
FREQUENCIES = {"monthly": ("Monthly", 1), "quarterly": ("Quarterly", 3),
               "annual": ("Yearly", 12)}
TIMINGS = {"advance": "In advance", "arrears": "In arrears"}
SHORT_TERM_MONTHS = 12


class LeaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    start_date: date
    term_months: int = Field(ge=1, le=1200)
    payment: float = Field(gt=0, le=1e12, allow_inf_nan=False)
    frequency: str = "monthly"
    timing: str = "arrears"
    rate_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    direct_costs: float = Field(default=0.0, ge=0, le=1e12, allow_inf_nan=False)
    client_liability: float | None = Field(default=None, ge=-1e13, le=1e13, allow_inf_nan=False)
    client_rou: float | None = Field(default=None, ge=-1e13, le=1e13, allow_inf_nan=False)
    note: str = Field(default="", max_length=2000)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def add_months(d: date, months: int) -> date:
    """`d` moved on by whole months; a day past the month's end becomes its last day."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)  # not reached: every month has a 28th


def shift_years(d: date, years: int) -> date:
    """`d` moved by whole years; a month end stays a month end (28 Feb to 29 Feb)."""
    last = calendar.monthrange(d.year, d.month)[1]
    target = calendar.monthrange(d.year + years, d.month)[1]
    return date(d.year + years, d.month, target if d.day == last else min(d.day, target))


def check(body: LeaseIn) -> None:
    if body.frequency not in FREQUENCIES:
        raise HTTPException(400, "Pick how often the payment falls due.")
    if body.timing not in TIMINGS:
        raise HTTPException(400, "Pick whether the payment is in advance or in arrears.")
    step = FREQUENCIES[body.frequency][1]
    if body.term_months % step:
        raise HTTPException(400, "The term must be a whole number of payment periods.")


def schedule(x: Lease) -> list[dict]:
    """The lease period by period: dates, the liability before and after, interest, payment."""
    step = FREQUENCIES[x.frequency][1]
    n = x.term_months // step
    r = x.rate_pct / 100 * step / 12
    pay = x.payment
    if r:
        pv = pay * (1 - (1 + r) ** -n) / r
    else:
        pv = pay * n
    if x.timing == "advance":
        pv *= 1 + r
    rows, liability = [], pv
    for k in range(n):
        start, end = add_months(x.start_date, k * step), add_months(x.start_date, (k + 1) * step)
        opening = liability
        if x.timing == "advance":
            base = opening - pay
            interest = base * r
            closing = base + interest
            paid_on = start
        else:
            base = opening
            interest = base * r
            closing = base + interest - pay
            paid_on = end - timedelta(days=1)  # the period's last day
        rows.append({"period": k + 1, "start": start, "end": end, "opening": opening,
                     "base": base, "interest": interest, "payment": pay, "paid_on": paid_on,
                     "closing": closing})
        liability = closing
    if rows:  # rounding can leave a hair's breadth at the end
        rows[-1]["closing"] = 0.0
    return rows


def first_liability(rows: list[dict]) -> float:
    return rows[0]["opening"] if rows else 0.0


def liability_at(rows: list[dict], d: date) -> float:
    """The liability at the end of day `d`, with interest accrued to that day."""
    if not rows or d < rows[0]["start"]:
        return 0.0
    for p in rows:
        if p["start"] <= d < p["end"]:
            if d == p["paid_on"] and p["paid_on"] != p["start"]:  # paid in arrears that day
                return p["closing"]
            days = (p["end"] - p["start"]).days
            accrued = p["interest"] * ((d - p["start"]).days + 1) / days
            return p["base"] + accrued
    return 0.0  # the lease has ended


def overlap(start: date, end: date, a: date, b: date) -> int:
    """Days of [start, end) that fall within [a, b] (both days included)."""
    lo, hi = max(start, a), min(end - timedelta(days=1), b)
    return max(0, (hi - lo).days + 1)


def year_of(e: Engagement) -> tuple[date, date]:
    if e.period_start:
        return e.period_start, e.period_end
    return shift_years(e.period_end, -1) + timedelta(days=1), e.period_end


def threshold(e: Engagement) -> float:
    return max(1.0, e.trivial_threshold or 0.0)


def calc(x: Lease, e: Engagement) -> dict:
    rows = schedule(x)
    fy_start, fy_end = year_of(e)
    lease_end = rows[-1]["end"] if rows else x.start_date
    rou_first = first_liability(rows) + (x.direct_costs or 0.0)
    total_days = max(1, (lease_end - x.start_date).days)
    used = min(total_days, max(0, (fy_end - x.start_date).days + 1))
    liability = liability_at(rows, fy_end)
    next_end = shift_years(fy_end, 1)
    current = max(0.0, liability - liability_at(rows, next_end))
    interest = sum(p["interest"] * overlap(p["start"], p["end"], fy_start, fy_end)
                   / (p["end"] - p["start"]).days for p in rows)
    payments = sum(p["payment"] for p in rows if fy_start <= p["paid_on"] <= fy_end)
    out = {
        "end_date": (lease_end - timedelta(days=1)).isoformat(),  # its last day
        "first_liability": _r(first_liability(rows)), "rou_first": _r(rou_first),
        "liability": _r(liability), "current": _r(current),
        "noncurrent": _r(liability - current),
        "rou": _r(rou_first * (1 - used / total_days)) if x.start_date <= fy_end else 0.0,
        "interest": _r(interest),
        "depreciation": _r(rou_first * overlap(x.start_date, lease_end, fy_start, fy_end)
                           / total_days),
        "payments": _r(payments),
        "diff_liability": None, "diff_rou": None, "flags": [],
    }
    limit = threshold(e)
    if x.client_liability is not None:
        out["diff_liability"] = _r(x.client_liability - out["liability"])
        if abs(out["diff_liability"]) > limit:
            out["flags"].append("liability")
    if x.client_rou is not None:
        out["diff_rou"] = _r(x.client_rou - out["rou"])
        if abs(out["diff_rou"]) > limit:
            out["flags"].append("rou")
    notes = []
    if x.term_months <= SHORT_TERM_MONTHS:
        notes.append("A lease of 12 months or less may be expensed as a short-term lease.")
    if x.start_date > fy_end:
        notes.append("It starts after the period end, so there is nothing at the period end.")
    elif lease_end <= fy_start:
        notes.append("It ended before this year.")
    out["notes"] = notes
    return out


def lease_out(x: Lease, e: Engagement) -> dict:
    return {"id": x.id, "name": x.name, "start_date": x.start_date.isoformat(),
            "term_months": x.term_months, "payment": x.payment, "frequency": x.frequency,
            "frequency_label": FREQUENCIES[x.frequency][0], "timing": x.timing,
            "timing_label": TIMINGS[x.timing], "rate_pct": x.rate_pct,
            "direct_costs": x.direct_costs, "client_liability": x.client_liability,
            "client_rou": x.client_rou, "note": x.note, "created_by": x.created_by,
            "updated_by": x.updated_by, **calc(x, e)}


TOTALS = ("liability", "current", "noncurrent", "rou", "interest", "depreciation", "payments")


def leases_out(e: Engagement) -> dict:
    leases = [lease_out(x, e) for x in e.leases]
    totals = {k: _r(sum(x[k] for x in leases)) for k in TOTALS}
    with_client = [x for x in leases if x["client_liability"] is not None]
    totals["client_liability"] = (_r(sum(x["client_liability"] for x in with_client))
                                  if with_client else None)
    with_rou = [x for x in leases if x["client_rou"] is not None]
    totals["client_rou"] = _r(sum(x["client_rou"] for x in with_rou)) if with_rou else None
    fy_start, fy_end = year_of(e)
    return {"leases": leases, "totals": totals, "flagged": sum(1 for x in leases if x["flags"]),
            "year": {"start": fy_start.isoformat(), "end": fy_end.isoformat()},
            "threshold": threshold(e),
            "frequencies": [{"id": k, "label": v[0]} for k, v in FREQUENCIES.items()],
            "timings": [{"id": k, "label": v} for k, v in TIMINGS.items()]}


@router.get("/engagements/{engagement_id}/leases")
def get_leases(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    return leases_out(get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/leases", status_code=201)
def add_lease(engagement_id: int, body: LeaseIn, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    check(body)
    e = held_engagement(session, user, engagement_id, f"leases:{engagement_id}")
    if len(e.leases) >= MAX_LEASES:
        raise HTTPException(400, f"An engagement can have up to {MAX_LEASES} leases.")
    x = Lease(engagement=e, created_by=user.name, updated_by=user.name,
              **{**body.model_dump(), "name": body.name.strip()})
    session.add(x)
    session.flush()
    log(session, user, "lease.create", f"Added the lease {x.name}", e,
        details={"lease_id": x.id, **body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return leases_out(e)


def held_lease(session: Session, user: User, lease_id: int) -> tuple[Lease, Engagement]:
    x = get_or_404(session, Lease, lease_id, user)
    e = held_engagement(session, user, x.engagement_id, f"leases:{x.engagement_id}")
    return get_or_404(session, Lease, lease_id, user), e


@router.put("/leases/{lease_id}")
def update_lease(lease_id: int, body: LeaseIn, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    check(body)
    x, e = held_lease(session, user, lease_id)
    before = {k: getattr(x, k) for k in LeaseIn.model_fields}
    for k, v in body.model_dump().items():
        setattr(x, k, v.strip() if k == "name" else v)
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "lease.update", f"Changed the lease {x.name}", e,
        details={"lease_id": x.id, "before": {k: str(v) if isinstance(v, date) else v
                                              for k, v in before.items()},
                 "after": body.model_dump(mode="json")})
    session.commit()
    session.refresh(e)
    return leases_out(e)


@router.delete("/leases/{lease_id}")
def delete_lease(lease_id: int, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    x, e = held_lease(session, user, lease_id)
    log(session, user, "lease.delete", f"Deleted the lease {x.name}", e,
        details={"lease_id": x.id, "name": x.name, "start_date": x.start_date.isoformat(),
                 "term_months": x.term_months, "payment": x.payment})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return leases_out(e)


def workbook(e: Engagement) -> tuple[str, bytes]:
    out = leases_out(e)
    t = out["totals"]
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Year", f"{out['year']['start']} to {out['year']['end']}"),
             ("Leases", len(out["leases"])), ("Lease liability", t["liability"]),
             ("Due within a year", t["current"]), ("Due after a year", t["noncurrent"]),
             ("Right-of-use assets", t["rou"]), ("Interest for the year", t["interest"]),
             ("Depreciation for the year", t["depreciation"]),
             ("Payments in the year", t["payments"]),
             ("Client's lease liability", t["client_liability"]),
             ("Client's right-of-use assets", t["client_rou"]),
             ("Differences flagged above", out["threshold"])]
    summary = [{"Lease": x["name"], "Starts": x["start_date"], "Ends": x["end_date"],
                "Term (months)": x["term_months"], "Payment": x["payment"],
                "Paid": f"{x['frequency_label']}, {x['timing_label'].lower()}",
                "Rate %": x["rate_pct"], "Direct costs": x["direct_costs"],
                "First liability": x["first_liability"],
                "Right-of-use asset at the start": x["rou_first"],
                "Lease liability": x["liability"], "Due within a year": x["current"],
                "Due after a year": x["noncurrent"], "Right-of-use asset": x["rou"],
                "Interest": x["interest"], "Depreciation": x["depreciation"],
                "Payments": x["payments"], "Client's liability": x["client_liability"],
                "Liability difference": x["diff_liability"],
                "Client's asset": x["client_rou"], "Asset difference": x["diff_rou"],
                "Notes": " ".join(x["notes"]), "Note": x["note"]} for x in out["leases"]]
    periods = [{"Lease": x.name, "Period": p["period"], "From": p["start"].isoformat(),
                "To": (p["end"] - timedelta(days=1)).isoformat(),
                "Paid on": p["paid_on"].isoformat(), "Opening liability": _r(p["opening"]),
                "Payment": _r(p["payment"]), "Interest": _r(p["interest"]),
                "Closing liability": _r(p["closing"])}
               for x in e.leases for p in schedule(x)]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(summary).to_excel(xl, sheet_name="Leases", index=False)
        pd.DataFrame(periods, columns=["Lease", "Period", "From", "To", "Paid on",
                                       "Opening liability", "Payment", "Interest",
                                       "Closing liability"]).to_excel(
            xl, sheet_name="Schedules", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-leases')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/leases/export")
def export_leases(engagement_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "lease.download", "Downloaded the lease calculation", e)
    session.commit()
    name, content = workbook(e)
    return Response(content, media_type=XLSX, headers=attachment(name))
