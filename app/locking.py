"""Locking and archiving engagements.

Once the audit report is signed, the file is assembled and locked (ISA 230): from then on
nothing in it can be added, changed or deleted, though everything can still be read and
downloaded. A partner locks it; only an admin can unlock it, giving a reason, and both are
logged. A locked engagement can be archived, which takes it out of the engagement list.

Every request that would change an engagement's file goes through `check_unlocked`, which
main.py adds to the engagement routers, so a new route is covered without remembering to.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from . import auth
from .auth import log, require, signed_in_user
from .db import (
    AdjustingEntry,
    AiDraft,
    BankReconciliation,
    Comparison,
    CrossMatch,
    DocumentRequest,
    Engagement,
    EngagementLock,
    Evidence,
    Finding,
    RequestFile,
    ReviewNote,
    Risk,
    RiskProcedure,
    Run,
    Sample,
    Upload,
    User,
    get_session,
    utcnow,
)
from .engagements import engagement_id_of, get_or_404, lock_out

router = APIRouter(prefix="/api")

# The record each path parameter names, to find the engagement a request would change.
PARAM_MODELS = {
    "engagement_id": Engagement, "upload_id": Upload, "run_id": Run,
    "comparison_id": Comparison, "sample_id": Sample, "finding_id": Finding,
    "evidence_id": Evidence, "entry_id": AdjustingEntry, "note_id": ReviewNote,
    "request_id": DocumentRequest, "file_id": RequestFile, "rec_id": BankReconciliation,
    "draft_id": AiDraft, "risk_id": Risk, "procedure_id": RiskProcedure,
    "match_id": CrossMatch,
}
# Requests that change nothing in the file, so they still work once it's locked: analyses
# and AI explanations that are only logged, carrying the engagement into next year, who is
# on the team, and locking itself.
ALLOWED_WHEN_LOCKED = {
    ("POST", "/api/uploads/{upload_id}/stratify"),
    ("POST", "/api/uploads/{upload_id}/aging"),
    ("POST", "/api/findings/{finding_id}/assistant"),
    ("POST", "/api/engagements/{engagement_id}/mapping/suggest"),
    ("POST", "/api/engagements/{engagement_id}/roll-forward"),
    ("PUT", "/api/engagements/{engagement_id}/team"),
    ("POST", "/api/engagements/{engagement_id}/lock"),
    ("DELETE", "/api/engagements/{engagement_id}/lock"),
    ("POST", "/api/engagements/{engagement_id}/archive"),
}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCKED = ("This engagement is locked, so its file can't be changed. An admin can unlock it "
          "if it has to be.")
CLIENT_LOCKED = ("Your auditors have closed this audit file, so nothing more can be sent "
                 "for it. Contact them if you need to.")
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def can_see(session: Session, user: User, obj, engagement_id: int) -> bool:
    """Whether `user` may see the record, so a refusal tells them nothing they couldn't
    already find out; anyone else gets the route's usual 404."""
    if user.role == "client":
        e = session.get(Engagement, engagement_id)
        return (e is not None and user.client_id == e.client_id
                and isinstance(obj, (DocumentRequest, RequestFile)))
    return auth.on_engagement(session, user, engagement_id)


def check_unlocked(request: Request, user: User = Depends(signed_in_user),
                   session: Session = Depends(get_session)) -> None:
    """Dependency: refuse a change to a locked engagement's file with a 409."""
    if request.method in READ_METHODS:
        return
    route = request.scope.get("route")
    if route is None or (request.method, route.path) in ALLOWED_WHEN_LOCKED:
        return
    for name, value in request.path_params.items():
        model = PARAM_MODELS.get(name)
        if model is None:
            continue
        try:
            obj = session.get(model, int(value))
        except ValueError:
            return  # not a number: the route answers with its own validation error
        engagement_id = engagement_id_of(obj) if obj is not None else None
        if engagement_id is None:
            return  # missing: the route answers with its own 404
        if not can_see(session, user, obj, engagement_id):
            return  # the route answers with its own 404
        if session.get(EngagementLock, engagement_id) is not None:
            raise HTTPException(409, locked_message(user))
        # Checked again as the change is saved: the engagement may be locked in between,
        # for instance while the AI assistant is answering or a large file is uploading.
        session.info["unlocked_engagement"] = (engagement_id, locked_message(user))
        return


def locked_message(user: User) -> str:
    return CLIENT_LOCKED if user.role == "client" else LOCKED


@event.listens_for(Session, "before_commit")
def _still_unlocked(session: Session) -> None:
    checked = session.info.get("unlocked_engagement")
    if checked is None:
        return
    engagement_id, message = checked
    locked = session.execute(select(EngagementLock.engagement_id)
                             .where(EngagementLock.engagement_id == engagement_id)).first()
    if locked is not None:
        raise HTTPException(409, message)


def held(session: Session, user: User, engagement_id: int) -> Engagement:
    """The engagement, holding the guard that deletes also hold (engagements.hold_for_delete),
    so it can't be locked while it is being deleted."""
    get_or_404(session, Engagement, engagement_id, user)
    auth.hold(session, f"team:{engagement_id}")
    session.expire_all()
    return get_or_404(session, Engagement, engagement_id, user)


class LockIn(BaseModel):
    note: Note = ""


class UnlockIn(BaseModel):
    reason: Reason


class ArchiveIn(BaseModel):
    archived: bool


@router.post("/engagements/{engagement_id}/lock")
def lock(engagement_id: int, body: LockIn, user: User = Depends(require("manage")),
         session: Session = Depends(get_session)) -> dict:
    e = held(session, user, engagement_id)
    if e.lock is not None:
        raise HTTPException(409, "This engagement is already locked.")
    e.lock = EngagementLock(locked_by=user.name, note=body.note)
    log(session, user, "engagement.lock", f"Locked engagement {e.name}", e,
        details={"note": body.note})
    session.commit()
    return {"lock": lock_out(e.lock)}


@router.delete("/engagements/{engagement_id}/lock")
def unlock(engagement_id: int, body: UnlockIn, user: User = Depends(require("admin")),
           session: Session = Depends(get_session)) -> dict:
    e = held(session, user, engagement_id)
    if e.lock is None:
        raise HTTPException(409, "This engagement isn't locked.")
    was = lock_out(e.lock)
    session.delete(e.lock)
    log(session, user, "engagement.unlock", f"Unlocked engagement {e.name}: {body.reason}", e,
        details={"reason": body.reason, "was": was})
    session.commit()
    return {"lock": None}


@router.post("/engagements/{engagement_id}/archive")
def archive(engagement_id: int, body: ArchiveIn, user: User = Depends(require("manage")),
            session: Session = Depends(get_session)) -> dict:
    e = held(session, user, engagement_id)
    if e.lock is None:
        raise HTTPException(409, "Lock the engagement before archiving it.")
    if e.lock.archived != body.archived:
        e.lock.archived = body.archived
        e.lock.archived_by, e.lock.archived_at = ((user.name, utcnow()) if body.archived
                                                  else ("", None))
        log(session, user, "engagement.archive" if body.archived else "engagement.unarchive",
            f"{'Archived' if body.archived else 'Brought back from the archive'} "
            f"engagement {e.name}", e)
        session.commit()
    return {"lock": lock_out(e.lock)}
