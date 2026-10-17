"""Substantive analytical procedures (ISA 520) and proofs in total.

For a statement line the auditor builds an expectation of what the balance should be, from
inputs checked elsewhere: last year's figure and the change expected, a base times a rate
(interest on the average loan, depreciation on the cost of the assets), or a number of units
times a price (headcount times average pay). The app compares it with the balance in the
adjusted trial balance and the threshold the auditor accepts (performance materiality unless
another is set). A difference above the threshold needs an explanation that is corroborated,
or is treated as a misstatement. Proofs in total for depreciation, payroll and interest are
the same thing with their own starting inputs.

The actual figure is read live from the lead sheets, so it follows later adjusting entries;
a reviewer's sign-off stands only while the expectation, the actual and the threshold are
those they reviewed.
"""

from __future__ import annotations

import hashlib
import io
import math
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user, log, require
from .db import Engagement, Expectation, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook
from .statements import LINE

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

KINDS = {"analytic": "Analytical procedure", "depreciation": "Depreciation proof",
         "payroll": "Payroll proof", "interest": "Interest proof"}
METHODS = {"prior_change": "Last year's figure and the change expected",
           "rate": "A base times a rate", "units": "Units times a price",
           "amount": "An amount worked out elsewhere"}
# What each proof starts from: its method, line and the names of its inputs.
PRESETS = {
    "analytic": {"method": "prior_change", "line": "revenue", "inputs": {}},
    "depreciation": {"method": "rate", "line": "depreciation",
                     "inputs": {"base_label": "Average cost of the assets depreciated",
                                "months": 12}},
    "payroll": {"method": "units", "line": "staff_costs",
                "inputs": {"units_label": "Average headcount",
                           "price_label": "Average monthly pay per person", "periods": 12}},
    "interest": {"method": "rate", "line": "finance_costs",
                 "inputs": {"base_label": "Average loan balance", "months": 12}},
}
OUTCOMES = {"no_actual": "No balance to compare yet", "no_expectation": "Inputs missing",
            "no_threshold": "Set a threshold or materiality",
            "within": "Within the threshold", "to_explain": "Difference to explain",
            "explained": "Difference explained", "misstatement": "Misstatement"}
OPEN = ("no_actual", "no_expectation", "no_threshold", "to_explain")
MAX_EXPECTATIONS = 200
MAX_AMOUNT = 1e13
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Line = Literal[tuple(LINE)]  # type: ignore[valid-type]
Kind = Literal[tuple(KINDS)]  # type: ignore[valid-type]
Method = Literal[tuple(METHODS)]  # type: ignore[valid-type]
Amount = Annotated[float, Field(ge=-MAX_AMOUNT, le=MAX_AMOUNT, allow_inf_nan=False)]


class Inputs(BaseModel):
    """Only the ones the method uses count; the rest are kept in case it is switched back."""
    base: Amount | None = None          # prior_change (instead of last year's figure), rate
    base_label: Short = ""
    change_pct: Annotated[float, Field(ge=-100, le=10_000)] | None = None
    rate_pct: Annotated[float, Field(ge=0, le=1000)] | None = None
    months: Annotated[float, Field(gt=0, le=24)] | None = None
    units: Annotated[float, Field(ge=0, le=MAX_AMOUNT)] | None = None
    units_label: Short = ""
    price: Amount | None = None
    price_label: Short = ""
    periods: Annotated[float, Field(gt=0, le=1000)] | None = None
    amount: Amount | None = None


class ExpectationIn(BaseModel):
    kind: Kind = "analytic"
    title: Short = ""
    line: Line | None = None
    method: Method | None = None
    inputs: Inputs | None = None
    basis: Long = ""
    threshold: Annotated[float, Field(ge=0, le=MAX_AMOUNT, allow_inf_nan=False)] | None = None


class ExpectationPatch(BaseModel):
    """Only the fields sent change; threshold and actual can be cleared with null."""
    title: Short | None = None
    line: Line | None = None
    method: Method | None = None
    inputs: Inputs | None = None
    basis: Long | None = None
    threshold: Annotated[float, Field(ge=0, le=MAX_AMOUNT)] | None = None
    actual_override: Amount | None = None
    actual_note: Long | None = None
    explanation: Long | None = None
    misstatement: bool | None = None
    conclusion: Long | None = None
    reviewed: bool | None = None

    @field_validator("threshold", "actual_override")
    @classmethod
    def finite(cls, v):
        if v is not None and not math.isfinite(v):
            raise ValueError("Enter an amount.")
        return v


# --- Working it out ------------------------------------------------------------------

def line_figures(data: dict | None) -> dict[str, dict]:
    """line -> {"actual", "prior"} as shown on the lead sheets (normal side positive)."""
    if not data:
        return {}
    return {s["line"]: {"actual": s["totals"]["adjusted"],
                        "prior": s["totals"]["prior"] if data.get("has_prior") else None}
            for s in data.get("lead_sheets", [])}


def base_used(x: Expectation, figures: dict[str, dict]) -> float | None:
    """What the expectation starts from: the base entered, or for a change on last year,
    last year's balance of the line when no base is entered."""
    i = x.inputs or {}
    if x.method == "prior_change" and i.get("base") is None and x.line:
        return (figures.get(x.line) or {}).get("prior")
    return i.get("base")


def expected(x: Expectation, figures: dict[str, dict]) -> tuple[float | None, str]:
    """The expected amount and how it was worked out, or None when an input is missing."""
    i = x.inputs or {}
    if x.method == "prior_change":
        base = i.get("base")
        source = "the base entered"
        if base is None and x.line:
            base = (figures.get(x.line) or {}).get("prior")
            source = "last year's balance"
        if base is None or i.get("change_pct") is None:
            return None, ""
        return round(base * (1 + i["change_pct"] / 100), 2), \
            f"{source} {base:,.2f} changed by {i['change_pct']:g}%"
    if x.method == "rate":
        if i.get("base") is None or i.get("rate_pct") is None:
            return None, ""
        months = i.get("months") or 12
        return round(i["base"] * i["rate_pct"] / 100 * months / 12, 2), \
            f"{i['base']:,.2f} at {i['rate_pct']:g}% for {months:g} months"
    if x.method == "units":
        if i.get("units") is None or i.get("price") is None:
            return None, ""
        periods = i.get("periods") or 1
        return round(i["units"] * i["price"] * periods, 2), \
            f"{i['units']:g} times {i['price']:,.2f}" + \
            (f" times {periods:g}" if periods != 1 else "")
    return (None, "") if i.get("amount") is None else (round(i["amount"], 2), "the amount entered")


def threshold_of(x: Expectation, e: Engagement) -> float | None:
    return x.threshold if x.threshold is not None else e.performance_materiality


def actual_of(x: Expectation, figures: dict[str, dict]) -> float | None:
    if x.actual_override is not None:
        return round(x.actual_override, 2)
    if not x.line:
        return None
    return (figures.get(x.line) or {}).get("actual")


def state(x: Expectation, e: Engagement, figures: dict[str, dict]) -> dict:
    exp, how = expected(x, figures)
    act, limit = actual_of(x, figures), threshold_of(x, e)
    diff = None if exp is None or act is None else round(act - exp, 2)
    if act is None:
        outcome = "no_actual"
    elif exp is None:
        outcome = "no_expectation"
    elif limit is None:
        outcome = "no_threshold"
    elif x.misstatement:
        outcome = "misstatement"
    elif abs(diff) <= limit:
        outcome = "within"
    elif x.explanation.strip():
        outcome = "explained"
    else:
        outcome = "to_explain"
    mark = fingerprint(exp, act, limit, x.misstatement)
    return {"expected": exp, "how": how, "base_used": base_used(x, figures),
            "actual": act, "threshold": limit,
            "threshold_set": x.threshold is not None, "difference": diff,
            "difference_pct": round(100 * diff / exp, 1) if diff is not None and exp else None,
            "outcome": outcome, "fingerprint": mark,
            "review_stale": bool(x.reviewed_at and x.reviewed_fingerprint != mark)}


def fingerprint(exp, act, limit, misstatement) -> str:
    return hashlib.sha256(repr((exp, act, limit, bool(misstatement))).encode()).hexdigest()[:32]


def expectation_out(x: Expectation, e: Engagement, figures: dict[str, dict]) -> dict:
    s = state(x, e, figures)
    return {
        "id": x.id, "engagement_id": x.engagement_id, "kind": x.kind,
        "kind_label": KINDS[x.kind], "title": x.title or title_for(x), "own_title": x.title,
        "line": x.line, "line_label": LINE[x.line]["label"] if x.line in LINE else None,
        "method": x.method, "method_label": METHODS[x.method], "inputs": dict(x.inputs or {}),
        "basis": x.basis, "actual_override": x.actual_override, "actual_note": x.actual_note,
        "explanation": x.explanation, "misstatement": x.misstatement,
        "conclusion": x.conclusion, **{k: v for k, v in s.items() if k != "fingerprint"},
        "outcome_label": OUTCOMES[s["outcome"]], "open": s["outcome"] in OPEN,
        "reviewed_by": x.reviewed_by,
        "reviewed_at": x.reviewed_at.isoformat() if x.reviewed_at else None,
        "source": x.source, "created_by": x.created_by, "updated_by": x.updated_by,
        "updated_at": x.updated_at.isoformat(),
    }


def title_for(x: Expectation) -> str:
    if x.kind != "analytic":
        return KINDS[x.kind]
    return LINE[x.line]["label"] if x.line in LINE else KINDS[x.kind]


def figures_for(session: Session, e: Engagement) -> tuple[dict[str, dict], str | None]:
    """The lead sheet figures, or a reason there are none."""
    from .leadsheets import compute  # leadsheets imports engagements, which imports this

    try:
        data, tb, _ = compute(session, e, None, None)
    except HTTPException as exc:
        return {}, str(exc.detail)
    if tb is None:
        return {}, "Upload this year's trial balance to compare the expectations with it."
    return line_figures(data), None


def summary_of(items: list[dict]) -> dict:
    counts = {o: sum(1 for x in items if x["outcome"] == o) for o in OUTCOMES}
    return {"expectations": len(items), "outcomes": counts,
            "open": sum(counts[o] for o in OPEN),
            "not_reviewed": sum(1 for x in items if not x["reviewed_at"] or x["review_stale"]),
            "misstatements": round(sum(x["difference"] or 0 for x in items
                                       if x["outcome"] == "misstatement"), 2)}


def summary(session: Session, e: Engagement) -> dict:
    if not e.expectations:
        return summary_of([])
    figures, _ = figures_for(session, e)
    return summary_of([expectation_out(x, e, figures) for x in e.expectations])


def list_out(session: Session, e: Engagement) -> dict:
    figures, problem = figures_for(session, e)
    items = [expectation_out(x, e, figures) for x in e.expectations]
    return {"expectations": items, "summary": summary_of(items), "problem": problem,
            "performance_materiality": e.performance_materiality}


# --- Routes ------------------------------------------------------------------------

@router.get("/expectations/options")
def options() -> dict:
    return {"kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
            "methods": [{"id": k, "label": v} for k, v in METHODS.items()],
            "lines": [{"id": v["id"], "label": v["label"]} for v in LINE.values()],
            "presets": PRESETS}


@router.get("/engagements/{engagement_id}/expectations")
def list_expectations(engagement_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> dict:
    return list_out(session, get_or_404(session, Engagement, engagement_id, user))


def held(session: Session, user: User, expectation_id: int) -> Expectation:
    engagement_id = get_or_404(session, Expectation, expectation_id, user).engagement_id
    auth.hold(session, f"expectations:{engagement_id}")
    session.expire_all()
    return get_or_404(session, Expectation, expectation_id, user)


def clean_inputs(inputs: Inputs | None, base: dict | None = None) -> dict:
    out = dict(base or {})
    if inputs is not None:
        out.update(inputs.model_dump(exclude_unset=True))
    return {k: v for k, v in out.items() if v not in (None, "")}


@router.post("/engagements/{engagement_id}/expectations", status_code=201)
def create_expectation(engagement_id: int, body: ExpectationIn,
                       user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"expectations:{engagement_id}")
    if len(e.expectations) >= MAX_EXPECTATIONS:
        raise HTTPException(400, f"An engagement can have at most {MAX_EXPECTATIONS} "
                                 "expectations and proofs.")
    preset = PRESETS[body.kind]
    x = Expectation(engagement=e, kind=body.kind, title=body.title,
                    line=body.line or preset["line"], method=body.method or preset["method"],
                    inputs=clean_inputs(body.inputs, preset["inputs"]), basis=body.basis,
                    threshold=body.threshold, created_by=user.name, updated_by=user.name)
    session.add(x)
    session.flush()
    log(session, user, "expectation.create", f"Added the expectation {title_for(x)}", e,
        details={"expectation_id": x.id, "kind": x.kind, "line": x.line, "method": x.method})
    session.commit()
    return list_out(session, e)


@router.patch("/expectations/{expectation_id}")
def update_expectation(expectation_id: int, body: ExpectationPatch,
                       user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> dict:
    x = held(session, user, expectation_id)
    e = x.engagement
    changes = body.model_dump(exclude_unset=True)
    clearable = {"threshold", "actual_override", "line"}
    changes = {k: v for k, v in changes.items() if v is not None or k in clearable}
    if "reviewed" in changes and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can sign off an expectation.")
    for field in ("title", "line", "method", "basis", "threshold", "actual_override",
                  "actual_note", "explanation", "misstatement", "conclusion"):
        if field in changes:
            setattr(x, field, changes[field])
    if "inputs" in changes:
        x.inputs = clean_inputs(body.inputs, x.inputs)
    if x.actual_override is not None and not x.actual_note.strip():
        raise HTTPException(400, "Say where the actual figure entered comes from.")
    figures, _ = figures_for(session, e)
    s = state(x, e, figures)
    if changes.get("reviewed") is True:
        if s["outcome"] in OPEN:
            raise HTTPException(400, f"Finish it first: {OUTCOMES[s['outcome']]}.")
        if not x.conclusion.strip():
            raise HTTPException(400, "Write a conclusion before signing it off.")
        x.reviewed_by, x.reviewed_at, x.reviewed_fingerprint = user.name, utcnow(), \
            s["fingerprint"]
    elif changes.get("reviewed") is False:
        x.reviewed_by, x.reviewed_at, x.reviewed_fingerprint = "", None, ""
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "expectation.update", f"Changed the expectation {title_for(x)}", e,
        details={"expectation_id": x.id, "changed": sorted(changes), "outcome": s["outcome"],
                 "expected": s["expected"], "actual": s["actual"],
                 "difference": s["difference"], "reviewed": bool(x.reviewed_at)})
    session.commit()
    return list_out(session, e)


@router.delete("/expectations/{expectation_id}")
def delete_expectation(expectation_id: int, user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> dict:
    x = held(session, user, expectation_id)
    e = x.engagement
    if x.reviewed_at and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can remove an expectation that was signed "
                                 "off.")
    log(session, user, "expectation.delete", f"Removed the expectation {title_for(x)}", e,
        details={"expectation_id": x.id, "kind": x.kind, "line": x.line})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return list_out(session, e)


# --- Excel -------------------------------------------------------------------------

def to_excel(e: Engagement, listing: dict) -> bytes:
    s = listing["summary"]
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()),
             ("Performance materiality", e.performance_materiality),
             ("Expectations and proofs", s["expectations"]), ("Still open", s["open"]),
             ("Differences treated as misstatements", s["misstatements"])]
    if listing["problem"]:
        about.append(("Note", listing["problem"]))
    rows = []
    for n, x in enumerate(listing["expectations"], 1):
        i = x["inputs"]
        rows.append({
            "Ref": f"AP{n}", "Type": x["kind_label"], "Title": x["title"],
            "Statement line": x["line_label"] or "", "Method": x["method_label"],
            "Inputs": "; ".join(f"{k}: {v}" for k, v in i.items()),
            "Where the inputs come from": x["basis"], "Expected": x["expected"],
            "How": x["how"], "Actual": x["actual"],
            "Actual entered by hand because": x["actual_note"]
            if x["actual_override"] is not None else "",
            "Difference": x["difference"], "Difference %": x["difference_pct"],
            "Threshold": x["threshold"], "Outcome": x["outcome_label"],
            "Explanation": x["explanation"], "Conclusion": x["conclusion"],
            "Reviewed by": x["reviewed_by"] + (" (changed since)" if x["review_stale"] else "")})
    columns = ["Ref", "Type", "Title", "Statement line", "Method", "Inputs",
               "Where the inputs come from", "Expected", "How", "Actual",
               "Actual entered by hand because", "Difference", "Difference %", "Threshold",
               "Outcome", "Explanation", "Conclusion", "Reviewed by"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows, columns=columns).to_excel(xl, sheet_name="Analytics", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    stem = safe_stem(f"{e.client.name}-{e.name}-analytical-procedures")
    return f"{stem}.xlsx", to_excel(e, list_out(session, e))


@router.get("/engagements/{engagement_id}/expectations/export")
def export_expectations(engagement_id: int, user: User = Depends(current_user),
                        session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(session, e)
    log(session, user, "expectation.download",
        "Downloaded the analytical procedures workpaper", e,
        details={"expectations": len(e.expectations)})
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))
