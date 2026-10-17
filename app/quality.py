"""The engagement quality review (ISQM 2, ISA 220): an objective look at the significant
judgments the team made and the conclusions it reached, by a partner named for it, before
the report is signed.

A partner or admin says whether the engagement needs one, why, and who reviews it. The
reviewer answers the checklist, writes a conclusion and signs off; only the named reviewer
can. A change afterwards shows the sign-off as needing a fresh look. Every change is logged,
and the record exports to Excel.
"""

from __future__ import annotations

import io
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user, log, require
from .db import Engagement, QualityReview, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

REASONS = {"listed": "Listed entity", "public_interest": "Public interest entity",
           "risk": "Higher risk engagement", "law": "Required by law or regulation",
           "firm_policy": "Firm policy", "other": "Other reason"}
# (id, question, the answer that needs no further thought).
QUESTIONS = [
    ("risks", "Do the planned responses address the significant risks?", "yes"),
    ("materiality", "Is materiality reasonable for the entity and its users?", "yes"),
    ("judgments", "Are the significant judgments supported by the work on file?", "yes"),
    ("misstatements", "Is the conclusion on uncorrected misstatements reasonable?", "yes"),
    ("independence", "Has the team confirmed its independence, with threats dealt with?",
     "yes"),
    ("consultation", "Were difficult matters consulted on, with the conclusions recorded?",
     "yes"),
    ("unresolved", "Are any matters raised by the team still unresolved?", "no"),
    ("disclosures", "Are the disclosures appropriate, given what the file shows?", "yes"),
    ("report", "Is the proposed auditor's report appropriate?", "yes"),
    ("governance", "Were significant matters reported to those charged with governance?",
     "yes"),
]
QUESTION = {q: (text, expected) for q, text, expected in QUESTIONS}
ANSWERS = {"yes": "Yes", "no": "No", "na": "Not applicable"}
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class AnswerIn(BaseModel):
    answer: Literal["yes", "no", "na"] | None = None
    comment: Text = ""


class SetupIn(BaseModel):
    """Only the fields sent are changed."""
    required: bool | None = None
    reason: Literal["listed", "public_interest", "risk", "law", "firm_policy", "other"] | None \
        = None
    reason_note: Text | None = None
    reviewer_id: int | None = None


class ReviewIn(BaseModel):
    answers: dict[str, AnswerIn] = Field(default={}, max_length=len(QUESTIONS))
    conclusion: Text | None = None


class SignOffIn(BaseModel):
    note: Text = ""


def answered(q: QualityReview | None) -> dict[str, dict]:
    raw = (q.answers if q else None) or {}
    return {k: {"answer": (raw.get(k) or {}).get("answer"),
                "comment": (raw.get(k) or {}).get("comment", "")} for k in QUESTION}


def flags(answers: dict[str, dict]) -> list[str]:
    return [k for k, (_, expected) in QUESTION.items()
            if answers[k]["answer"] not in (None, expected, "na")]


def stale(q: QualityReview | None) -> bool:
    return bool(q and q.signed_at and q.updated_at > q.signed_at)


def blockers(q: QualityReview | None, answers: dict[str, dict]) -> list[str]:
    out = []
    if not q or not q.required:
        out.append("Say that this engagement needs a quality review first.")
        return out
    if q.reviewer_id is None:
        out.append("Name the quality reviewer.")
    missing = sum(1 for a in answers.values() if not a["answer"])
    if missing:
        out.append(f"Answer every question ({missing} left).")
    if not q.conclusion:
        out.append("Write the reviewer's conclusion.")
    return out


def reviewers(session: Session) -> list[User]:
    """Partners and admins who can be named as the quality reviewer."""
    return list(session.scalars(select(User).where(User.role.in_(("partner", "admin")),
                                                   User.active.is_(True))
                                .order_by(User.name)))


def state(session: Session, e: Engagement, user: User) -> dict:
    q = e.quality_review
    answers = answered(q)
    on_team = {m.user_id for m in e.team}
    return {
        "engagement_id": e.id,
        "required": bool(q and q.required),
        "reason": q.reason if q else None,
        "reason_label": REASONS.get(q.reason) if q and q.reason else None,
        "reason_note": q.reason_note if q else "",
        "reasons": [{"id": k, "label": v} for k, v in REASONS.items()],
        "reviewer_id": q.reviewer_id if q else None,
        "reviewer_name": q.reviewer_name if q else "",
        # ISQM 2: the reviewer is not a member of the engagement team.
        "reviewer_on_team": bool(q and q.reviewer_id in on_team),
        "reviewers": [{"id": u.id, "name": u.name, "on_team": u.id in on_team}
                      for u in reviewers(session)],
        "questions": [{"id": k, "text": text, "expected": expected}
                      for k, text, expected in QUESTIONS],
        "answers": answers,
        "answered": sum(1 for a in answers.values() if a["answer"]),
        "flags": flags(answers),
        "conclusion": q.conclusion if q else "",
        "updated_by": q.updated_by if q else "",
        "updated_at": q.updated_at.isoformat() if q else None,
        "signed_by": q.signed_by if q else "",
        "signed_at": q.signed_at.isoformat() if q and q.signed_at else None,
        "signed_note": q.signed_note if q else "",
        "changed_since_signoff": stale(q),
        "blockers": blockers(q, answers),
        "is_reviewer": bool(q and q.reviewer_id == user.id),
    }


def summary(e: Engagement) -> dict:
    """For the dashboard: whether a review is needed, and done."""
    q = e.quality_review
    return {"required": bool(q and q.required), "named": bool(q and q.reviewer_id),
            "signed": bool(q and q.signed_at) and not stale(q),
            "stale": stale(q)}


def touch(e: Engagement, user: User) -> QualityReview:
    q = e.quality_review or QualityReview(engagement=e)
    q.updated_by, q.updated_at = user.name, utcnow()
    return q


@router.get("/engagements/{engagement_id}/quality-review")
def get_review(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    return state(session, get_or_404(session, Engagement, engagement_id, user), user)


@router.put("/engagements/{engagement_id}/quality-review/setup")
def setup(engagement_id: int, body: SetupIn, user: User = Depends(require("manage")),
          session: Session = Depends(get_session)) -> dict:
    """Whether a review is needed, why, and who does it: a partner's or admin's call."""
    e = held_engagement(session, user, engagement_id, f"quality:{engagement_id}")
    q = e.quality_review
    sent = body.model_fields_set
    new = {"required": bool(q and q.required), "reason": q.reason if q else None,
           "reason_note": q.reason_note if q else "", "reviewer_id": q.reviewer_id if q else None}
    for f in sent:
        value = getattr(body, f)
        if f == "required" and value is None:
            continue
        new[f] = "" if f == "reason_note" and value is None else value
    reviewer = None
    if new["reviewer_id"] is not None:
        reviewer = session.get(User, new["reviewer_id"])
        if reviewer is None or reviewer.role not in ("partner", "admin") or not reviewer.active:
            raise HTTPException(400, "The quality reviewer must be an active partner or admin.")
    if new["required"] and not new["reason"]:
        raise HTTPException(400, "Say why the engagement needs a quality review.")
    changed = sorted(f for f in new if new[f] != (getattr(q, f) if q else
                                                   {"required": False, "reason": None,
                                                    "reason_note": "", "reviewer_id": None}[f]))
    if changed:
        q = touch(e, user)
        for f, v in new.items():
            setattr(q, f, v)
        q.reviewer_name = reviewer.name if reviewer else ""
        if "reviewer_id" in changed and q.signed_at:  # a new reviewer signs afresh
            q.signed_by, q.signed_at, q.signed_note = "", None, ""
        session.add(q)
        log(session, user, "quality.setup", "Set up the quality review", e,
            details={"changed": changed, "required": q.required, "reason": q.reason,
                     "reviewer": q.reviewer_name})
        session.commit()
    return state(session, e, user)


@router.put("/engagements/{engagement_id}/quality-review")
def save_review(engagement_id: int, body: ReviewIn, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> dict:
    unknown = sorted(set(body.answers) - set(QUESTION))
    if unknown:
        raise HTTPException(400, f"Unknown question: {unknown[0]}.")
    e = held_engagement(session, user, engagement_id, f"quality:{engagement_id}")
    q = e.quality_review
    if not q or not q.required:
        raise HTTPException(400, "Say that this engagement needs a quality review first.")
    if q.reviewer_id != user.id and not auth.can(user, "admin"):
        raise HTTPException(403, "Only the quality reviewer fills in the quality review.")
    before = answered(q)
    answers = {**before, **{k: {**before[k], **a.model_dump(exclude_unset=True)}
                            for k, a in body.answers.items()}}
    changed = sorted(k for k in QUESTION if answers[k] != before[k])
    conclusion = q.conclusion if body.conclusion is None else body.conclusion
    new_conclusion = conclusion != q.conclusion
    if changed or new_conclusion:  # saving without a change leaves the sign-off alone
        touch(e, user)
        q.answers = {k: a for k, a in answers.items() if a["answer"] or a["comment"]}
        q.conclusion = conclusion
        log(session, user, "quality.update", "Updated the quality review", e,
            details={"changed": changed, "answers": {k: answers[k]["answer"] for k in changed},
                     "conclusion_changed": new_conclusion})
        session.commit()
    return state(session, e, user)


@router.post("/engagements/{engagement_id}/quality-review/signoff")
def sign_off(engagement_id: int, body: SignOffIn, user: User = Depends(require("manage")),
             session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"quality:{engagement_id}")
    q = e.quality_review
    answers = answered(q)
    left = blockers(q, answers)
    if left:
        raise HTTPException(400, left[0])
    if q.reviewer_id != user.id:
        raise HTTPException(403, f"Only the quality reviewer, {q.reviewer_name}, can sign the "
                                 "quality review off.")
    if flags(answers) and not body.note:
        raise HTTPException(400, "Some answers need a second look. Say in the note why the "
                                 "quality review can still be signed off.")
    q.signed_by, q.signed_at, q.signed_note = user.name, utcnow(), body.note
    log(session, user, "quality.signoff", "Signed off the quality review", e,
        details={"flags": flags(answers), "note": body.note})
    session.commit()
    return state(session, e, user)


@router.delete("/engagements/{engagement_id}/quality-review/signoff")
def take_back(engagement_id: int, user: User = Depends(require("manage")),
              session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"quality:{engagement_id}")
    q = e.quality_review
    if not q or not q.signed_at:
        raise HTTPException(400, "The quality review isn't signed off.")
    if q.reviewer_id != user.id and not auth.can(user, "admin"):
        raise HTTPException(403, "Only the quality reviewer or an admin can take the sign-off "
                                 "back.")
    q.signed_by, q.signed_at, q.signed_note = "", None, ""
    log(session, user, "quality.unsign", "Took back the quality review sign-off", e)
    session.commit()
    return state(session, e, user)


def workbook(e: Engagement) -> tuple[str, bytes]:
    q = e.quality_review
    answers = answered(q)
    about = pd.DataFrame([
        ("Client", e.client.name), ("Engagement", e.name),
        ("Period end", e.period_end.isoformat()),
        ("Quality review needed", "Yes" if q and q.required else "No"),
        ("Why", REASONS.get(q.reason, "") if q and q.reason else ""),
        ("Note", q.reason_note if q else ""),
        ("Quality reviewer", q.reviewer_name if q else ""),
        ("Conclusion", q.conclusion if q else ""),
        ("Signed off by", q.signed_by if q else ""),
        ("Signed off at", q.signed_at.isoformat(timespec="minutes") if q and q.signed_at
         else ""),
        ("Sign-off note", q.signed_note if q else ""),
        ("Changed since the sign-off", "Yes" if stale(q) else "No")], columns=["Item", "Value"])
    checklist = pd.DataFrame([{
        "Question": text, "Answer": ANSWERS.get(answers[k]["answer"], ""),
        "Comment": answers[k]["comment"],
        "Needs a second look": "Yes" if k in flags(answers) else ""}
        for k, text, _ in QUESTIONS])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        about.to_excel(xl, sheet_name="About", index=False)
        checklist.to_excel(xl, sheet_name="Checklist", index=False)
        finish_workbook(xl.book)
    return safe_stem(f"{e.client.name}-{e.name}-quality-review") + ".xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/quality-review/export")
def export_review(engagement_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(e)
    log(session, user, "quality.download", "Downloaded the quality review", e)
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))
