"""The draft independent auditor's report.

The team picks the opinion (unmodified, qualified, adverse or a disclaimer), writes the basis
for a modified one, and adds any emphasis of matter, key audit matters and other matter. The
app suggests the opinion from the file: the going concern conclusion and whether uncorrected
misstatements are material. It warns when the draft and the file disagree, for example a
report dated before the representation letter. The draft downloads as a Word file in English or
Arabic, with short template sentences for the team to finish.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import completion, docx
from .auth import current_user, log, require
from .db import AuditReport, Engagement, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment
from .report_wording import REPORT

router = APIRouter(prefix="/api")
OPINIONS = {"unmodified": "Unmodified", "qualified": "Qualified", "adverse": "Adverse",
            "disclaimer": "Disclaimer of opinion"}
SEVERITY = {"unmodified": 0, "qualified": 1, "adverse": 2, "disclaimer": 2}
MAX_ITEMS = 10
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class KeyMatter(BaseModel):
    title: Short
    why: Text = ""
    how: Text = ""


class ReportIn(BaseModel):
    """Only the fields sent change; the report date can be cleared with null."""
    opinion: Literal["unmodified", "qualified", "adverse", "disclaimer"] | None = None
    basis: Text | None = None
    entity_name: Short | None = None
    addressee: Short | None = None
    framework_name: Short | None = None
    emphasis: list[Text] | None = Field(default=None, max_length=MAX_ITEMS)
    key_matters: list[KeyMatter] | None = Field(default=None, max_length=MAX_ITEMS)
    other_matter: Text | None = None
    firm_name: Short | None = None
    partner_name: Short | None = None
    city: Short | None = None
    report_date: date | None = None


def entity_name(e: Engagement) -> str:
    return (e.audit_report.entity_name if e.audit_report else "") or e.client.name


def framework_text(e: Engagement, language: str) -> str:
    if e.audit_report and e.audit_report.framework_name:
        return e.audit_report.framework_name
    t = REPORT[language]
    return t["frameworks"].get(e.framework, t["framework_other"])


def suggestion(session: Session, e: Engagement) -> dict:
    """The opinion the file points to, and why. The partner decides; this is a prompt."""
    c = completion.summary(e)
    reasons, opinion = [], "unmodified"

    def worse(to: str, why: str) -> None:
        nonlocal opinion
        reasons.append(why)
        if SEVERITY[to] > SEVERITY[opinion]:
            opinion = to

    gc = c["going_concern"]
    if gc == "inappropriate":
        worse("adverse", "The going concern basis is inappropriate, so the opinion is adverse.")
    elif gc == "uncertainty_not_disclosed":
        worse("qualified", "The going concern uncertainty isn't adequately disclosed: a "
                           "qualified or adverse opinion.")
    signs = completion.going_concern_signs(session, e)
    if (signs["uncorrected"] or "").startswith("Uncorrected misstatements are material"):
        worse("qualified", "Uncorrected misstatements are material: a qualified opinion, or "
                           "adverse if their effect is pervasive.")
    return {"opinion": opinion, "label": OPINIONS[opinion], "reasons": reasons,
            "going_concern_section": gc == "uncertainty_disclosed"}


def warnings(e: Engagement, r: AuditReport | None, suggested: dict) -> list[str]:
    c = completion.summary(e)
    out = []
    opinion = r.opinion if r else "unmodified"
    if SEVERITY[opinion] < SEVERITY[suggested["opinion"]]:
        out.append(f"The file points to a {suggested['label'].lower()} opinion, but the draft "
                   f"is {OPINIONS[opinion].lower()}.")
    if opinion != "unmodified" and not (r and r.basis):
        out.append("Describe the basis for the modified opinion.")
    if r and r.report_date:
        if r.report_date <= e.period_end:
            out.append("The report date must be after the period end.")
        if c["reps_dated"] and c["reps_dated"] > r.report_date:
            out.append("The representation letter is dated after the report; it should be "
                       "dated as close as possible to, and not after, the report.")
        if not c["signed"]:
            out.append("The completion procedures aren't signed off yet.")
    if c["pending_events"]:
        out.append("Some subsequent events still need a decision.")
    if not c["going_concern"]:
        out.append("Conclude on going concern in the completion checklist.")
    return out


def report_out(e: Engagement) -> dict:
    r = e.audit_report
    return {
        "opinion": r.opinion if r else "unmodified", "basis": r.basis if r else "",
        "entity_name": entity_name(e), "addressee": r.addressee if r else "",
        "framework_name": r.framework_name if r else "",
        "emphasis": list(r.emphasis or []) if r else [],
        "key_matters": list(r.key_matters or []) if r else [],
        "other_matter": r.other_matter if r else "", "firm_name": r.firm_name if r else "",
        "partner_name": r.partner_name if r else "", "city": r.city if r else "",
        "report_date": r.report_date.isoformat() if r and r.report_date else None,
        "updated_by": r.updated_by if r else "",
        "updated_at": r.updated_at.isoformat() if r else None,
    }


def state(session: Session, e: Engagement) -> dict:
    suggested = suggestion(session, e)
    return {**report_out(e), "opinions": [{"id": k, "label": v} for k, v in OPINIONS.items()],
            "suggestion": suggested, "warnings": warnings(e, e.audit_report, suggested)}


@router.get("/engagements/{engagement_id}/report")
def get_report(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    return state(session, get_or_404(session, Engagement, engagement_id, user))


@router.put("/engagements/{engagement_id}/report")
def save_report(engagement_id: int, body: ReportIn, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"report:{engagement_id}")
    r = e.audit_report or AuditReport()
    changes = body.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if v is not None or k == "report_date"}
    if "entity_name" in changes and changes["entity_name"] == e.client.name:
        changes["entity_name"] = ""  # the client's name, kept in step if it is renamed
    changed = sorted(k for k, v in changes.items() if getattr(r, k) != v)
    if changed:
        for k in changed:
            setattr(r, k, changes[k])
        r.engagement = e  # only now, so saving nothing doesn't leave a half-made report
        r.updated_by, r.updated_at = user.name, utcnow()
        session.add(r)
        log(session, user, "report.update", "Updated the draft auditor's report", e,
            details={"changed": changed, "opinion": r.opinion})
        session.commit()
    return state(session, e)


def paragraphs(session: Session, e: Engagement, language: str) -> list[tuple[str, str]]:
    t = REPORT[language]
    r = report_out(e)
    c = e.completion
    opinion = r["opinion"]
    entity = r["entity_name"]
    framework = framework_text(e, language)
    end = docx.long_date(e.period_end, language)
    out = [("title", t["title"]),
           ("bold", t["to"].format(addressee=r["addressee"]
                                   or t["addressee"].format(entity=entity))),
           ("heading", t["opinion_heading"][opinion]),
           ("text", t["scope"].format(entity=entity, end=end)),
           ("text", t["opinion"][opinion].format(end=end, framework=framework)),
           ("heading", t["basis_heading"][opinion])]
    if opinion != "unmodified":
        out.append(("text", r["basis"] or t["basis_placeholder"]))
    if opinion != "disclaimer":
        out.append(("text", t["basis"]))
        if c and c.going_concern == "uncertainty_disclosed":
            out += [("heading", t["gc_heading"]),
                    ("text", t["gc"].format(note=c.going_concern_note.rstrip(".")))]
        for text in r["emphasis"]:
            out += [("heading", t["emphasis_heading"]), ("text", f"{text} {t['emphasis_end']}")]
        if r["key_matters"]:
            out += [("heading", t["kam_heading"]), ("text", t["kam_intro"])]
            for k in r["key_matters"]:
                out.append(("subheading", k["title"]))
                out += [("text", t[f"kam_{p}"].format(text=k[p])) for p in ("why", "how")
                        if k[p]]
    if r["other_matter"]:
        out += [("heading", t["other_heading"]), ("text", r["other_matter"])]
    out += [("heading", t["mgmt_heading"]), ("text", t["mgmt"].format(framework=framework)),
            ("heading", t["auditor_heading"]),
            ("text", t["auditor_disclaimer"] if opinion == "disclaimer" else t["auditor"])]
    if r["partner_name"]:
        out.append(("text", t["partner"].format(partner=r["partner_name"])))
    dated = docx.long_date(date.fromisoformat(r["report_date"]), language) \
        if r["report_date"] else t["date"]
    out += [("line", r["firm_name"] or t["firm"]), ("line", r["city"] or t["city"]),
            ("line", dated)]
    return out


@router.get("/engagements/{engagement_id}/report/preview")
def preview(engagement_id: int, language: Literal["en", "ar"] = "en",
            user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    return {"language": language,
            "paragraphs": [{"style": s, "text": x} for s, x in paragraphs(session, e, language)]}


@router.get("/engagements/{engagement_id}/report/download")
def download(engagement_id: int, language: Literal["en", "ar"] = "en",
             user: User = Depends(current_user),
             session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = word_file(session, e, language)
    log(session, user, "report.download", "Downloaded the draft auditor's report", e,
        details={"language": language})
    session.commit()
    return Response(content, media_type=docx.MIME, headers=attachment(name))


def word_file(session: Session, e: Engagement, language: str = "en") -> tuple[str, bytes]:
    stem = safe_stem(f"{e.client.name}-{e.name}-auditor-report-draft-{language}")
    return f"{stem}.docx", docx.build(paragraphs(session, e, language), rtl=language == "ar")
