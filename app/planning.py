"""The engagement letter and the planning memo, as in Caseware and CCH's planning documents.

The team records the letter's terms (who it goes to, the fee, when the report is due, other
services) and when the client accepted it, plus the memo's own text: the business, the
approach and the timetable. The rest comes from the file: materiality, the risk register,
the acceptance decision, the team, the time budget, the staff schedule and the group's
components. Both download as Word files in English or Arabic with short template sentences
for the team to finish, and go in the audit file pack.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import docx, schedule
from .auth import current_user, log, require
from .db import Engagement, EngagementPlan, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment
from .planning_wording import LETTER, MEMO
from .report_draft import entity_name, framework_text
from .risks import LEVEL_RANK, area_label

router = APIRouter(prefix="/api")
APPROACHES = {"substantive": "Substantive", "combined": "Combined (relying on controls)"}
FEE_BASES = {"fixed": "Fixed fee", "hourly": "Time spent (estimate)"}
MAX_MEMO_RISKS = 20
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]  # as the columns
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True,
                                            max_length=3)]
FIELDS = ("addressee", "firm_name", "partner_name", "city", "letter_date", "fee", "currency",
          "fee_basis", "report_due", "other_services", "accepted_by", "accepted_on",
          "business", "approach", "timetable", "other_notes")
DATES = ("letter_date", "report_due", "accepted_on")


class PlanIn(BaseModel):
    """Only the fields sent change; the dates and the fee can be cleared with null."""
    addressee: Short | None = None
    firm_name: Short | None = None
    partner_name: Name | None = None
    city: Name | None = None
    letter_date: date | None = None
    fee: float | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)
    currency: Currency | None = None
    fee_basis: Literal["fixed", "hourly"] | None = None
    report_due: date | None = None
    other_services: Text | None = None
    accepted_by: Name | None = None
    accepted_on: date | None = None
    business: Text | None = None
    approach: Literal["substantive", "combined"] | None = None
    timetable: Text | None = None
    other_notes: Text | None = None


def plan_out(e: Engagement) -> dict:
    p = e.plan or EngagementPlan(fee_basis="fixed", approach="substantive")
    out = {k: getattr(p, k) for k in FIELDS}
    for k in DATES:
        out[k] = out[k].isoformat() if out[k] else None
    for k in FIELDS:
        if out[k] is None and k not in DATES and k != "fee":
            out[k] = ""
    # Until set, the firm, partner and city follow the draft auditor's report.
    r = e.audit_report
    for k in ("firm_name", "partner_name", "city"):
        if not out[k] and r is not None:
            out[k] = getattr(r, k) or ""
    out["updated_by"] = e.plan.updated_by if e.plan else ""
    out["updated_at"] = e.plan.updated_at.isoformat() if e.plan else None
    return out


def warnings(e: Engagement, p: dict) -> list[str]:
    out = []
    a = e.acceptance
    if a is None or a.decision is None:
        out.append("The partner's acceptance decision isn't recorded yet.")
    elif a.decision == "decline":
        out.append("The partner declined this engagement.")
    if not p["accepted_on"]:
        out.append("Record when the client accepted the engagement letter.")
    elif p["letter_date"] and p["accepted_on"] < p["letter_date"]:
        out.append("The client's acceptance is dated before the letter.")
    if p["report_due"] and p["report_due"] <= e.period_end.isoformat():
        out.append("The report due date must be after the period end.")
    if p["fee"] is None:
        out.append("Set the fee for the engagement letter.")
    if e.overall_materiality is None:
        out.append("Set materiality before finishing the planning memo.")
    if not e.risks:
        out.append("Add the risks to the register before finishing the planning memo.")
    if not e.time_budgets:
        out.append("Set the time budget before finishing the planning memo.")
    return out


def state(e: Engagement) -> dict:
    p = plan_out(e)
    return {**p, "warnings": warnings(e, p),
            "approaches": [{"id": k, "label": v} for k, v in APPROACHES.items()],
            "fee_bases": [{"id": k, "label": v} for k, v in FEE_BASES.items()],
            "booked_hours": schedule.booked_hours(e),
            "budget_hours": round(sum(b.hours for b in e.time_budgets), 2)}


@router.get("/engagements/{engagement_id}/planning")
def get_planning(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    return state(get_or_404(session, Engagement, engagement_id, user))


@router.put("/engagements/{engagement_id}/planning")
def save_planning(engagement_id: int, body: PlanIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    if body.currency and not re.fullmatch(r"[A-Z]{3}", body.currency):
        raise HTTPException(400, "Use the three-letter currency code, such as USD or EGP.")
    e = held_engagement(session, user, engagement_id, f"planning:{engagement_id}")
    p = e.plan or EngagementPlan(fee_basis="fixed", approach="substantive", currency="")
    nullable = (*DATES, "fee")
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if v is not None or k in nullable}
    changed = sorted(k for k, v in changes.items() if getattr(p, k) != v)
    if changed:
        for k in changed:
            setattr(p, k, changes[k])
        p.engagement = e  # only now, so saving nothing doesn't leave an empty record
        p.updated_by, p.updated_at = user.name, utcnow()
        session.add(p)
        log(session, user, "plan.update", "Updated the engagement letter and planning memo", e,
            details={"changed": changed})
        session.commit()
    return state(e)


def money(amount: float | None, currency: str = "") -> str:
    if amount is None:
        return ""
    return f"{currency} {amount:,.2f}".strip()


def letter(e: Engagement, language: str) -> list[tuple[str, str]]:
    t = LETTER[language]
    p = plan_out(e)
    entity = entity_name(e)
    framework = framework_text(e, language)
    end = docx.long_date(e.period_end, language)
    when = (docx.long_date(date.fromisoformat(p["letter_date"]), language)
            if p["letter_date"] else t["date"])
    out = [("title", t["title"]), ("line", when), ("line", ""),
           ("bold", t["to"].format(addressee=p["addressee"]
                                   or t["addressee"].format(entity=entity))),
           ("heading", t["scope_heading"]),
           ("text", t["scope"].format(entity=entity, end=end, framework=framework)),
           ("heading", t["ours_heading"]), ("text", t["ours"]),
           ("heading", t["mgmt_heading"]), ("text", t["mgmt"].format(framework=framework)),
           ("heading", t["report_heading"])]
    due = (t["report_due"].format(due=docx.long_date(date.fromisoformat(p["report_due"]),
                                                     language))
           if p["report_due"] else t["report_open"])
    out.append(("text", f"{due} {t['report_modified']}"))
    if p["other_services"]:
        out += [("heading", t["other_heading"]), ("text", p["other_services"])]
    out.append(("heading", t["fee_heading"]))
    if p["fee"] is None:
        out.append(("text", t["fee_none"]))
    else:
        out.append(("text", t[f"fee_{p['fee_basis']}"].format(fee=money(p["fee"],
                                                                       p["currency"]))))
    out += [("heading", t["agree_heading"]), ("text", t["agree"]),
            ("line", p["firm_name"] or t["firm"])]
    if p["partner_name"]:
        out.append(("line", t["partner"].format(partner=p["partner_name"])))
    out += [("line", p["city"] or t["city"]), ("line", ""),
            ("heading", t["accepted_heading"].format(entity=entity)),
            ("line", t["accepted_by"].format(name=p["accepted_by"] or t["blank"])),
            ("line", t["accepted_on"].format(
                date=docx.long_date(date.fromisoformat(p["accepted_on"]), language)
                if p["accepted_on"] else t["blank"]))]
    return out


def memo(e: Engagement, language: str) -> list[tuple[str, str]]:
    t = MEMO[language]
    p = plan_out(e)
    long = lambda d: docx.long_date(d, language)  # noqa: E731
    out = [("title", t["title"]),
           ("line", t["entity"].format(entity=entity_name(e))),
           ("line", t["period"].format(end=long(e.period_end))),
           ("line", t["framework"].format(framework=framework_text(e, language)))]
    if p["updated_by"]:
        out.append(("line", t["prepared"].format(name=p["updated_by"])))
    out += [("line", ""), ("heading", t["business_heading"]),
            ("text", p["business"] or t["business_placeholder"]),
            ("heading", t["acceptance_heading"])]
    a = e.acceptance
    if a is not None and a.decision == "accept":
        out.append(("text", t["accepted"].format(name=a.decided_by, date=long(a.decided_at))))
    elif a is not None and a.decision == "decline":
        out.append(("text", t["declined"]))
    else:
        out.append(("text", t["acceptance_open"]))
    out.append(("text", t["letter_accepted"].format(
        date=long(date.fromisoformat(p["accepted_on"]))) if p["accepted_on"]
        else t["letter_open"]))

    out.append(("heading", t["materiality_heading"]))
    if e.overall_materiality is None:
        out.append(("text", t["materiality_open"]))
    else:
        out += [("line", t["overall"].format(amount=money(e.overall_materiality))),
                ("line", t["performance"].format(amount=money(e.performance_materiality))),
                ("line", t["trivial"].format(amount=money(e.trivial_threshold))), ("line", "")]
        if e.benchmark and e.benchmark_amount is not None and e.benchmark_pct is not None:
            out.append(("text", t["basis"].format(
                pct=f"{e.benchmark_pct:g}", benchmark=t["benchmarks"].get(e.benchmark,
                                                                          e.benchmark),
                amount=money(e.benchmark_amount))))
        if e.materiality_rationale:
            out.append(("text", e.materiality_rationale))

    out.append(("heading", t["risks_heading"]))
    ranked = sorted(enumerate(e.risks, 1), key=lambda x: (LEVEL_RANK.get(x[1].level, 9), x[0]))
    shown = [(n, r) for n, r in ranked if r.level != "lower"][:MAX_MEMO_RISKS]
    if not e.risks:
        out.append(("text", t["risks_none"]))
    for n, r in shown:
        out.append(("subheading", t["risk"].format(
            ref=f"R{n}", title=r.title, level=t["levels"].get(r.level, r.level),
            area=area_label(r.area))))
        if r.response:
            out.append(("text", t["risk_response"].format(text=r.response)))
    if len(shown) < len(e.risks):
        out.append(("text", t["risks_more"].format(n=len(e.risks))))

    out += [("heading", t["approach_heading"]), ("text", t["approach"][p["approach"]])]
    if e.components:
        full = sum(1 for c in e.components if c.scope == "full")
        out.append(("text", t["group"].format(n=len(e.components), full=full)))

    out.append(("heading", t["team_heading"]))
    members = sorted(e.team, key=lambda m: m.user.name.lower())
    out += [("line", t["member"].format(name=m.user.name,
                                        role=t["roles"].get(m.user.role, m.user.role)))
            for m in members] or [("line", t["team_none"])]
    out.append(("line", ""))
    budget = sum(b.hours for b in e.time_budgets)
    out.append(("line", t["budget"].format(hours=f"{budget:,.1f}") if budget
                else t["budget_none"]))
    booked = schedule.booked_hours(e)
    if booked:
        out.append(("line", t["booked"].format(hours=f"{booked:,.1f}")))
    out += [("line", ""), ("heading", t["timetable_heading"])]
    if p["report_due"]:
        out.append(("line", t["report_due"].format(date=long(date.fromisoformat(
            p["report_due"])))))
    out.append(("text", p["timetable"] or t["timetable_placeholder"]))
    if p["other_notes"]:
        out += [("heading", t["other_heading"]), ("text", p["other_notes"])]
    return out


DOCUMENTS = {"letter": ("engagement-letter", letter, "Downloaded the engagement letter"),
             "memo": ("planning-memo", memo, "Downloaded the planning memo")}


def word_file(e: Engagement, kind: str, language: str = "en") -> tuple[str, bytes]:
    slug, build, _ = DOCUMENTS[kind]
    stem = safe_stem(f"{e.client.name}-{e.name}-{slug}-{language}")
    return f"{stem}.docx", docx.build(build(e, language), rtl=language == "ar")


@router.get("/engagements/{engagement_id}/planning/{document}")
def download(engagement_id: int, document: Literal["letter", "memo"],
             language: Literal["en", "ar"] = "en", user: User = Depends(current_user),
             session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = word_file(e, document, language)
    log(session, user, f"plan.download_{document}", DOCUMENTS[document][2], e,
        details={"language": language})
    session.commit()
    return Response(content, media_type=docx.MIME, headers=attachment(name))
