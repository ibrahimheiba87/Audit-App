"""Completing the audit: subsequent events, going concern, written representations and the
final review, with the partner's sign-off.

The team works through the completion checklist (ISA 560, 570, 580, 520, 450 and 260), logs
the events after the period end and how each was dealt with, concludes on going concern
with the warning signs the draft statements show beside it, and records when management's
representation letter was asked for and dated. The app drafts that letter, in English or
Arabic, from what the file already holds. A partner signs the completion off once nothing is
left open. Every change is logged, and the record exports to Excel.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import docx
from .auth import current_user, log, require
from .db import CompletionChecklist, Engagement, SubsequentEvent, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook
from .wording import LETTER

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

GROUPS = {
    "events": "Subsequent events (ISA 560)",
    "going_concern": "Going concern (ISA 570)",
    "representations": "Written representations (ISA 580)",
    "final": "Final review",
}
# (id, group, question, the answer that needs no further thought). Another answer is flagged
# for the partner, who can still sign off with a note that says why.
QUESTIONS = [
    ("se_minutes", "events", "Have we read the minutes of meetings of the owners, those "
                             "charged with governance and management held after the period "
                             "end?", "yes"),
    ("se_inquiry", "events", "Have we asked management whether any events after the period "
                             "end need adjusting or disclosing in the statements?", "yes"),
    ("se_interim", "events", "Have we read the latest interim figures, budgets and cash flow "
                             "forecasts after the period end?", "yes"),
    ("se_legal", "events", "Have we asked the client's lawyers about litigation and claims up "
                           "to a date close to our report?", "yes"),
    ("gc_assessment", "going_concern", "Has management assessed going concern for at least "
                                       "twelve months from the period end?", "yes"),
    ("gc_forecasts", "going_concern", "Have we evaluated management's cash flow forecasts and "
                                      "the assumptions behind them?", "yes"),
    ("gc_financing", "going_concern", "Are borrowing facilities in place and their covenants "
                                      "met?", "yes"),
    ("gc_doubt", "going_concern", "Are there events or conditions that may cast significant "
                                  "doubt on going concern (losses, net current liabilities, "
                                  "loan defaults, loss of a key customer)?", "no"),
    ("wr_responsibilities", "representations", "Has management confirmed in writing its "
                                               "responsibility for the statements and that it "
                                               "gave us all the information?", "yes"),
    ("wr_specific", "representations", "Do the representations cover the specific matters "
                                       "(related parties, subsequent events, uncorrected "
                                       "misstatements, litigation, estimates)?", "yes"),
    ("wr_date", "representations", "Is the letter dated as close as possible to, and not "
                                   "after, the date of our report?", "yes"),
    ("fa_analytics", "final", "Have we done final analytical procedures, and do the "
                              "statements agree with our understanding of the client?", "yes"),
    ("fa_misstatements", "final", "Have uncorrected misstatements been reported to management "
                                  "and those charged with governance?", "yes"),
    ("fa_disclosures", "final", "Have the disclosures been checked against the framework?",
     "yes"),
    ("fa_governance", "final", "Have significant findings been reported to those charged with "
                               "governance (ISA 260)?", "yes"),
]
QUESTION = {q: (group, text, expected) for q, group, text, expected in QUESTIONS}
ANSWERS = {"yes": "Yes", "no": "No", "na": "Not applicable"}
GOING_CONCERN = {
    "appropriate": "Going concern basis appropriate, no material uncertainty",
    "uncertainty_disclosed": "Material uncertainty, adequately disclosed",
    "uncertainty_not_disclosed": "Material uncertainty, not adequately disclosed",
    "inappropriate": "Going concern basis inappropriate",
}
KINDS = {"adjusting": "Adjusting event", "non_adjusting": "Non-adjusting event"}
TREATMENTS = {"pending": "To decide", "adjusted": "Adjusted in the statements",
              "disclosed": "Disclosed in the notes", "not_needed": "No change needed"}
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                               max_length=5000)]


class AnswerIn(BaseModel):
    answer: Literal["yes", "no", "na"] | None = None
    comment: Text = ""


class CompletionIn(BaseModel):
    """Only the fields sent are changed."""
    answers: dict[str, AnswerIn] = Field(default={}, max_length=len(QUESTIONS))
    conclusion: Text | None = None
    going_concern: Literal["appropriate", "uncertainty_disclosed", "uncertainty_not_disclosed",
                           "inappropriate"] | None = None
    going_concern_note: Text | None = None
    reps_requested_on: date | None = None
    reps_dated: date | None = None


class EventIn(BaseModel):
    occurred_on: date | None = None
    description: Description
    kind: Literal["adjusting", "non_adjusting"] = "non_adjusting"
    amount: float | None = Field(default=None, allow_inf_nan=False)
    treatment: Literal["pending", "adjusted", "disclosed", "not_needed"] = "pending"
    note: Text = ""


class EventPatch(BaseModel):
    occurred_on: date | None = None
    description: Description | None = None
    kind: Literal["adjusting", "non_adjusting"] | None = None
    amount: float | None = Field(default=None, allow_inf_nan=False)
    treatment: Literal["pending", "adjusted", "disclosed", "not_needed"] | None = None
    note: Text | None = None


class SignOffIn(BaseModel):
    note: Text = ""


def answered(c: CompletionChecklist | None) -> dict[str, dict]:
    raw = (c.answers if c else None) or {}
    return {q: {"answer": (raw.get(q) or {}).get("answer"),
                "comment": (raw.get(q) or {}).get("comment", "")} for q in QUESTION}


def flags(answers: dict[str, dict]) -> list[str]:
    """Questions answered other than the reassuring way (not applicable is fine)."""
    return [q for q, (_, _, expected) in QUESTION.items()
            if answers[q]["answer"] not in (None, expected, "na")]


def event_out(x: SubsequentEvent) -> dict:
    return {"id": x.id, "occurred_on": x.occurred_on.isoformat() if x.occurred_on else None,
            "description": x.description, "kind": x.kind, "kind_label": KINDS[x.kind],
            "amount": x.amount, "treatment": x.treatment,
            "treatment_label": TREATMENTS[x.treatment], "note": x.note,
            "created_by": x.created_by, "created_at": x.created_at.isoformat(),
            "updated_by": x.updated_by, "updated_at": x.updated_at.isoformat()}


def check_event(x: SubsequentEvent, e: Engagement) -> None:
    if x.occurred_on is not None and x.occurred_on <= e.period_end:
        raise HTTPException(400, "A subsequent event happens after the period end "
                                 f"({e.period_end.isoformat()}).")
    if x.kind == "non_adjusting" and x.treatment == "adjusted":
        raise HTTPException(400, "A non-adjusting event isn't booked in the statements: "
                                 "disclose it, or say no change is needed.")


def blockers(e: Engagement, answers: dict[str, dict]) -> list[str]:
    """What stops the partner signing off."""
    c = e.completion
    out = []
    missing = sum(1 for a in answers.values() if not a["answer"])
    if missing:
        out.append(f"Answer every question ({missing} left).")
    if not c or not c.going_concern:
        out.append("Conclude on going concern.")
    if not c or not c.reps_dated:
        out.append("Enter the date of the representation letter.")
    pending = sum(1 for x in e.subsequent_events if x.treatment == "pending")
    if pending:
        out.append(f"Decide how to deal with {pending} subsequent "
                   f"{'event' if pending == 1 else 'events'}.")
    return out


def going_concern_signs(session: Session, e: Engagement) -> dict:
    """Warning signs in the draft statements, and whether uncorrected misstatements are
    material, to look at beside the going concern conclusion."""
    from .dashboard import statements_state  # dashboard imports this module
    fs = statements_state(session, e)
    if not fs or "error" in fs:
        return {"warnings": [], "uncorrected": None, "statements": bool(fs)}
    return {"warnings": [w["text"] for w in fs["warnings"]], "uncorrected": fs["uncorrected"],
            "statements": True}


def state(session: Session, e: Engagement) -> dict:
    c = e.completion
    answers = answered(c)
    signed_at = c.signed_at if c else None
    events = [event_out(x) for x in e.subsequent_events]
    return {
        "groups": [{"id": g, "label": label} for g, label in GROUPS.items()],
        "questions": [{"id": q, "group": g, "text": text, "expected": expected}
                      for q, g, text, expected in QUESTIONS],
        "answers": answers,
        "answered": sum(1 for a in answers.values() if a["answer"]),
        "flags": flags(answers),
        "conclusion": c.conclusion if c else "",
        "going_concern": c.going_concern if c else None,
        "going_concern_label": GOING_CONCERN.get(c.going_concern) if c and c.going_concern
        else None,
        "going_concern_note": c.going_concern_note if c else "",
        "going_concern_choices": [{"id": k, "label": v} for k, v in GOING_CONCERN.items()],
        "signs": going_concern_signs(session, e),
        "reps_requested_on": c.reps_requested_on.isoformat() if c and c.reps_requested_on
        else None,
        "reps_dated": c.reps_dated.isoformat() if c and c.reps_dated else None,
        "events": events,
        "event_kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
        "treatments": [{"id": k, "label": v} for k, v in TREATMENTS.items()],
        "pending_events": sum(1 for x in events if x["treatment"] == "pending"),
        "updated_by": c.updated_by if c else "",
        "updated_at": c.updated_at.isoformat() if c else None,
        "signed_by": c.signed_by if c else "",
        "signed_at": signed_at.isoformat() if signed_at else None,
        "signed_note": c.signed_note if c else "",
        # Changed after the partner signed off, so the sign-off may need a look.
        "changed_since_signoff": stale(e),
        "blockers": blockers(e, answers),
    }


def stale(e: Engagement) -> bool:
    """Whether the checklist or an event changed after the partner signed off."""
    c = e.completion
    return bool(c and c.signed_at and (
        c.updated_at > c.signed_at or any(x.updated_at > c.signed_at
                                          for x in e.subsequent_events)))


def summary(e: Engagement) -> dict:
    """For the dashboard and the report draft: where completion stands."""
    c = e.completion
    return {"started": c is not None or bool(e.subsequent_events),
            "signed": bool(c and c.signed_at) and not stale(e),
            "going_concern": c.going_concern if c else None,
            "reps_dated": c.reps_dated if c else None, "signed_at": c.signed_at if c else None,
            "pending_events": sum(1 for x in e.subsequent_events if x.treatment == "pending"),
            "doubt": bool(c and (c.answers.get("gc_doubt") or {}).get("answer") == "yes")}


def touch(session: Session, e: Engagement, user: User) -> CompletionChecklist:
    c = e.completion or CompletionChecklist(engagement=e)
    c.updated_by, c.updated_at = user.name, utcnow()
    session.add(c)
    return c


@router.get("/engagements/{engagement_id}/completion")
def get_completion(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    return state(session, get_or_404(session, Engagement, engagement_id, user))


@router.put("/engagements/{engagement_id}/completion")
def save_completion(engagement_id: int, body: CompletionIn,
                    user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    unknown = sorted(set(body.answers) - set(QUESTION))
    if unknown:
        raise HTTPException(400, f"Unknown question: {unknown[0]}.")
    e = held_engagement(session, user, engagement_id, f"completion:{engagement_id}")
    c = e.completion
    before = answered(c)
    # Only the parts sent change: a comment on its own keeps the answer.
    answers = {**before, **{q: {**before[q], **a.model_dump(exclude_unset=True)}
                            for q, a in body.answers.items()}}
    changed = sorted(q for q in QUESTION if answers[q] != before[q])
    sent = body.model_fields_set - {"answers"}
    fields = {"conclusion": "", "going_concern": None, "going_concern_note": "",
              "reps_requested_on": None, "reps_dated": None}
    new = {}
    for f, default in fields.items():
        value = getattr(body, f)
        # The going concern choice and the dates can be cleared with null; text sent as null
        # stays as it is.
        keep = f not in sent or (value is None and f in ("conclusion", "going_concern_note"))
        new[f] = (getattr(c, f) if c else default) if keep else value
    if new["going_concern"] not in (None, "appropriate") and not new["going_concern_note"]:
        raise HTTPException(400, "Describe the events or conditions behind the going concern "
                                 "conclusion.")
    for f, label in (("reps_requested_on", "asked for"), ("reps_dated", "dated")):
        if new[f] is not None and new[f] <= e.period_end:
            raise HTTPException(400, f"The representation letter can't be {label} before the "
                                     "period end.")
    if new["reps_dated"] and new["reps_requested_on"] \
            and new["reps_dated"] < new["reps_requested_on"]:
        raise HTTPException(400, "The representation letter can't be dated before it was "
                                 "asked for.")
    changed_fields = sorted(f for f in fields if new[f] != (getattr(c, f) if c else fields[f]))
    if changed or changed_fields:  # saving without a change leaves the sign-off alone
        c = touch(session, e, user)
        c.answers = {q: a for q, a in answers.items() if a["answer"] or a["comment"]}
        for f in fields:
            setattr(c, f, new[f])
        log(session, user, "completion.update", "Updated the completion checklist", e,
            details={"changed": changed, "answers": {q: answers[q]["answer"] for q in changed},
                     "fields": changed_fields, "going_concern": new["going_concern"]})
        session.commit()
    return state(session, e)


@router.post("/engagements/{engagement_id}/completion/events")
def add_event(engagement_id: int, body: EventIn, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"completion:{engagement_id}")
    x = SubsequentEvent(engagement=e, **body.model_dump(), created_by=user.name,
                        updated_by=user.name)
    check_event(x, e)
    session.add(x)
    session.flush()
    log(session, user, "completion.event", f"Added a subsequent event: {x.description[:80]}", e,
        details={"event_id": x.id, "kind": x.kind, "treatment": x.treatment,
                 "amount": x.amount})
    session.commit()
    return state(session, e)


def held_event(session: Session, user: User, event_id: int) -> SubsequentEvent:
    x = get_or_404(session, SubsequentEvent, event_id, user)
    held_engagement(session, user, x.engagement_id, f"completion:{x.engagement_id}")
    return get_or_404(session, SubsequentEvent, event_id, user)


@router.patch("/subsequent-events/{event_id}")
def update_event(event_id: int, body: EventPatch, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    x = held_event(session, user, event_id)
    changes = body.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if v is not None or k in ("occurred_on",
                                                                           "amount")}
    for field, value in changes.items():
        setattr(x, field, value)
    check_event(x, x.engagement)
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "completion.event",
        f"Changed the subsequent event: {x.description[:80]}", x.engagement,
        details={"event_id": x.id, "changed": sorted(changes), "kind": x.kind,
                 "treatment": x.treatment, "amount": x.amount})
    session.commit()
    return state(session, x.engagement)


@router.delete("/subsequent-events/{event_id}")
def delete_event(event_id: int, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> dict:
    x = held_event(session, user, event_id)
    e = x.engagement
    log(session, user, "completion.event", f"Removed the subsequent event: "
        f"{x.description[:80]}", e, details={"event_id": x.id, "removed": True})
    session.delete(x)
    if e.completion is not None:  # so a signed completion shows it changed
        e.completion.updated_by, e.completion.updated_at = user.name, utcnow()
    session.commit()
    session.refresh(e)
    return state(session, e)


@router.post("/engagements/{engagement_id}/completion/signoff")
def sign_off(engagement_id: int, body: SignOffIn, user: User = Depends(require("manage")),
             session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"completion:{engagement_id}")
    answers = answered(e.completion)
    left = blockers(e, answers)
    if left:
        raise HTTPException(400, left[0])
    c = e.completion
    if flags(answers) and not body.note:
        raise HTTPException(400, "Some answers need a second look. Say in the note why the "
                                 "completion can still be signed off.")
    c.signed_by, c.signed_at, c.signed_note = user.name, utcnow(), body.note
    log(session, user, "completion.signoff", "Signed off the completion procedures", e,
        details={"flags": flags(answers), "going_concern": c.going_concern, "note": body.note})
    session.commit()
    return state(session, e)


# --- The representation letter --------------------------------------------------------

def amount(x: float) -> str:
    return f"{x:,.2f}"


def uncorrected_entries(session: Session, e: Engagement) -> dict | None:
    """The uncorrected misstatements from the lead sheets, or None without a usable trial
    balance."""
    from .leadsheets import compute
    try:
        data, tb, _ = compute(session, e, None, None)
    except HTTPException:
        return None
    return data["uncorrected"] if tb is not None else None


def letter_paragraphs(session: Session, e: Engagement, language: str) -> list[tuple[str, str]]:
    from .report_draft import entity_name, framework_text  # it imports this module
    t = LETTER[language]
    c = e.completion
    framework = framework_text(e, language)
    firm = (e.audit_report.firm_name if e.audit_report else "") or t["firm"]
    dated = docx.long_date(c.reps_dated, language) if c and c.reps_dated else t["no_date"]
    out = [("title", t["title"]), ("line", t["to"].format(firm=firm)),
           ("text", t["date"].format(date=dated)),
           ("text", t["intro"].format(entity=entity_name(e), end=docx.long_date(e.period_end,
                                                                              language),
                                      framework=framework)),
           ("text", t["confirm"]), ("heading", t["fs"])]
    out += [("text", "• " + item.format(framework=framework)) for item in t["fs_items"]]
    events = [x for x in e.subsequent_events if x.treatment != "not_needed"]
    if events:
        out.append(("text", t["events"]))
        out += [("line", "   - " + t["event_line"].format(
            date=f"{x.occurred_on.isoformat()}: " if x.occurred_on else "",
            description=x.description, kind=t["kinds"][x.kind],
            treatment=t["treatments"][x.treatment])) for x in events]
        out.append(("line", ""))
    un = uncorrected_entries(session, e)
    if un and un["entries"]:
        out.append(("text", "• " + t["uncorrected"]))
    out.append(("heading", t["info"]))
    out += [("text", "• " + item) for item in t["info_items"]]
    out.append(("text", t["close"]))
    out += [("text", s) for s in t["signatures"]]
    if un and un["entries"]:
        out.append(("heading", t["schedule"]))
        out += [("line", t["schedule_line"].format(
            ref=i["ref"], description=i["description"], profit=amount(i["profit"]),
            net_assets=amount(i["net_assets"]))) for i in un["entries"]]
        out.append(("bold", t["schedule_total"].format(profit=amount(un["profit"]),
                                                       net_assets=amount(un["net_assets"]))))
    return out


@router.get("/engagements/{engagement_id}/completion/representation-letter")
def representation_letter(engagement_id: int, language: Literal["en", "ar"] = "en",
                          user: User = Depends(current_user),
                          session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    content = docx.build(letter_paragraphs(session, e, language), rtl=language == "ar")
    log(session, user, "completion.download", "Downloaded the draft representation letter", e,
        details={"language": language})
    session.commit()
    stem = safe_stem(f"{e.client.name}-{e.name}-representation-letter-{language}")
    return Response(content, media_type=docx.MIME, headers=attachment(f"{stem}.docx"))


# --- Excel ----------------------------------------------------------------------------

def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    """The completion record as an Excel file, and its name."""
    s = state(session, e)
    about = [
        ("Client", e.client.name), ("Engagement", e.name),
        ("Period end", e.period_end.isoformat()),
        ("Going concern", s["going_concern_label"] or "Not yet concluded"),
        ("Going concern note", s["going_concern_note"]),
        ("Warning signs in the draft statements", "\n".join(s["signs"]["warnings"])),
        ("Uncorrected misstatements", s["signs"]["uncorrected"] or ""),
        ("Representation letter asked for", s["reps_requested_on"] or ""),
        ("Representation letter dated", s["reps_dated"] or ""),
        ("Conclusion", s["conclusion"]),
        ("Signed off by", s["signed_by"] or "Not yet signed off"),
        ("Signed off at", f"{s['signed_at'][:16].replace('T', ' ')} UTC"
         if s["signed_at"] else ""),
        ("Sign-off note", s["signed_note"]),
        ("Still open", "\n".join(s["blockers"])),
    ]
    checklist = [{"Area": GROUPS[g], "Question": text,
                  "Answer": ANSWERS.get(s["answers"][q]["answer"], ""),
                  "Comment": s["answers"][q]["comment"],
                  "Needs a second look": "Yes" if q in s["flags"] else ""}
                 for q, g, text, _ in QUESTIONS]
    events = [{"Date": x["occurred_on"] or "", "Event": x["description"],
               "Type": x["kind_label"], "Amount": x["amount"],
               "Treatment": x["treatment_label"], "Note": x["note"],
               "Added by": x["created_by"], "Last changed by": x["updated_by"]}
              for x in s["events"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(checklist).to_excel(xl, sheet_name="Completion checklist", index=False)
        pd.DataFrame(events, columns=["Date", "Event", "Type", "Amount", "Treatment", "Note",
                                      "Added by", "Last changed by"]).to_excel(
            xl, sheet_name="Subsequent events", index=False)
        finish_workbook(xl.book)
    return safe_stem(f"{e.client.name}-{e.name}-completion") + ".xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/completion/export")
def export_completion(engagement_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(session, e)
    log(session, user, "completion.download", "Downloaded the completion record", e)
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))
