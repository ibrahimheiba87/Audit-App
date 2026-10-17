"""Foreign currency translation of the draft statements, as in Caseware and CCH for a foreign
subsidiary (IAS 21): the statements are prepared in the entity's functional currency and
translated into the presentation currency, with

- assets and liabilities at the closing rate;
- income and expenses at the average rate for the year;
- share capital at the historical (equity) rate, and opening retained earnings at the
  amount already reported in the presentation currency (or the equity rate when not given);
- the difference that leaves goes to the foreign currency translation reserve in equity.

The team sets the currencies and rates once per engagement; every change is logged. The
translated statements go in the lead sheets download.
"""

from __future__ import annotations

import io
import re
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import statements
from .auth import current_user, log, require
from .db import CurrencyTranslation, Engagement, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True,
                                            min_length=3, max_length=3)]
Rate = Annotated[float, Field(gt=0, le=1e6, allow_inf_nan=False)]
Amount = Annotated[float, Field(ge=-1e13, le=1e13, allow_inf_nan=False)]
RESERVE = "Foreign currency translation reserve"


class TranslationIn(BaseModel):
    functional: Currency
    presentation: Currency
    closing_rate: Rate
    average_rate: Rate
    equity_rate: Rate
    opening_reserves: Amount | None = None
    opening_translation_reserve: Amount | None = None


def settings_out(t: CurrencyTranslation | None) -> dict | None:
    if t is None:
        return None
    return {"functional": t.functional, "presentation": t.presentation,
            "closing_rate": t.closing_rate, "average_rate": t.average_rate,
            "equity_rate": t.equity_rate, "opening_reserves": t.opening_reserves,
            "opening_translation_reserve": t.opening_translation_reserve,
            "updated_by": t.updated_by, "updated_at": t.updated_at.isoformat()}


def rate_for(line: str, t: CurrencyTranslation) -> tuple[float, str]:
    section = statements.LINE[line]["section"]
    if statements.SECTION[section].statement == "is":
        return t.average_rate, "average"
    if section == "equity":
        return t.equity_rate, "historical"
    return t.closing_rate, "closing"


def retained_presented(data: dict, t: CurrencyTranslation) -> float | None:
    """Retained earnings and reserves in the presentation currency (credit-positive) when the
    opening amount as reported is given: that amount, plus this year's movements in the line
    (such as dividends booked to it) at the average rate. The trial balance is before closing
    entries, so the opening amount in the functional currency is last year's reserves plus last
    year's profit; without last year's trial balance the line is taken as unchanged."""
    if t.opening_reserves is None:
        return None
    sheet = next((s for s in data["lead_sheets"] if s["line"] == "retained_earnings"), None)
    if sheet is None or not data.get("has_prior"):
        return t.opening_reserves
    opening = sheet["totals"]["prior"] + data["statements"]["profit"]["prior"]
    return t.opening_reserves + (sheet["totals"]["adjusted"] - opening) * t.average_rate


def translate(data: dict, t: CurrencyTranslation) -> dict:
    """The statements in `data` (from leadsheets.compute) in the presentation currency."""
    by_line: dict[str, list[dict]] = {}
    lines = []
    for s in data["lead_sheets"]:
        line = s["line"]
        rate, basis = rate_for(line, t)
        rows = [{"adjusted": a["adjusted"] if statements.debit_normal(line) else -a["adjusted"]}
                for a in s["accounts"]]  # back to debit-positive
        functional = s["totals"]["adjusted"]
        presented = functional * rate
        if line == "retained_earnings" and t.opening_reserves is not None:
            presented = retained_presented(data, t)
            rows = [{"adjusted": -presented}]
            rate, basis = None, "as reported"
        by_line[line] = [{"adjusted": r["adjusted"] * (rate or 1), "prior": 0.0}
                         for r in rows]
        lines.append({"line": line, "label": s["label"], "functional": functional,
                      "rate": rate, "basis": basis, "presented": round(presented, 2)})
    translated = statements._statements(by_line)
    reserve = translated["difference"]["current"]  # what translating at different rates leaves
    movement = (round(reserve - t.opening_translation_reserve, 2)
                if t.opening_translation_reserve is not None else None)
    functional_rows = data["statements"]
    rows = {}
    for name in ("income_statement", "balance_sheet"):
        rows[name] = [{"label": f["label"], "kind": f["kind"], "functional": f["current"],
                       "presented": p["current"]}
                      for f, p in zip(functional_rows[name], translated[name], strict=True)]
    # The reserve goes in equity, so the translated balance sheet balances.
    bs = rows["balance_sheet"]
    at = next(i for i, r in enumerate(bs) if r["label"] == "Total equity")
    bs.insert(at, {"label": RESERVE, "kind": "line", "functional": 0.0, "presented": reserve})
    for r in bs[at + 1:]:
        if r["label"] in ("Total equity", "Total liabilities and equity"):
            r["presented"] = round(r["presented"] + reserve, 2)
    return {"settings": settings_out(t), "lines": lines, "statements": rows,
            "profit": translated["profit"]["current"], "reserve": reserve,
            "reserve_movement": movement,
            "difference": data["statements"]["difference"]["current"],
            "notes": ["Without last year's trial balance, retained earnings are taken as "
                      "unchanged since the start of the year."]
            if t.opening_reserves is not None and not data.get("has_prior") else []}


def sheet(data: dict, t: CurrencyTranslation | None) -> dict[str, list[dict]]:
    """The translation as a sheet for the lead sheets workbook."""
    if t is None or not data.get("lead_sheets"):
        return {}
    out = translate(data, t)
    f, p = t.functional, t.presentation
    rows = [{"Statement": "Income statement" if name == "income_statement" else "Balance sheet",
             "Line": r["label"], f: None if r["kind"] == "heading" else r["functional"],
             p: None if r["kind"] == "heading" else r["presented"]}
            for name, list_ in out["statements"].items() for r in list_]
    rows += [{"Statement": "Rates", "Line": f"{label} ({f} 1 = {p} x)", p: rate}
             for label, rate in (("Closing rate", t.closing_rate), ("Average rate",
                                 t.average_rate), ("Historical rate for equity",
                                                   t.equity_rate))]
    if out["reserve_movement"] is not None:
        rows.append({"Statement": "Rates", "Line": "Translation difference for the year",
                     p: out["reserve_movement"]})
    return {"Translation": rows}


@router.get("/engagements/{engagement_id}/translation")
def get_translation(engagement_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    from .leadsheets import compute
    e = get_or_404(session, Engagement, engagement_id, user)
    if e.translation is None:
        return {"settings": None, "result": None}
    data, tb, _ = compute(session, e, None, None)
    return {"settings": settings_out(e.translation),
            "result": translate(data, e.translation) if tb is not None else None}


@router.put("/engagements/{engagement_id}/translation")
def save_translation(engagement_id: int, body: TranslationIn,
                     user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    if not all(re.fullmatch(r"[A-Z]{3}", c) for c in (body.functional, body.presentation)):
        raise HTTPException(400, "Use the three-letter currency code, such as USD or EGP.")
    if body.functional == body.presentation:
        raise HTTPException(400, "The functional and presentation currencies are the same, so "
                                 "there is nothing to translate.")
    e = held_engagement(session, user, engagement_id, f"translation:{engagement_id}")
    before = settings_out(e.translation)
    t = e.translation or CurrencyTranslation(engagement=e)
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    t.updated_by, t.updated_at = user.name, utcnow()
    session.add(t)
    log(session, user, "translation.save",
        f"Set the translation from {body.functional} to {body.presentation} (closing "
        f"{body.closing_rate:g}, average {body.average_rate:g}, equity {body.equity_rate:g})", e,
        details={"before": before, "after": body.model_dump()})
    session.commit()
    return get_translation(engagement_id, user, session)


@router.delete("/engagements/{engagement_id}/translation", status_code=204)
def delete_translation(engagement_id: int, user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> Response:
    e = held_engagement(session, user, engagement_id, f"translation:{engagement_id}")
    if e.translation is None:
        raise HTTPException(404, "There is no currency translation set up.")
    log(session, user, "translation.delete", "Removed the currency translation", e,
        details={"before": settings_out(e.translation)})
    e.translation = None
    session.commit()
    return Response(status_code=204)


@router.get("/engagements/{engagement_id}/translation/export")
def export_translation(engagement_id: int, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> Response:
    from .leadsheets import compute
    e = get_or_404(session, Engagement, engagement_id, user)
    data, tb, _ = compute(session, e, None, None)
    if e.translation is None or tb is None:
        raise HTTPException(400, "Set up the translation and upload a trial balance first.")
    log(session, user, "translation.download", "Downloaded the translated statements", e)
    session.commit()
    rows = sheet(data, e.translation).get("Translation")
    if not rows:
        raise HTTPException(400, "Map the trial balance's accounts to statement lines first.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(rows).to_excel(xl, sheet_name="Translation", index=False)
        finish_workbook(xl.book)
    name = safe_stem(f"{e.client.name}-{e.name}-translation")
    return Response(buf.getvalue(), media_type=XLSX, headers=attachment(f"{name}.xlsx"))

