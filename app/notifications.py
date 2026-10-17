"""The bell: what needs the signed-in user now, across the engagements they are on.

Review notes waiting for an answer (for preparers) or to be cleared (for reviewers), their
own tasks overdue or due within a week, files clients sent that need checking, and, for
admins, people asking for an account. Nothing is stored: the list is worked out on each
look. The time the user last opened the bell is kept, so later items show as new.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user
from .db import (
    DocumentRequest,
    Engagement,
    EngagementLock,
    ReviewNote,
    SignupRequest,
    Task,
    User,
    UserSetting,
    get_session,
    utcnow,
)
from .review import SECTION

router = APIRouter(prefix="/api")
DUE_SOON_DAYS = 7
MAX_ITEMS = 50


def engagement_ids(session: Session, user: User) -> list[int]:
    """The unlocked engagements the user works on (admins: all of them)."""
    q = select(Engagement.id).where(~Engagement.id.in_(select(EngagementLock.engagement_id)))
    if not auth.sees_all_engagements(user):
        q = q.where(Engagement.id.in_(auth.assigned_engagement_ids(user)))
    return list(session.scalars(q))


def items_for(session: Session, user: User, today: date | None = None) -> list[dict]:
    if user.role == "client":
        return []
    today = today or date.today()
    ids = engagement_ids(session, user)
    names = {e.id: f"{e.client.name} · {e.name}" for e in session.scalars(
        select(Engagement).where(Engagement.id.in_(ids)))} if ids else {}
    out: list[dict] = []

    def add(kind: str, text: str, at: datetime, engagement_id: int | None, panel: str,
            urgent: bool = False, **extra) -> None:
        out.append({"kind": kind, "text": text, "at": at.isoformat(),
                    "engagement_id": engagement_id,
                    "engagement": names.get(engagement_id, ""), "panel": panel,
                    "urgent": urgent, **extra})

    if ids:
        # Notes a preparer answers, and answered notes a reviewer clears.
        wanted = ["open"] + (["responded"] if auth.can(user, "review") else [])
        for n in session.scalars(select(ReviewNote).where(
                ReviewNote.engagement_id.in_(ids), ReviewNote.status.in_(wanted))):
            label = SECTION[n.section].label if n.section in SECTION else n.section
            if n.status == "open":
                if n.author_id == user.id:
                    continue  # their own question, waiting on someone else
                add("note", f"Review note on {label} to answer", n.created_at,
                    n.engagement_id, "rv-panel", by=n.author, note=n.text[:200])
            else:
                last = n.replies[-1] if n.replies else None
                add("note_answered", f"Answered review note on {label} to clear",
                    last.at if last else n.created_at, n.engagement_id, "rv-panel",
                    by=last.author if last else "", note=n.text[:200])
        soon = today + timedelta(days=DUE_SOON_DAYS)
        for t in session.scalars(select(Task).where(
                Task.engagement_id.in_(ids), Task.assignee_id == user.id,
                Task.status != "done", Task.due_on.is_not(None), Task.due_on <= soon)):
            late = t.due_on < today
            # New from the day it came within a week of its due date, or went overdue.
            since = t.due_on + timedelta(days=1) if late else t.due_on - timedelta(days=DUE_SOON_DAYS)
            add("task", "Task overdue" if late else "Task due soon",
                max(t.updated_at, datetime.combine(since, datetime.min.time())), t.engagement_id,
                "wp-panel", urgent=late, title=t.title, due_on=t.due_on.isoformat())
        for r in session.scalars(select(DocumentRequest).where(
                DocumentRequest.engagement_id.in_(ids),
                DocumentRequest.status == "submitted")):
            sent = [m for m in r.messages if m.from_client]
            add("request", "Client sent files to check",
                sent[-1].at if sent else r.updated_at, r.engagement_id, "rq-panel",
                title=f"{r.number}. {r.title}")
    if auth.sees_all_engagements(user):
        signups = list(session.scalars(select(SignupRequest)))
        if signups:
            add("signup", "People asking for an account",
                max(s.created_at for s in signups), None, "signups-panel",
                count=len(signups))
    out.sort(key=lambda x: (x["urgent"], x["at"]), reverse=True)
    return out[:MAX_ITEMS]


def seen_at(user: User) -> datetime | None:
    return user.settings.notifications_seen_at if user.settings else None


@router.get("/notifications")
def notifications(user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    items = items_for(session, user)
    seen = seen_at(user)
    for x in items:
        x["new"] = seen is None or x["at"] > seen.isoformat()
    return {"items": items, "new": sum(1 for x in items if x["new"]),
            "seen_at": seen.isoformat() if seen else None}


@router.post("/notifications/seen")
def mark_seen(user: User = Depends(current_user),
              session: Session = Depends(get_session)) -> dict:
    if user.settings is None:  # no language picked yet: leave it unpicked ("")
        user.settings = UserSetting(language="")
    now = user.settings.notifications_seen_at = utcnow()
    user_id = user.id
    try:
        session.commit()
    except IntegrityError:  # another tab saved this user's first settings at the same moment
        session.rollback()
        session.expunge_all()
        session.execute(update(UserSetting).where(UserSetting.user_id == user_id)
                        .values(notifications_seen_at=now))
        session.commit()
    return {"seen_at": now.isoformat()}
