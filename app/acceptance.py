"""Client acceptance and continuance, and the team's independence declarations.

Before taking on (or continuing) an audit the firm considers the client's integrity, its own
competence and independence, the reporting framework and the terms (ISA 210, ISA 220,
ISQM 1). The team answers the checklist, a partner records the decision, and each member of
the team confirms their independence or declares the threats and the safeguards against
them. Every change is logged, and the record exports to Excel.
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

from .auth import ROLES, current_user, log, require
from .db import (
    AcceptanceChecklist,
    Engagement,
    IndependenceDeclaration,
    User,
    get_session,
    utcnow,
)
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# (id, question, the answer that needs no further thought). A different answer is flagged for
# the partner, who can still accept with a conclusion that says why.
QUESTIONS = [
    ("integrity", "Have we considered the integrity of the client's owners, those charged "
                  "with governance and management, with nothing that makes us unwilling to act?",
     "yes"),
    ("competence", "Does the team have the competence, time and resources to do the audit?",
     "yes"),
    ("ethics", "Can the firm and the team meet the ethical requirements, including "
               "independence?", "yes"),
    ("framework", "Is the financial reporting framework acceptable for these statements?",
     "yes"),
    ("terms", "Has management agreed its responsibilities and the terms in an engagement "
              "letter?", "yes"),
    ("fees", "Are the fees enough for a proper audit, with no unpaid fees from earlier "
             "work?", "yes"),
    ("predecessor", "For a new client, have we contacted the previous auditor and seen "
                    "nothing that stops us accepting?", "yes"),
    ("prior_issues", "Have significant matters from earlier audits (ours or the previous "
                     "auditor's) been considered?", "yes"),
    ("engagement_risk", "Is the engagement risk acceptable (going concern, litigation, "
                        "regulators, related parties, media attention)?", "yes"),
    ("quality_review", "Does the engagement need an engagement quality review (listed or "
                       "public interest entity, or firm policy)?", None),
]
QUESTION = {q: (text, expected) for q, text, expected in QUESTIONS}
ANSWERS = {"yes": "Yes", "no": "No", "na": "Not applicable"}
DECISIONS = {"accept": "Accepted", "decline": "Declined"}
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class AnswerIn(BaseModel):
    answer: Literal["yes", "no", "na"] | None = None
    comment: Text = ""


class ChecklistIn(BaseModel):
    answers: dict[str, AnswerIn] = Field(default={}, max_length=len(QUESTIONS))
    conclusion: Text | None = None  # None keeps the conclusion as it is


class DecisionIn(BaseModel):
    decision: Literal["accept", "decline"]
    note: Text = ""


class DeclarationIn(BaseModel):
    independent: bool
    threats: Text = ""
    safeguards: Text = ""


def answered(c: AcceptanceChecklist | None) -> dict[str, dict]:
    raw = (c.answers if c else None) or {}
    return {q: {"answer": (raw.get(q) or {}).get("answer"),
                "comment": (raw.get(q) or {}).get("comment", "")} for q, _, _ in QUESTIONS}


def flags(answers: dict[str, dict]) -> list[str]:
    """Questions answered other than the reassuring way (not applicable is fine)."""
    return [q for q, _, expected in QUESTIONS
            if expected and answers[q]["answer"] not in (None, expected, "na")]


def team_members(session: Session, e: Engagement) -> list[User]:
    """Who should declare: the engagement's team, and anyone else who has declared."""
    ids = {m.user_id for m in e.team} | {d.user_id for d in e.declarations}
    if not ids:
        return []
    users = session.scalars(select(User).where(User.id.in_(ids))).all()
    return sorted((u for u in users if u.role != "client"), key=lambda u: u.name.lower())


def declaration_out(d: IndependenceDeclaration) -> dict:
    return {"user_id": d.user_id, "user_name": d.user_name, "independent": d.independent,
            "threats": d.threats, "safeguards": d.safeguards,
            "declared_at": d.declared_at.isoformat()}


def state(session: Session, e: Engagement, user: User) -> dict:
    c = e.acceptance
    answers = answered(c)
    by_user = {d.user_id: d for d in e.declarations}
    members = [{"user_id": u.id, "name": u.name, "role": u.role, "role_label": ROLES[u.role],
                "active": u.active, "on_team": any(m.user_id == u.id for m in e.team),
                "declaration": declaration_out(by_user[u.id]) if u.id in by_user else None}
               for u in team_members(session, e)]
    active = [m for m in members if m["active"] and m["on_team"]]
    decided_at = c.decided_at if c else None
    return {
        "questions": [{"id": q, "text": text, "expected": expected}
                      for q, text, expected in QUESTIONS],
        "answers": answers,
        "conclusion": c.conclusion if c else "",
        "answered": sum(1 for a in answers.values() if a["answer"]),
        "flags": flags(answers),
        "updated_by": c.updated_by if c else "",
        "updated_at": c.updated_at.isoformat() if c else None,
        "decision": c.decision if c else None,
        "decision_label": DECISIONS.get(c.decision) if c and c.decision else None,
        "decision_note": c.decision_note if c else "",
        "decided_by": c.decided_by if c else "",
        "decided_at": decided_at.isoformat() if decided_at else None,
        # The checklist was changed after the partner decided, so the decision may need a look.
        "changed_since_decision": bool(c and decided_at and c.updated_at > decided_at),
        "independence": {
            "members": members,
            "declared": sum(1 for m in active if m["declaration"]),
            "expected": len(active),
            "threats": sum(1 for d in e.declarations if not d.independent),
            "mine": declaration_out(by_user[user.id]) if user.id in by_user else None,
        },
    }


def summary(e: Engagement) -> dict:
    """For the dashboard: where acceptance and independence stand."""
    c = e.acceptance
    on_team = {m.user_id for m in e.team if m.user.active}  # as state() counts them
    declared = {d.user_id for d in e.declarations}
    return {"started": c is not None, "decision": c.decision if c else None,
            "threats": sum(1 for d in e.declarations if not d.independent),
            "declared": len(declared & on_team), "team": len(on_team),
            "any_declared": bool(declared)}


@router.get("/engagements/{engagement_id}/acceptance")
def get_acceptance(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    return state(session, get_or_404(session, Engagement, engagement_id, user), user)


@router.put("/engagements/{engagement_id}/acceptance")
def save_checklist(engagement_id: int, body: ChecklistIn,
                   user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    unknown = sorted(set(body.answers) - set(QUESTION))
    if unknown:
        raise HTTPException(400, f"Unknown question: {unknown[0]}.")
    e = held_engagement(session, user, engagement_id, f"acceptance:{engagement_id}")
    before = answered(e.acceptance)
    answers = {**before, **{q: a.model_dump() for q, a in body.answers.items()}}
    changed = sorted(q for q in QUESTION if answers[q] != before[q])
    old_conclusion = e.acceptance.conclusion if e.acceptance else ""
    conclusion = old_conclusion if body.conclusion is None else body.conclusion
    # Saving without a change leaves it alone, so the decision isn't marked out of date.
    if changed or conclusion != old_conclusion:
        c = e.acceptance or AcceptanceChecklist(engagement=e)
        c.answers = {q: a for q, a in answers.items() if a["answer"] or a["comment"]}
        c.conclusion = conclusion
        c.updated_by, c.updated_at = user.name, utcnow()
        session.add(c)
        log(session, user, "acceptance.update",
            "Updated the acceptance and continuance checklist", e,
            details={"changed": changed, "answers": {q: answers[q]["answer"] for q in changed},
                     "conclusion_changed": conclusion != old_conclusion})
        session.commit()
    return state(session, e, user)


@router.post("/engagements/{engagement_id}/acceptance/decision")
def decide(engagement_id: int, body: DecisionIn, user: User = Depends(require("manage")),
           session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"acceptance:{engagement_id}")
    c = e.acceptance
    answers = answered(c)
    missing = [q for q in QUESTION if not answers[q]["answer"]]
    if missing:
        raise HTTPException(400, f"Answer every question first ({len(missing)} left).")
    if body.decision == "accept" and flags(answers) and not (body.note or c.conclusion):
        raise HTTPException(400, "Some answers need a second look. Say in the note why the "
                                 "engagement can still be accepted.")
    c.decision, c.decision_note = body.decision, body.note
    c.decided_by, c.decided_at = user.name, utcnow()
    log(session, user, "acceptance.decide",
        f"{DECISIONS[body.decision]} the engagement at acceptance", e,
        details={"decision": body.decision, "flags": flags(answers), "note": body.note})
    session.commit()
    return state(session, e, user)


@router.post("/engagements/{engagement_id}/independence")
def declare(engagement_id: int, body: DeclarationIn, user: User = Depends(require("prepare")),
            session: Session = Depends(get_session)) -> dict:
    """The signed-in user's own declaration; it replaces any earlier one."""
    e = held_engagement(session, user, engagement_id, f"acceptance:{engagement_id}")
    if not body.independent and not body.threats:
        raise HTTPException(400, "Describe the threat to your independence.")
    d = next((x for x in e.declarations if x.user_id == user.id), None)
    if d is None:
        d = IndependenceDeclaration(engagement=e, user_id=user.id)
        session.add(d)
    d.user_name, d.independent = user.name, body.independent
    d.threats = "" if body.independent else body.threats
    d.safeguards = "" if body.independent else body.safeguards
    d.declared_at = utcnow()
    log(session, user, "independence.declare",
        "Confirmed independence" if body.independent
        else "Declared a threat to independence", e,
        details={"independent": body.independent, "threats": d.threats,
                 "safeguards": d.safeguards})
    session.commit()
    return state(session, e, user)


def to_excel(e: Engagement, s: dict, context: dict) -> bytes:
    about = list(context.items()) + [
        ("Decision", s["decision_label"] or "Not yet decided"),
        ("Decided by", s["decided_by"]),
        ("Decided at", f"{s['decided_at'][:16].replace('T', ' ')} UTC" if s["decided_at"]
         else ""),
        ("Decision note", s["decision_note"]),
        ("Conclusion", s["conclusion"]),
        ("Last updated by", s["updated_by"]),
    ]
    checklist = [{"Question": text, "Answer": ANSWERS.get(s["answers"][q]["answer"], ""),
                  "Comment": s["answers"][q]["comment"],
                  "Needs a second look": "Yes" if q in s["flags"] else ""}
                 for q, text, _ in QUESTIONS]
    people = [{"Name": m["name"], "Role": m["role_label"],
               "On the team": "Yes" if m["on_team"] else "No",
               "Independent": "" if not m["declaration"]
               else ("Yes" if m["declaration"]["independent"] else "No"),
               "Threats": (m["declaration"] or {}).get("threats", ""),
               "Safeguards": (m["declaration"] or {}).get("safeguards", ""),
               "Declared at": f"{m['declaration']['declared_at'][:16].replace('T', ' ')} UTC"
               if m["declaration"] else "Not yet declared"}
              for m in s["independence"]["members"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(checklist).to_excel(xl, sheet_name="Acceptance checklist", index=False)
        pd.DataFrame(people, columns=["Name", "Role", "On the team", "Independent", "Threats",
                                      "Safeguards", "Declared at"]).to_excel(
            xl, sheet_name="Independence", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.get("/engagements/{engagement_id}/acceptance/export")
def export_acceptance(engagement_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    s = state(session, e, user)
    log(session, user, "acceptance.download",
        "Downloaded the acceptance and independence record", e)
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat()}
    stem = safe_stem(f"{e.client.name}-{e.name}-acceptance")
    return Response(to_excel(e, s, context), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))

