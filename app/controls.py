"""Internal controls register and tests of controls (ISA 315 and ISA 330).

The team records the controls that matter to the audit, each with its area and the risk it
addresses, assesses its design and implementation (usually from a walkthrough), and tests
the operating effectiveness of the key ones. A test's sample size comes either from how often
the control runs (the usual firm practice for manual controls) or from attribute sampling: the
smallest sample that, with the deviations expected, still shows the deviation rate is below
the tolerable rate at the chosen confidence. The items to test are picked at random from the
population with a stored seed, so the selection can be repeated. Once tested, the upper
deviation limit is worked out from the deviations found.

Every change is logged, and the register exports to Excel.
"""

from __future__ import annotations

import io
import math
import secrets
from typing import Annotated, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy.orm import Session

from . import auth
from .ai_writer import AREAS
from .auth import current_user, log, require
from .db import Control, Engagement, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook
from .risks import area_label, check_area
from .statements import LINES

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

KINDS = {"preventive": "Preventive", "detective": "Detective"}
NATURES = {"manual": "Manual", "automated": "Automated",
           "it_dependent": "IT-dependent manual"}
# How often the control runs, and the sample many firms test for it when the test isn't
# statistical. Firms set their own; this is a starting point that can be changed.
FREQUENCIES = {
    "annual": ("Annual", 1),
    "quarterly": ("Quarterly", 2),
    "monthly": ("Monthly", 2),
    "weekly": ("Weekly", 5),
    "daily": ("Daily", 20),
    "multiple_daily": ("Several times a day", 25),
    "per_transaction": ("Each transaction", 25),
}
DESIGNS = {"not_assessed": "Not assessed", "effective": "Designed and implemented",
           "deficient": "Deficient"}
METHODS = {"frequency": "By how often it runs", "statistical": "Attribute sampling"}
RESULTS = {"not_tested": "Not tested", "effective": "Operating effectively",
           "not_effective": "Not operating effectively"}
CONFIDENCES = (90, 95)
MAX_CONTROLS = 500
MAX_SAMPLE = 2000
MAX_POPULATION = 10_000_000
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


# --- Attribute sampling ------------------------------------------------------------

def _beta_fraction(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)),
                   -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 3e-14:
            break
    return h


def beta_inc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(a, b, x) / a
    return 1 - front * _beta_fraction(b, a, 1 - x) / b


def at_most(k: float, n: int, p: float) -> float:
    """P(at most k deviations in n items) at deviation rate p. k may be a fraction (the
    expected deviations), which the incomplete beta function handles smoothly."""
    if k >= n:
        return 1.0
    return beta_inc(n - k, k + 1, 1 - p)


def sample_size(confidence: int, tolerable: float, expected: float) -> dict:
    """The smallest sample that, if it holds the expected deviations, still shows at
    `confidence`% that the deviation rate is below `tolerable`%."""
    if confidence not in CONFIDENCES:
        raise ValueError("Confidence must be 90% or 95%.")
    if not (math.isfinite(tolerable) and 0 < tolerable < 50):
        raise ValueError("The tolerable deviation rate must be above 0% and below 50%.")
    if not (math.isfinite(expected) and 0 <= expected < tolerable):
        raise ValueError("The expected deviation rate must be at least 0% and below the "
                         "tolerable rate.")
    risk, t, e = 1 - confidence / 100, tolerable / 100, expected / 100
    for n in range(1, MAX_SAMPLE + 1):
        # The expected deviations, rounded up to whole ones (as the published tables do), so
        # finding them still lets the control be relied on.
        allowed = math.ceil(n * e - 1e-9)
        if at_most(allowed, n, t) <= risk + 1e-12:
            return {"sample_size": n, "expected_deviations": round(n * e, 2),
                    "allowed_deviations": allowed}
    raise ValueError(f"That needs more than {MAX_SAMPLE} items. Raise the tolerable rate or "
                     "lower the expected rate.")


def upper_limit(deviations: int, tested: int, confidence: int) -> float:
    """The upper deviation limit in %: the highest deviation rate consistent, at
    `confidence`%, with finding `deviations` in `tested` items."""
    if deviations >= tested:
        return 100.0
    risk = 1 - confidence / 100
    low, high = deviations / tested, 1.0
    for _ in range(100):
        mid = (low + high) / 2
        if at_most(deviations, tested, mid) > risk:
            low = mid
        else:
            high = mid
    return round(100 * high, 2)


def pick_items(population: int, size: int, seed: int) -> list[int]:
    """Item numbers (1 to population) to test, the same every time for the same seed."""
    if size >= population:
        return list(range(1, population + 1))
    rng = np.random.default_rng(seed)
    return sorted(int(i) + 1 for i in rng.choice(population, size=size, replace=False))


# --- Schemas -----------------------------------------------------------------------

def check_choice(value: str | None, choices: dict, what: str) -> str | None:
    if value is not None and value not in choices:
        raise ValueError(f"Unknown {what}.")
    return value


class ControlIn(BaseModel):
    area: str
    title: Title
    process: Short = ""
    description: Long = ""
    risk_id: int | None = None
    kind: str = "preventive"
    nature: str = "manual"
    frequency: str = "monthly"
    key: bool = True
    owner: Short = ""

    _area = field_validator("area")(classmethod(lambda cls, v: check_area(v)))
    _kind = field_validator("kind")(classmethod(lambda cls, v: check_choice(v, KINDS, "type")))
    _nature = field_validator("nature")(
        classmethod(lambda cls, v: check_choice(v, NATURES, "nature")))
    _frequency = field_validator("frequency")(
        classmethod(lambda cls, v: check_choice(v, FREQUENCIES, "frequency")))


class ControlPatch(BaseModel):
    area: str | None = None
    title: Title | None = None
    process: Short | None = None
    description: Long | None = None
    risk_id: int | None = None
    kind: str | None = None
    nature: str | None = None
    frequency: str | None = None
    key: bool | None = None
    owner: Short | None = None
    design: str | None = None
    design_note: Long | None = None
    walkthrough: Short | None = None

    _area = field_validator("area")(
        classmethod(lambda cls, v: v if v is None else check_area(v)))
    _kind = field_validator("kind")(classmethod(lambda cls, v: check_choice(v, KINDS, "type")))
    _nature = field_validator("nature")(
        classmethod(lambda cls, v: check_choice(v, NATURES, "nature")))
    _frequency = field_validator("frequency")(
        classmethod(lambda cls, v: check_choice(v, FREQUENCIES, "frequency")))
    _design = field_validator("design")(
        classmethod(lambda cls, v: check_choice(v, DESIGNS, "design assessment")))


class PlanIn(BaseModel):
    method: Literal["frequency", "statistical"]
    confidence: Literal[90, 95] = 95
    tolerable_rate: float | None = Field(default=None, gt=0, lt=50)
    expected_rate: float | None = Field(default=None, ge=0, lt=50)
    population: int | None = Field(default=None, ge=1, le=MAX_POPULATION)
    sample_size: int | None = Field(default=None, ge=1, le=MAX_SAMPLE)  # None: as suggested


class ResultIn(BaseModel):
    tested: int = Field(ge=1, le=MAX_SAMPLE)
    deviations: int = Field(ge=0, le=MAX_SAMPLE)
    deviation_note: Long = ""
    conclusion: Long = ""
    workpaper: Short = ""


# --- Output ------------------------------------------------------------------------

def result_of(c: Control) -> dict:
    """Whether the test shows the control operating effectively, and the upper deviation
    limit for a statistical test."""
    if c.tested is None or c.deviations is None:
        return {"result": "not_tested", "upper_limit": None}
    if c.method == "statistical" and c.confidence and c.tolerable_rate:
        whole = c.population is not None and c.tested >= c.population
        limit = (round(100 * c.deviations / c.tested, 2) if whole
                 else upper_limit(c.deviations, c.tested, c.confidence))
        return {"result": "effective" if limit <= c.tolerable_rate else "not_effective",
                "upper_limit": limit}
    # Tests sized by frequency expect no deviations: any one means the control can't be
    # relied on as planned.
    return {"result": "effective" if c.deviations == 0 else "not_effective",
            "upper_limit": None}


def control_out(c: Control, risks: dict[int, str]) -> dict:
    r = result_of(c)
    return {
        "id": c.id, "engagement_id": c.engagement_id, "area": c.area,
        "area_label": area_label(c.area), "process": c.process, "title": c.title,
        "description": c.description,
        "risk_id": c.risk_id if c.risk_id in risks else None,
        "risk_title": risks.get(c.risk_id) if c.risk_id else None,
        "kind": c.kind, "kind_label": KINDS[c.kind],
        "nature": c.nature, "nature_label": NATURES[c.nature],
        "frequency": c.frequency, "frequency_label": FREQUENCIES[c.frequency][0],
        "key": c.key, "owner": c.owner,
        "design": c.design, "design_label": DESIGNS[c.design], "design_note": c.design_note,
        "walkthrough": c.walkthrough,
        "method": c.method, "method_label": METHODS.get(c.method or ""),
        "confidence": c.confidence, "tolerable_rate": c.tolerable_rate,
        "expected_rate": c.expected_rate, "population": c.population,
        "sample_size": c.sample_size, "seed": c.seed, "selected": c.selected or [],
        "tested": c.tested, "deviations": c.deviations, "deviation_note": c.deviation_note,
        "test_conclusion": c.test_conclusion, "test_workpaper": c.test_workpaper,
        "tested_by": c.tested_by, "tested_at": c.tested_at.isoformat() if c.tested_at else None,
        "result": r["result"], "result_label": RESULTS[r["result"]],
        "upper_limit": r["upper_limit"], "source": c.source,
        "created_by": c.created_by, "created_at": c.created_at.isoformat(),
    }


def summary(controls: list[Control]) -> dict:
    results = [result_of(c)["result"] for c in controls]
    key = [c for c in controls if c.key]
    return {
        "controls": len(controls), "key": len(key),
        "design": {d: sum(1 for c in controls if c.design == d) for d in DESIGNS},
        "tested": sum(1 for r in results if r != "not_tested"),
        "effective": results.count("effective"),
        "not_effective": results.count("not_effective"),
        # Key controls designed well enough to rely on, but not yet tested.
        "key_to_test": sum(1 for c in key if c.design == "effective"
                           and result_of(c)["result"] == "not_tested"),
        "deficient": sum(1 for c, r in zip(controls, results, strict=True)
                         if c.design == "deficient" or r == "not_effective"),
    }


def risk_titles(e: Engagement) -> dict[int, str]:
    return {r.id: r.title for r in e.risks}


def sorted_controls(e: Engagement) -> list[Control]:
    return sorted(e.controls, key=lambda c: (not c.key, c.process.lower(), c.id))


def register_out(e: Engagement) -> dict:
    controls = sorted_controls(e)
    risks = risk_titles(e)
    return {"summary": summary(controls), "controls": [control_out(c, risks) for c in controls]}


# --- Routes --------------------------------------------------------------------------

@router.get("/controls/options")
def options() -> dict:
    return {
        "areas": [{"id": k, "label": v} for k, v in AREAS.items()]
                 + [{"id": id_, "label": label} for id_, label, _ in LINES],
        "kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
        "natures": [{"id": k, "label": v} for k, v in NATURES.items()],
        "frequencies": [{"id": k, "label": v[0], "sample_size": v[1]}
                        for k, v in FREQUENCIES.items()],
        "designs": [{"id": k, "label": v} for k, v in DESIGNS.items()],
        "methods": [{"id": k, "label": v} for k, v in METHODS.items()],
        "confidences": list(CONFIDENCES),
    }


@router.get("/controls/sample-size")
def get_sample_size(confidence: int = 95, tolerable: float = 5.0,
                    expected: float = 0.0) -> dict:
    try:
        return sample_size(confidence, tolerable, expected)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/engagements/{engagement_id}/controls")
def list_controls(engagement_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    return register_out(get_or_404(session, Engagement, engagement_id, user))


def held(session: Session, user: User, control_id: int) -> Control:
    """The control, with its register's guard held so changes to it take turns."""
    engagement_id = get_or_404(session, Control, control_id, user).engagement_id
    auth.hold(session, f"controls:{engagement_id}")
    session.expire_all()
    return get_or_404(session, Control, control_id, user)


def check_risk(e: Engagement, risk_id: int | None) -> None:
    if risk_id is not None and risk_id not in risk_titles(e):
        raise HTTPException(400, "Pick a risk from this engagement's risk register.")


@router.post("/engagements/{engagement_id}/controls", status_code=201)
def create_control(engagement_id: int, body: ControlIn, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"controls:{engagement_id}")
    if len(e.controls) >= MAX_CONTROLS:
        raise HTTPException(400, f"An engagement can have at most {MAX_CONTROLS} controls.")
    check_risk(e, body.risk_id)
    c = Control(engagement=e, created_by=user.name, **body.model_dump())
    session.add(c)
    session.flush()
    log(session, user, "control.create", f"Added control #{c.id}: {c.title}", e,
        details={"control_id": c.id, "area": c.area, "key": c.key, "frequency": c.frequency})
    session.commit()
    return control_out(c, risk_titles(e))


@router.patch("/controls/{control_id}")
def update_control(control_id: int, body: ControlPatch, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    c = held(session, user, control_id)
    changes = body.model_dump(exclude_unset=True)
    # Only the risk link can be cleared; other fields left out or null stay as they are.
    changes = {k: v for k, v in changes.items() if v is not None or k == "risk_id"}
    if "risk_id" in changes:
        check_risk(c.engagement, changes["risk_id"])
    for field, value in changes.items():
        setattr(c, field, value)
    action = "control.design" if {"design", "design_note", "walkthrough"} & set(changes) \
        else "control.update"
    log(session, user, action, f"Changed control #{c.id}: {c.title}", c.engagement,
        details={"control_id": c.id, "changed": sorted(changes), "design": c.design})
    session.commit()
    return control_out(c, risk_titles(c.engagement))


@router.post("/controls/{control_id}/plan")
def plan_test(control_id: int, body: PlanIn, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    """Plan the test of operating effectiveness: its sample size and, when the population
    is known, the items to test. Planning again clears an earlier result."""
    c = held(session, user, control_id)
    if c.nature == "automated" and body.method == "statistical":
        raise HTTPException(400, "An automated control is usually tested once, with the IT "
                                 "general controls around it. Size it by how often it runs.")
    if body.method == "statistical":
        if body.tolerable_rate is None:
            raise HTTPException(400, "Enter the tolerable deviation rate.")
        try:
            suggested = sample_size(body.confidence, body.tolerable_rate,
                                    body.expected_rate or 0.0)["sample_size"]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        suggested = 1 if c.nature == "automated" else FREQUENCIES[c.frequency][1]
    size = body.sample_size or suggested
    if body.population is not None:
        size = min(size, body.population)
    c.method = body.method
    c.confidence = body.confidence if body.method == "statistical" else None
    c.tolerable_rate = body.tolerable_rate if body.method == "statistical" else None
    c.expected_rate = (body.expected_rate or 0.0) if body.method == "statistical" else None
    c.population, c.sample_size = body.population, size
    c.seed = secrets.randbelow(2**31 - 1) if body.population else None
    c.selected = pick_items(body.population, size, c.seed) if body.population else None
    c.tested = c.deviations = None
    c.tested_by, c.tested_at = "", None
    log(session, user, "control.plan", f"Planned the test of control #{c.id}: {c.title}",
        c.engagement, details={"control_id": c.id, "method": c.method,
                               "sample_size": size, "suggested": suggested,
                               "population": c.population, "seed": c.seed,
                               "confidence": c.confidence, "tolerable_rate": c.tolerable_rate,
                               "expected_rate": c.expected_rate})
    session.commit()
    return control_out(c, risk_titles(c.engagement))


@router.post("/controls/{control_id}/result")
def record_result(control_id: int, body: ResultIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    c = held(session, user, control_id)
    if c.method is None:
        raise HTTPException(400, "Plan the test first.")
    if body.deviations > body.tested:
        raise HTTPException(400, "There can't be more deviations than items tested.")
    if c.sample_size and body.tested < c.sample_size:
        raise HTTPException(400, f"Test at least the {c.sample_size} items planned, or plan "
                                 "the test again with a smaller sample.")
    if c.population is not None and body.tested > c.population:
        raise HTTPException(400, "More items were tested than the population holds.")
    if body.deviations and not body.deviation_note:
        raise HTTPException(400, "Describe the deviations found.")
    c.tested, c.deviations = body.tested, body.deviations
    c.deviation_note, c.test_conclusion = body.deviation_note, body.conclusion
    c.test_workpaper = body.workpaper
    c.tested_by, c.tested_at = user.name, utcnow()
    r = result_of(c)
    log(session, user, "control.test", f"Recorded the test of control #{c.id}: {c.title}",
        c.engagement, details={"control_id": c.id, "tested": c.tested,
                               "deviations": c.deviations, "result": r["result"],
                               "upper_limit": r["upper_limit"]})
    session.commit()
    return control_out(c, risk_titles(c.engagement))


@router.delete("/controls/{control_id}", status_code=204)
def delete_control(control_id: int, user: User = Depends(require("review")),
                   session: Session = Depends(get_session)) -> Response:
    c = held(session, user, control_id)
    log(session, user, "control.delete", f"Removed control #{c.id}: {c.title}", c.engagement,
        details={"control_id": c.id, "tested": c.tested, "deviations": c.deviations})
    session.delete(c)
    session.commit()
    return Response(status_code=204)


# --- Excel -------------------------------------------------------------------------

def to_excel(e: Engagement, context: dict) -> bytes:
    controls = sorted_controls(e)
    s = summary(controls)
    risks = risk_titles(e)
    about = list(context.items()) + [
        ("Controls", s["controls"]), ("Key controls", s["key"]),
        *[(f"Design: {DESIGNS[k]}", v) for k, v in s["design"].items()],
        ("Tested", s["tested"]), ("Operating effectively", s["effective"]),
        ("Not operating effectively", s["not_effective"]),
        ("Deficiencies (design or operation)", s["deficient"]),
    ]
    register, tests = [], []
    for n, c in enumerate(controls, 1):
        out = control_out(c, risks)
        register.append({
            "Ref": f"C{n}", "Key": "Yes" if c.key else "No", "Process": c.process,
            "Area": out["area_label"], "Control": c.title, "Description": c.description,
            "Risk addressed": out["risk_title"] or "", "Type": out["kind_label"],
            "Nature": out["nature_label"], "Frequency": out["frequency_label"],
            "Owner": c.owner, "Design and implementation": out["design_label"],
            "Design note": c.design_note, "Walkthrough": c.walkthrough,
            "Operating effectiveness": out["result_label"]})
        if c.method:
            tests.append({
                "Ref": f"C{n}", "Control": c.title, "Method": out["method_label"],
                "Confidence %": c.confidence, "Tolerable rate %": c.tolerable_rate,
                "Expected rate %": c.expected_rate, "Population": c.population,
                "Sample size": c.sample_size, "Seed": c.seed,
                "Items picked": ", ".join(str(i) for i in (c.selected or [])),
                "Tested": c.tested, "Deviations": c.deviations,
                "Upper deviation limit %": out["upper_limit"],
                "Result": out["result_label"], "Deviations found": c.deviation_note,
                "Conclusion": c.test_conclusion, "Workpaper": c.test_workpaper,
                "Tested by": c.tested_by,
                "Tested at": f"{c.tested_at:%Y-%m-%d %H:%M} UTC" if c.tested_at else ""})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(register, columns=[
            "Ref", "Key", "Process", "Area", "Control", "Description", "Risk addressed", "Type",
            "Nature", "Frequency", "Owner", "Design and implementation", "Design note",
            "Walkthrough", "Operating effectiveness"]).to_excel(
            xl, sheet_name="Controls register", index=False)
        pd.DataFrame(tests, columns=[
            "Ref", "Control", "Method", "Confidence %", "Tolerable rate %", "Expected rate %",
            "Population", "Sample size", "Seed", "Items picked", "Tested", "Deviations",
            "Upper deviation limit %", "Result", "Deviations found", "Conclusion", "Workpaper",
            "Tested by", "Tested at"]).to_excel(xl, sheet_name="Tests of controls", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.get("/engagements/{engagement_id}/controls/export")
def export_controls(engagement_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "control.download", "Downloaded the controls register", e,
        details={"controls": len(e.controls)})
    session.commit()
    name, content = workbook(e)
    return Response(content, media_type=XLSX, headers=attachment(name))


def workbook(e: Engagement) -> tuple[str, bytes]:
    """The controls register and tests of controls as an Excel file, and its name."""
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat()}
    stem = safe_stem(f"{e.client.name}-{e.name}-controls")
    return f"{stem}.xlsx", to_excel(e, context)
