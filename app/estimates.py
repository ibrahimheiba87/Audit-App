"""Accounting estimates (ISA 540), as Caseware and TeamMate keep them: a register of the
client's estimates with how each was made and how the auditor tested it.

For each estimate the team records management's amount, the method and key assumptions, how
uncertain it is, and the auditor's own range (or point). The app shows how far management's
amount falls outside the range, which is the misstatement to consider, and where in the
range it sits. With last year's estimate and what it turned out to be, it shows how far off
last year's estimate was (the look back ISA 540 asks for). When most estimates sit at the
same end of the auditor's ranges, it notes a possible bias. Roll-forward carries each
estimate, with this year's amount as next year's "last year's estimate".
"""

from __future__ import annotations

import io
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from .auth import current_user, log, require
from .db import AccountingEstimate, Engagement, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook
from .leases import threshold
from .statements import LINE, LINES

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MAX_ESTIMATES = 100
UNCERTAINTY = {"low": "Low", "moderate": "Moderate", "high": "High"}
APPROACHES = {
    "test_method": "Test how management made it",
    "own_estimate": "Make our own estimate or range",
    "later_events": "Use events after the period end",
}
BIAS_MIN = 3  # estimates needed before a pattern is worth noting
BIAS_NOTES = {
    side: f"Most of management's estimates sit at the {side} end of our ranges. Consider "
          "whether this points to management bias (ISA 540)." for side in ("high", "low")}

Money = Annotated[float, Field(ge=-1e13, le=1e13, allow_inf_nan=False)]


class EstimateIn(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    line: str | None = None
    management: Money
    method: str = Field(default="", max_length=5000)
    assumptions: str = Field(default="", max_length=5000)
    uncertainty: str = "moderate"
    approach: str = "test_method"
    low: Money | None = None
    high: Money | None = None
    prior_estimate: Money | None = None
    prior_outcome: Money | None = None
    conclusion: str = Field(default="", max_length=5000)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def check(body: EstimateIn) -> None:
    if body.line is not None and body.line not in LINE:
        raise HTTPException(400, "Pick a statement line from the list.")
    if body.uncertainty not in UNCERTAINTY:
        raise HTTPException(400, "Pick how uncertain the estimate is.")
    if body.approach not in APPROACHES:
        raise HTTPException(400, "Pick how the estimate is tested.")
    if body.low is not None and body.high is not None and body.low > body.high:
        raise HTTPException(400, "The low end of the range is above the high end.")


def estimate_out(x: AccountingEstimate, e: Engagement) -> dict:
    low = x.low if x.low is not None else x.high
    high = x.high if x.high is not None else x.low
    outside = position = None
    if low is not None:
        if x.management < low:
            outside = _r(x.management - low)
        elif x.management > high:
            outside = _r(x.management - high)
        else:
            outside = 0.0
        if high > low:
            position = round(100 * (x.management - low) / (high - low), 1)
    look_back = look_back_pct = None
    if x.prior_estimate is not None and x.prior_outcome is not None:
        look_back = _r(x.prior_outcome - x.prior_estimate)
        if x.prior_estimate:
            look_back_pct = round(100 * look_back / abs(x.prior_estimate), 1)
    flags = []
    if outside and abs(outside) > threshold(e):
        flags.append("outside")
    if look_back_pct is not None and abs(look_back_pct) >= 10 and abs(look_back) > threshold(e):
        flags.append("look_back")
    if (x.uncertainty == "high" and low is None) or (x.uncertainty == "high" and not x.assumptions):
        flags.append("high_uncertainty")
    return {"id": x.id, "title": x.title, "line": x.line,
            "line_label": LINE[x.line]["label"] if x.line in LINE else None,
            "management": x.management, "method": x.method, "assumptions": x.assumptions,
            "uncertainty": x.uncertainty, "uncertainty_label": UNCERTAINTY[x.uncertainty],
            "approach": x.approach, "approach_label": APPROACHES[x.approach],
            "low": x.low, "high": x.high, "outside": outside, "position_pct": position,
            "prior_estimate": x.prior_estimate, "prior_outcome": x.prior_outcome,
            "look_back": look_back, "look_back_pct": look_back_pct,
            "conclusion": x.conclusion, "flags": flags, "created_by": x.created_by,
            "updated_by": x.updated_by}


def estimates_out(e: Engagement) -> dict:
    rows = [estimate_out(x, e) for x in e.estimates]
    ranged = [x for x in rows if x["position_pct"] is not None]
    notes = []
    if len(ranged) >= BIAS_MIN:
        top = sum(1 for x in ranged if x["position_pct"] > 50)
        bottom = sum(1 for x in ranged if x["position_pct"] < 50)
        if top >= len(ranged) * 0.75 or bottom >= len(ranged) * 0.75:
            notes.append(BIAS_NOTES["high" if top > bottom else "low"])
    if any("high_uncertainty" in x["flags"] for x in rows):
        notes.append("An estimate with high uncertainty needs its key assumptions and our "
                     "range recorded.")
    # Kept apart, so an estimate above our range and another below don't cancel out.
    above = _r(sum(x["outside"] for x in rows if x["outside"] and x["outside"] > 0))
    below = _r(sum(x["outside"] for x in rows if x["outside"] and x["outside"] < 0))
    return {"estimates": rows, "above": above, "below": below, "notes": notes,
            "flagged": sum(1 for x in rows if x["flags"]),
            "lines": [{"id": i, "label": label} for i, label, _ in LINES],
            "uncertainties": [{"id": k, "label": v} for k, v in UNCERTAINTY.items()],
            "approaches": [{"id": k, "label": v} for k, v in APPROACHES.items()]}


@router.get("/engagements/{engagement_id}/estimates")
def get_estimates(engagement_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    return estimates_out(get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/estimates", status_code=201)
def add_estimate(engagement_id: int, body: EstimateIn, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    check(body)
    e = held_engagement(session, user, engagement_id, f"estimates:{engagement_id}")
    if len(e.estimates) >= MAX_ESTIMATES:
        raise HTTPException(400, f"An engagement can have up to {MAX_ESTIMATES} estimates.")
    x = AccountingEstimate(engagement=e, created_by=user.name, updated_by=user.name,
                           **{**body.model_dump(), "title": body.title.strip()})
    session.add(x)
    session.flush()
    log(session, user, "estimate.create", f"Added the estimate {x.title}", e,
        details={"estimate_id": x.id, **body.model_dump()})
    session.commit()
    session.refresh(e)
    return estimates_out(e)


def held_estimate(session: Session, user: User, estimate_id: int):
    x = get_or_404(session, AccountingEstimate, estimate_id, user)
    e = held_engagement(session, user, x.engagement_id, f"estimates:{x.engagement_id}")
    return get_or_404(session, AccountingEstimate, estimate_id, user), e


@router.put("/estimates/{estimate_id}")
def update_estimate(estimate_id: int, body: EstimateIn, user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    check(body)
    x, e = held_estimate(session, user, estimate_id)
    before = {k: getattr(x, k) for k in EstimateIn.model_fields}
    for k, v in body.model_dump().items():
        setattr(x, k, v.strip() if k == "title" else v)
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "estimate.update", f"Changed the estimate {x.title}", e,
        details={"estimate_id": x.id, "before": before, "after": body.model_dump()})
    session.commit()
    session.refresh(e)
    return estimates_out(e)


@router.delete("/estimates/{estimate_id}")
def delete_estimate(estimate_id: int, user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    x, e = held_estimate(session, user, estimate_id)
    log(session, user, "estimate.delete", f"Deleted the estimate {x.title}", e,
        details={"estimate_id": x.id, "title": x.title, "management": x.management})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return estimates_out(e)


def workbook(e: Engagement) -> tuple[str, bytes]:
    out = estimates_out(e)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Estimates", len(out["estimates"])),
             ("Above our ranges in total", out["above"]),
             ("Below our ranges in total", out["below"]),
             *[("To note", n) for n in out["notes"]]]
    rows = [{"Estimate": x["title"], "Statement line": x["line_label"] or "",
             "Management's amount": x["management"], "Method": x["method"],
             "Key assumptions": x["assumptions"], "Uncertainty": x["uncertainty_label"],
             "How we tested it": x["approach_label"], "Our range from": x["low"],
             "Our range to": x["high"], "Outside our range by": x["outside"],
             "Where in our range %": x["position_pct"],
             "Last year's estimate": x["prior_estimate"],
             "What it turned out to be": x["prior_outcome"], "Off by": x["look_back"],
             "Off by %": x["look_back_pct"], "Conclusion": x["conclusion"]}
            for x in out["estimates"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows).to_excel(xl, sheet_name="Estimates", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-accounting-estimates')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/estimates/export")
def export_estimates(engagement_id: int, user: User = Depends(current_user),
                     session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "estimate.download", "Downloaded the accounting estimates", e)
    session.commit()
    name, content = workbook(e)
    return Response(content, media_type=XLSX, headers=attachment(name))
