"""Firm checklists, as Caseware and TeamMate let a firm build its own: partners write a
checklist once (the firm's own questions, beyond the app's acceptance, disclosure and
completion checklists) and any team adds it to an engagement and answers it.

The engagement keeps its own copy of the questions, so a later change to the firm's checklist
doesn't change work already done; the panel says when the firm's version has changed. Each
question is answered Yes, No or Not applicable, and a No needs a comment. A checklist with
every question answered can be marked complete; any later change reopens it. Roll-forward
carries the checklists with the answers cleared.
"""

from __future__ import annotations

import io
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user, log, require
from .db import Engagement, EngagementChecklist, FirmChecklist, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GUARD = "firm-checklists"
MAX_CHECKLISTS = 100       # in the firm's library
MAX_PER_ENGAGEMENT = 30
ANSWERS = {"yes": "Yes", "no": "No", "na": "Not applicable"}

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class ChecklistIn(BaseModel):
    name: Name
    description: str = Field(default="", max_length=5000)
    items: list[Question] = Field(min_length=1, max_length=200)


class AddIn(BaseModel):
    checklist_id: int


class AnswerIn(BaseModel):
    answer: Literal["yes", "no", "na"] | None = None
    comment: str = Field(default="", max_length=5000)


class AnswersIn(BaseModel):
    answers: list[AnswerIn] = Field(max_length=200)
    items: list[str] | None = Field(default=None, max_length=200)  # the questions answered
    conclusion: str = Field(default="", max_length=5000)


def firm_out(c: FirmChecklist) -> dict:
    return {"id": c.id, "name": c.name, "description": c.description, "items": c.items,
            "updated_by": c.updated_by, "updated_at": c.updated_at.isoformat()}


def held(session: Session) -> None:
    """Take turns, so two partners can't make the same name at once."""
    auth.hold(session, GUARD)
    session.expire_all()


def check_name(session: Session, name: str, checklist_id: int | None = None) -> None:
    clash = session.scalar(select(FirmChecklist).where(FirmChecklist.name == name))
    if clash is not None and clash.id != checklist_id:
        raise HTTPException(409, f"There's already a checklist called {name}.")


def firm_or_404(session: Session, checklist_id: int) -> FirmChecklist:
    c = session.get(FirmChecklist, checklist_id)
    if c is None:
        raise HTTPException(404, "Checklist not found.")
    return c


@router.get("/checklists")
def list_firm(_: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> list[dict]:
    return [firm_out(c) for c in session.scalars(select(FirmChecklist)
                                                 .order_by(FirmChecklist.name))]


@router.post("/checklists", status_code=201)
def create_firm(body: ChecklistIn, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> dict:
    held(session)
    check_name(session, body.name)
    if len(session.scalars(select(FirmChecklist.id)).all()) >= MAX_CHECKLISTS:
        raise HTTPException(400, f"The firm can have up to {MAX_CHECKLISTS} checklists.")
    c = FirmChecklist(name=body.name, description=body.description.strip(), items=body.items,
                      updated_by=user.name, updated_at=utcnow())
    session.add(c)
    session.flush()
    log(session, user, "firm_checklist.create", f"Added the firm checklist {c.name}",
        details={"checklist_id": c.id, "items": c.items})
    session.commit()
    return firm_out(c)


@router.put("/checklists/{checklist_id}")
def update_firm(checklist_id: int, body: ChecklistIn, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> dict:
    held(session)
    c = firm_or_404(session, checklist_id)
    check_name(session, body.name, c.id)
    before = firm_out(c)
    c.name, c.description, c.items = body.name, body.description.strip(), body.items
    c.updated_by, c.updated_at = user.name, utcnow()
    log(session, user, "firm_checklist.update", f"Changed the firm checklist {c.name}",
        details={"checklist_id": c.id, "before": before, "items": c.items})
    session.commit()
    return firm_out(c)


@router.delete("/checklists/{checklist_id}", status_code=204)
def delete_firm(checklist_id: int, user: User = Depends(require("manage")),
                session: Session = Depends(get_session)) -> Response:
    held(session)
    c = firm_or_404(session, checklist_id)
    log(session, user, "firm_checklist.delete", f"Deleted the firm checklist {c.name}",
        details={"checklist_id": c.id, "items": c.items})
    # Engagements keep their own copies, no longer tied to the firm's.
    session.execute(update(EngagementChecklist).where(EngagementChecklist.source_id == c.id)
                    .values(source_id=None))
    session.delete(c)
    session.commit()
    return Response(status_code=204)


# --- On an engagement ---------------------------------------------------------------


def answer_of(x: EngagementChecklist, i: int) -> dict:
    a = (x.answers or {}).get(str(i)) or {}
    return {"answer": a.get("answer"), "comment": a.get("comment", "")}


def checklist_out(x: EngagementChecklist, library: dict[int, FirmChecklist]) -> dict:
    items = []
    for i, text in enumerate(x.items):
        a = answer_of(x, i)
        items.append({"text": text, **a,
                      "answer_label": ANSWERS.get(a["answer"]) if a["answer"] else None,
                      "needs_comment": a["answer"] == "no" and not a["comment"].strip()})
    answered = sum(1 for it in items if it["answer"])
    source = library.get(x.source_id) if x.source_id is not None else None
    return {"id": x.id, "name": x.name, "source_id": x.source_id, "items": items,
            "answered": answered, "total": len(items),
            "no": sum(1 for it in items if it["answer"] == "no"),
            "needs_comment": sum(1 for it in items if it["needs_comment"]),
            "conclusion": x.conclusion, "completed_by": x.completed_by,
            "completed_at": x.completed_at.isoformat() if x.completed_at else None,
            "changed_at_firm": source is not None and list(source.items) != list(x.items),
            "created_by": x.created_by, "updated_by": x.updated_by,
            "updated_at": x.updated_at.isoformat()}


def checklists_out(session: Session, e: Engagement) -> dict:
    library = {c.id: c for c in session.scalars(select(FirmChecklist)
                                                .order_by(FirmChecklist.name))}
    rows = [checklist_out(x, library) for x in e.checklists]
    return {"checklists": rows,
            "library": [{"id": c.id, "name": c.name, "items": len(c.items)}
                        for c in library.values()],
            "complete": sum(1 for x in rows if x["completed_at"]),
            "answers": [{"id": k, "label": v} for k, v in ANSWERS.items()]}


@router.get("/engagements/{engagement_id}/checklists")
def get_checklists(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    return checklists_out(session, get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/checklists", status_code=201)
def add_checklist(engagement_id: int, body: AddIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"checklists:{engagement_id}")
    c = firm_or_404(session, body.checklist_id)
    if any(x.source_id == c.id or x.name == c.name for x in e.checklists):
        raise HTTPException(409, f"This engagement already has the checklist {c.name}.")
    if len(e.checklists) >= MAX_PER_ENGAGEMENT:
        raise HTTPException(400, f"An engagement can have up to {MAX_PER_ENGAGEMENT} checklists.")
    x = EngagementChecklist(engagement=e, source_id=c.id, name=c.name, items=list(c.items),
                            answers={}, created_by=user.name, updated_by=user.name)
    session.add(x)
    session.flush()
    log(session, user, "checklist.add", f"Added the checklist {x.name}", e,
        details={"checklist_id": x.id, "firm_checklist_id": c.id})
    session.commit()
    session.refresh(e)
    return checklists_out(session, e)


def held_checklist(session: Session, user: User, engagement_checklist_id: int):
    x = get_or_404(session, EngagementChecklist, engagement_checklist_id, user)
    e = held_engagement(session, user, x.engagement_id, f"checklists:{x.engagement_id}")
    return get_or_404(session, EngagementChecklist, engagement_checklist_id, user), e


@router.put("/engagement-checklists/{engagement_checklist_id}")
def answer_checklist(engagement_checklist_id: int, body: AnswersIn,
                     user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    x, e = held_checklist(session, user, engagement_checklist_id)
    if len(body.answers) != len(x.items) or (body.items is not None
                                              and list(body.items) != list(x.items)):
        raise HTTPException(400, "The checklist has changed. Reload the page and try again.")
    answers = {str(i): {"answer": a.answer, "comment": a.comment.strip()}
               for i, a in enumerate(body.answers) if a.answer or a.comment.strip()}
    conclusion = body.conclusion.strip()
    if answers == (x.answers or {}) and conclusion == x.conclusion:
        return checklists_out(session, e)
    before = {"answers": x.answers, "conclusion": x.conclusion}
    reopened = x.completed_at is not None
    x.answers, x.conclusion = answers, conclusion
    x.completed_by, x.completed_at = "", None
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "checklist.update", f"Answered the checklist {x.name}"
        + (" (it was complete, so it is open again)" if reopened else ""), e,
        details={"engagement_checklist_id": x.id, "before": before, "answers": answers,
                 "conclusion": conclusion})
    session.commit()
    session.refresh(e)
    return checklists_out(session, e)


@router.post("/engagement-checklists/{engagement_checklist_id}/complete")
def complete_checklist(engagement_checklist_id: int, user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> dict:
    x, e = held_checklist(session, user, engagement_checklist_id)
    out = checklist_out(x, {})
    if out["answered"] < out["total"]:
        raise HTTPException(400, "Answer every question first.")
    if out["needs_comment"]:
        raise HTTPException(400, "Add a comment to every question answered No.")
    if x.completed_at is None:
        x.completed_by, x.completed_at = user.name, utcnow()
        log(session, user, "checklist.complete", f"Marked the checklist {x.name} complete", e,
            details={"engagement_checklist_id": x.id})
        session.commit()
    session.refresh(e)
    return checklists_out(session, e)


@router.post("/engagement-checklists/{engagement_checklist_id}/latest")
def use_latest(engagement_checklist_id: int, user: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> dict:
    """Take the firm's current questions, keeping the answers to questions that haven't
    changed."""
    x, e = held_checklist(session, user, engagement_checklist_id)
    c = session.get(FirmChecklist, x.source_id) if x.source_id is not None else None
    if c is None:
        raise HTTPException(400, "The firm no longer has this checklist.")
    old = {text: answer_of(x, i) for i, text in enumerate(x.items)}
    kept = {str(i): old[text] for i, text in enumerate(c.items)
            if text in old and (old[text]["answer"] or old[text]["comment"])}
    before = {"items": x.items, "answers": x.answers}
    x.items, x.answers = list(c.items), kept
    x.completed_by, x.completed_at = "", None
    x.updated_by, x.updated_at = user.name, utcnow()
    log(session, user, "checklist.latest", f"Took the firm's latest version of the checklist "
        f"{x.name}", e, details={"engagement_checklist_id": x.id, "before": before, "items": x.items})
    session.commit()
    session.refresh(e)
    return checklists_out(session, e)


@router.delete("/engagement-checklists/{engagement_checklist_id}")
def remove_checklist(engagement_checklist_id: int, user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    x, e = held_checklist(session, user, engagement_checklist_id)
    log(session, user, "checklist.remove", f"Removed the checklist {x.name}", e,
        details={"engagement_checklist_id": x.id, "items": x.items, "answers": x.answers,
                 "conclusion": x.conclusion})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return checklists_out(session, e)


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    out = checklists_out(session, e)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Checklists", len(out["checklists"])),
             ("Complete", out["complete"])]
    summary = [{"Checklist": x["name"], "Answered": x["answered"], "Questions": x["total"],
                "Answered No": x["no"],
                "Complete": f"{x['completed_by']} {x['completed_at'][:10]}"
                            if x["completed_at"] else "",
                "Conclusion": x["conclusion"]} for x in out["checklists"]]
    rows = [{"Checklist": x["name"], "No.": i + 1, "Question": it["text"],
             "Answer": it["answer_label"] or "", "Comment": it["comment"]}
            for x in out["checklists"] for i, it in enumerate(x["items"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(summary, columns=["Checklist", "Answered", "Questions", "Answered No",
                                       "Complete", "Conclusion"]).to_excel(
            xl, sheet_name="Checklists", index=False)
        pd.DataFrame(rows, columns=["Checklist", "No.", "Question", "Answer", "Comment"]).to_excel(
            xl, sheet_name="Answers", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-firm-checklists')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/checklists/export")
def export_checklists(engagement_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "checklist.download", "Downloaded the firm checklists", e)
    session.commit()
    name, content = workbook(session, e)
    return Response(content, media_type=XLSX, headers=attachment(name))
