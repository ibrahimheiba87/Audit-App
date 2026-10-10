"""Review notes and sign-off by section of the engagement file.

Each section is signed off twice: by whoever prepared it and then by a different person who
reviewed it. A sign-off goes stale when the section changes afterwards, judged from the
activity log, so the reviewer can see what moved since they looked. Reviewers raise notes on
a section; the preparer answers and a reviewer clears them. A section can't be signed off as
reviewed while it has notes that aren't cleared, and completion needs every other section
reviewed first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth
from .auth import log, require
from .db import Activity, Engagement, ReviewNote, ReviewReply, SignOff, User, get_session, utcnow
from .engagements import get_or_404

router = APIRouter(prefix="/api")


@dataclass(frozen=True)
class Section:
    id: str
    label: str
    description: str
    actions: tuple[str, ...]  # activity log actions that change the section


SECTIONS = [
    Section("planning", "Planning and materiality", "Engagement details and materiality.",
            ("engagement.update", "materiality.set")),
    Section("data", "Files and checks", "Uploaded files and the audit check runs.",
            ("upload.create", "upload.delete", "run.create")),
    Section("comparison", "Prior-year comparison", "Comparisons with last year's balances.",
            ("comparison.create",)),
    Section("sampling", "Sampling", "Samples drawn for testing.", ("sample.create",)),
    Section("findings", "Findings register", "Findings, their notes, evidence and status.",
            ("finding.create", "finding.delete", "finding.note", "finding.severity",
             "finding.status", "evidence.add", "evidence.remove")),
    Section("statements", "Lead sheets and statements",
            "Account mapping, adjusting entries and the draft statements.",
            ("mapping.update", "entry.create", "entry.update", "entry.delete")),
    Section("completion", "Completion",
            "The overall sign-off, once every other section is reviewed with no open notes.",
            ()),
]
SECTION = {s.id: s for s in SECTIONS}
WORK = [s for s in SECTIONS if s.id != "completion"]
ALL_ACTIONS = tuple(a for s in WORK for a in s.actions)

SectionId = Literal["planning", "data", "comparison", "sampling", "findings", "statements",
                    "completion"]
Kind = Literal["prepared", "reviewed"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class SignOffIn(BaseModel):
    section: SectionId
    kind: Kind


class NoteIn(BaseModel):
    section: SectionId
    text: Text


class ReplyIn(BaseModel):
    text: Text


class NotePatch(BaseModel):
    status: Literal["open", "cleared"]


def iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None


def last_changes(session: Session, engagement_id: int) -> dict[str, datetime | None]:
    """When each section last changed, from the activity log."""
    rows = session.execute(
        select(Activity.action, func.max(Activity.at)).where(
            Activity.engagement_id == engagement_id, Activity.action.in_(ALL_ACTIONS))
        .group_by(Activity.action)).all()
    by_action = dict(rows)
    out: dict[str, datetime | None] = {}
    for s in WORK:
        times = [by_action[a] for a in s.actions if by_action.get(a)]
        out[s.id] = max(times) if times else None
    times = [t for t in out.values() if t]
    out["completion"] = max(times) if times else None
    return out


def section_state(e: Engagement, changes: dict[str, datetime | None]) -> list[dict]:
    signoffs = {(s.section, s.kind): s for s in e.signoffs}
    notes: dict[str, int] = {}
    for n in e.review_notes:
        if n.status != "cleared":
            notes[n.section] = notes.get(n.section, 0) + 1
    out = []
    for s in SECTIONS:
        changed = changes.get(s.id)
        prep, rev = signoffs.get((s.id, "prepared")), signoffs.get((s.id, "reviewed"))
        prep_stale = bool(prep and changed and changed > prep.at)
        rev_stale = bool(rev and ((changed and changed > rev.at) or (prep and prep.at > rev.at)))
        if prep is None:
            status = "Not started"
        elif prep_stale:
            status = "Changed since prepared"
        elif rev is None:
            status = "Ready for review"
        elif rev_stale:
            status = "Changed since review"
        else:
            status = "Reviewed"
        out.append({
            "id": s.id, "label": s.label, "description": s.description,
            "last_change_at": iso(changed), "open_notes": notes.get(s.id, 0), "status": status,
            "done": status == "Reviewed" and not notes.get(s.id),
            "prepared": signoff_out(prep, prep_stale), "reviewed": signoff_out(rev, rev_stale),
        })
    return out


def signoff_out(s: SignOff | None, stale: bool) -> dict | None:
    if s is None:
        return None
    return {"user_id": s.user_id, "user_name": s.user_name, "at": iso(s.at), "stale": stale}


def note_out(n: ReviewNote) -> dict:
    return {"id": n.id, "section": n.section, "section_label": SECTION[n.section].label,
            "text": n.text, "status": n.status, "author_id": n.author_id, "author": n.author,
            "created_at": iso(n.created_at), "cleared_by": n.cleared_by,
            "cleared_at": iso(n.cleared_at),
            "replies": [{"id": r.id, "text": r.text, "author": r.author, "at": iso(r.at)}
                        for r in n.replies]}


def review_out(session: Session, e: Engagement) -> dict:
    sections = section_state(e, last_changes(session, e.id))
    return {"sections": sections,
            "notes": [note_out(n) for n in reversed(e.review_notes)],
            "open_notes": sum(s["open_notes"] for s in sections),
            "reviewed": sum(1 for s in sections if s["done"]),
            "total": len(sections)}


@router.get("/engagements/{engagement_id}/review")
def get_review(engagement_id: int, session: Session = Depends(get_session)) -> dict:
    return review_out(session, get_or_404(session, Engagement, engagement_id))


def withdraw_reviews(e: Engagement, section: str) -> list[str]:
    """Take back the review sign-off on `section` and on completion, e.g. when a note is
    raised. Returns the labels of the sections whose sign-off was withdrawn."""
    withdrawn = []
    for s in list(e.signoffs):
        if s.kind == "reviewed" and s.section in (section, "completion"):
            e.signoffs.remove(s)
            withdrawn.append(SECTION[s.section].label)
    return withdrawn


@router.post("/engagements/{engagement_id}/signoffs", status_code=201)
def sign_off(engagement_id: int, body: SignOffIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    if body.kind == "reviewed":
        auth.ensure(user, "manage" if body.section == "completion" else "review")
    auth.hold(session, f"review:{engagement_id}")  # notes and sign-offs change one at a time
    e = get_or_404(session, Engagement, engagement_id)
    section = SECTION[body.section]
    state = {s["id"]: s for s in section_state(e, last_changes(session, e.id))}
    mine = state[section.id]
    if body.section == "completion":
        if body.kind == "prepared":
            todo = [s["label"] for s in state.values()
                    if s["id"] != "completion" and (not s["prepared"] or s["prepared"]["stale"])]
        else:
            todo = [s["label"] for s in state.values() if s["id"] != "completion" and not s["done"]]
        if todo:
            need = "prepared" if body.kind == "prepared" else "reviewed with no open notes"
            raise HTTPException(400, f"Sign off completion once every section is {need}. "
                                     f"Still to do: {', '.join(todo)}.")
    if body.kind == "reviewed":
        prep = mine["prepared"]
        if prep is None or prep["stale"]:
            raise HTTPException(400, f"{section.label} needs to be signed off as prepared "
                                     "(again, if it changed) before it can be reviewed.")
        if prep["user_id"] == user.id:
            raise HTTPException(400, "You prepared this section, so someone else needs to "
                                     "review it.")
        if mine["open_notes"]:
            raise HTTPException(400, f"{section.label} has {mine['open_notes']} review "
                                     f"note{'s' if mine['open_notes'] != 1 else ''} still to "
                                     "clear.")
    existing = next((s for s in e.signoffs if s.section == section.id and s.kind == body.kind),
                    None)
    if existing is None:
        e.signoffs.append(SignOff(section=section.id, kind=body.kind, user_id=user.id,
                                  user_name=user.name, at=utcnow()))
    else:
        existing.user_id, existing.user_name, existing.at = user.id, user.name, utcnow()
    log(session, user, "signoff.add", f"Signed off {section.label} as {body.kind}", e,
        details={"section": section.id, "kind": body.kind})
    session.commit()
    return review_out(session, e)


@router.delete("/engagements/{engagement_id}/signoffs/{section}/{kind}")
def remove_sign_off(engagement_id: int, section: SectionId, kind: Kind,
                    user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    """Take back a sign-off: your own, or anyone's if you're a reviewer. Taking back the
    preparer's sign-off takes back the review too, since it was a review of that work."""
    auth.hold(session, f"review:{engagement_id}")
    e = get_or_404(session, Engagement, engagement_id)
    target = next((s for s in e.signoffs if s.section == section and s.kind == kind), None)
    if target is None:
        raise HTTPException(404, "That section isn't signed off.")
    if target.user_id != user.id:
        auth.ensure(user, "review")
    removed = [s for s in e.signoffs if s.section == section and
               (s.kind == kind or kind == "prepared")]
    for s in removed:
        e.signoffs.remove(s)
    label = SECTION[section].label
    log(session, user, "signoff.remove",
        f"Took back the {' and '.join(s.kind for s in removed)} sign-off on {label}", e,
        details={"section": section, "kinds": [s.kind for s in removed],
                 "signed_by": [s.user_name for s in removed]})
    session.commit()
    return review_out(session, e)


@router.post("/engagements/{engagement_id}/review-notes", status_code=201)
def add_note(engagement_id: int, body: NoteIn, user: User = Depends(require("review")),
             session: Session = Depends(get_session)) -> dict:
    auth.hold(session, f"review:{engagement_id}")
    e = get_or_404(session, Engagement, engagement_id)
    note = ReviewNote(section=body.section, text=body.text, author_id=user.id,
                      author=user.name)
    e.review_notes.append(note)
    withdrawn = withdraw_reviews(e, body.section)
    session.flush()
    label = SECTION[body.section].label
    log(session, user, "note.create", f"Raised a review note on {label}: {body.text[:200]}", e,
        details={"note_id": note.id, "section": body.section, "text": body.text,
                 "withdrew_review": withdrawn})
    session.commit()
    return note_out(note)


def note_and_engagement(session: Session, note_id: int) -> tuple[ReviewNote, Engagement]:
    engagement_id = get_or_404(session, ReviewNote, note_id).engagement_id
    auth.hold(session, f"review:{engagement_id}")
    session.expire_all()  # it may have changed before we held the guard
    note = get_or_404(session, ReviewNote, note_id)
    return note, note.engagement


@router.post("/review-notes/{note_id}/replies", status_code=201)
def reply_to_note(note_id: int, body: ReplyIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    note, e = note_and_engagement(session, note_id)
    if note.status == "cleared":
        raise HTTPException(400, "This note is cleared. A reviewer can reopen it first.")
    note.replies.append(ReviewReply(text=body.text, author_id=user.id, author=user.name))
    if note.author_id != user.id:
        note.status = "responded"
    log(session, user, "note.reply",
        f"Replied to a review note on {SECTION[note.section].label}: {body.text[:200]}", e,
        details={"note_id": note.id, "text": body.text})
    session.commit()
    return note_out(note)


@router.patch("/review-notes/{note_id}")
def update_note(note_id: int, body: NotePatch, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> dict:
    """Clear a note once its answer is satisfactory, or reopen it."""
    note, e = note_and_engagement(session, note_id)
    label = SECTION[note.section].label
    if body.status == "cleared" and note.status != "cleared":
        note.status, note.cleared_by, note.cleared_at = "cleared", user.name, utcnow()
        log(session, user, "note.clear", f"Cleared a review note on {label}", e,
            details={"note_id": note.id})
    elif body.status == "open" and note.status == "cleared":
        note.status, note.cleared_by, note.cleared_at = "open", "", None
        withdrawn = withdraw_reviews(e, note.section)
        log(session, user, "note.reopen", f"Reopened a review note on {label}", e,
            details={"note_id": note.id, "withdrew_review": withdrawn})
    session.commit()
    return note_out(note)


@router.delete("/review-notes/{note_id}", status_code=204)
def delete_note(note_id: int, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> Response:
    """Delete a note raised by mistake: only its author, and only before anyone replies."""
    note, e = note_and_engagement(session, note_id)
    if note.author_id != user.id or note.replies:
        raise HTTPException(400, "Only the reviewer who raised a note can delete it, and only "
                                 "before anyone replies. Clear it instead.")
    log(session, user, "note.delete",
        f"Deleted a review note on {SECTION[note.section].label}: {note.text[:200]}", e,
        details={"note_id": note.id, "text": note.text})
    session.delete(note)
    session.commit()
    return Response(status_code=204)

