"""Sign-in, roles and the activity log.

Roles, lowest to highest. Each role can do everything the roles below it can:
    preparer  upload files, run checks, comparisons and samples, add findings, notes and
              evidence, mark findings reviewed
    reviewer  clear and reopen findings, change severity, remove evidence and files,
              add and edit clients and engagements
    partner   set materiality, delete clients and engagements
    admin     manage users, see the whole activity log
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from .db import Activity, AuthSession, Engagement, Guard, User, get_session, utcnow

ROLES = {"preparer": "Preparer", "reviewer": "Reviewer", "partner": "Partner", "admin": "Admin"}
RANK = {role: i for i, role in enumerate(ROLES, start=1)}
# Lowest role allowed to do each kind of thing.
PERMISSIONS = {"prepare": "preparer", "review": "reviewer", "manage": "partner", "admin": "admin"}
PERMISSION_TEXT = {
    "review": "a reviewer, partner or admin",
    "manage": "a partner or admin",
    "admin": "an admin",
}

COOKIE = "audit_session"
SESSION_HOURS = 12
MIN_PASSWORD = 10
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


# --- Passwords ----------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}$" \
           f"{b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Passwords need at least {MIN_PASSWORD} characters.")


# --- Sessions -----------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(session: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(token_hash=_token_hash(token), user_id=user.id,
                            expires_at=utcnow() + timedelta(hours=SESSION_HOURS)))
    user.last_login_at = utcnow()
    return token


def end_session(session: Session, token: str | None) -> None:
    if token:
        for s in session.scalars(select(AuthSession).where(
                AuthSession.token_hash == _token_hash(token))):
            session.delete(s)


def end_user_sessions(session: Session, user: User) -> None:
    for s in session.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
        session.delete(s)


def secure_cookies(request: Request) -> bool:
    return request.url.scheme == "https" or os.environ.get("AUDIT_SECURE_COOKIES") == "1"


def user_for_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    s = session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
    if s is None or s.expires_at < utcnow() or not s.user.active:
        return None
    return s.user


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = user_for_token(session, request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return user


def can(user: User, permission: str) -> bool:
    return RANK[user.role] >= RANK[PERMISSIONS[permission]]


def ensure(user: User, permission: str) -> None:
    if not can(user, permission):
        raise HTTPException(403, f"Your role ({ROLES[user.role]}) can't do this; it needs "
                                 f"{PERMISSION_TEXT[permission]}.")


def require(permission: str):
    """Dependency: the signed-in user, if their role allows `permission`."""
    def dependency(user: User = Depends(current_user)) -> User:
        ensure(user, permission)
        return user
    dependency.__name__ = f"require_{permission}"
    return dependency


# --- Activity log -------------------------------------------------------------


def log(session: Session, user: User | None, action: str, summary: str,
        engagement: Engagement | None = None, details: dict | None = None,
        request: Request | None = None) -> None:
    """Record an action. It is saved with the caller's next commit."""
    session.add(Activity(
        user_id=user.id if user else None, user_name=user.name if user else "",
        user_role=user.role if user else "", action=action,
        engagement_id=engagement.id if engagement else None,
        engagement_label=f"{engagement.client.name} · {engagement.name}" if engagement else "",
        summary=summary, details=details or {},
        ip=(request.client.host if request and request.client else "")[:64],
    ))


# --- Serializing checks -------------------------------------------------------


def hold(session: Session, key: str) -> None:
    """Make concurrent requests that hold the same key run one at a time.

    Writes the guard row for `key`, which keeps a write lock on it until this transaction
    commits or rolls back. Call it before reading the data a check depends on, and before any
    other write in the transaction.
    """
    # One atomic upsert, so a key that doesn't exist yet can't be created twice. db.py only
    # allows SQLite and PostgreSQL, which both have it.
    insert = postgresql.insert if session.get_bind().dialect.name == "postgresql" \
        else sqlite.insert
    session.execute(insert(Guard).values(key=key, n=1).on_conflict_do_update(
        index_elements=[Guard.key], set_={"n": Guard.n + 1}))


def login_key(email: str) -> str:
    """Guard key for sign-ins to `email`. Bucketed so failed guesses can't grow the table."""
    return f"login:{hashlib.sha256(email.encode()).hexdigest()[:2]}"


def recent_failed_logins(session: Session, email: str) -> int:
    since = utcnow() - timedelta(minutes=LOCKOUT_MINUTES)
    return session.scalar(select(func.count(Activity.id)).where(
        Activity.action == "auth.login_failed", Activity.at >= since,
        Activity.summary == f"Failed sign-in for {email}",
    )) or 0


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role,
            "role_label": ROLES[u.role], "rank": RANK[u.role], "active": u.active,
            "created_at": u.created_at.isoformat(),
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


def activity_out(a: Activity) -> dict:
    return {"id": a.id, "at": a.at.isoformat(), "user_id": a.user_id, "user_name": a.user_name,
            "user_role": a.user_role, "action": a.action, "engagement_id": a.engagement_id,
            "engagement_label": a.engagement_label, "summary": a.summary,
            "details": a.details, "ip": a.ip}
