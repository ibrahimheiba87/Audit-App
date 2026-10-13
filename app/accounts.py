"""Sign-in, user management and activity log endpoints."""

from __future__ import annotations

import hmac
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth
from .auth import COOKIE, ROLES, current_user, log, require, signed_in_user, user_out
from .db import (
    Activity,
    Client,
    ClientUser,
    Engagement,
    EngagementMember,
    User,
    UserSetting,
    get_session,
)

router = APIRouter(prefix="/api")

Role = Literal["client", "preparer", "reviewer", "partner", "admin"]


class SetupIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(max_length=200)
    setup_code: str = Field(default="", max_length=200)


def setup_code() -> str:
    """AUDIT_SETUP_CODE, if set: the first admin must enter it, so that on a server that's
    already online, a stranger can't set the app up before you do."""
    return os.environ.get("AUDIT_SETUP_CODE", "").strip()


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class LanguageIn(BaseModel):
    language: Literal["en", "ar"]


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    role: Role
    password: str = Field(max_length=200)
    client_id: int | None = None  # required for client users, who only see this client


class UserPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    active: bool | None = None
    password: str | None = Field(default=None, max_length=200)
    client_id: int | None = None


def clean_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(400, "Enter a valid email address.")
    return email


def signed_in(request: Request, session: Session, user: User, status: int = 200) -> Response:
    token = auth.start_session(session, user)
    session.commit()
    res = JSONResponse({"user": user_out(user)}, status_code=status)
    res.set_cookie(COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True,
                   samesite="lax", secure=auth.secure_cookies(request), path="/")
    return res


def link_client(session: Session, user: User, role: str, client_id: int | None) -> str:
    """Tie a client user to their client (or untie a user who stops being one).

    Returns the client's name for the activity log, or "" for audit team roles.
    """
    if role != "client":
        if client_id is not None:
            raise HTTPException(400, "Only client users belong to a client.")
        user.client_link = None
        return ""
    client_id = client_id if client_id is not None else user.client_id
    client = session.get(Client, client_id) if client_id is not None else None
    if client is None:
        raise HTTPException(400, "Pick the client this user works for.")
    if user.client_link is None:
        user.client_link = ClientUser(client=client)
    else:
        user.client_link.client = client
    return client.name


def active_admins(session: Session) -> int:
    return session.scalar(select(func.count(User.id)).where(
        User.role == "admin", User.active.is_(True))) or 0


# --- Sign-in ------------------------------------------------------------------


@router.get("/auth/status")
def status(request: Request, session: Session = Depends(get_session)) -> dict:
    user = auth.user_for_token(session, request.cookies.get(COOKIE))
    needs_setup = session.scalar(select(func.count(User.id))) == 0
    return {"needs_setup": needs_setup, "setup_code_required": needs_setup and bool(setup_code()),
            "user": user_out(user) if user else None,
            "roles": [{"id": k, "label": v} for k, v in ROLES.items()]}


@router.post("/auth/setup", status_code=201)
def setup(body: SetupIn, request: Request, session: Session = Depends(get_session)) -> Response:
    """Create the first admin. Only works while there are no users."""
    code = setup_code()
    if code and not hmac.compare_digest(body.setup_code.strip().encode(), code.encode()):
        raise HTTPException(403, "That setup code is wrong. It's AUDIT_SETUP_CODE in the "
                                 "server's settings.")
    auth.check_password_strength(body.password)
    auth.hold(session, "setup")  # so two first-run requests can't both create an admin
    if session.scalar(select(func.count(User.id))):
        raise HTTPException(409, "The app is already set up. Sign in instead.")
    user = User(email=clean_email(body.email), name=body.name.strip(), role="admin",
                password_hash=auth.hash_password(body.password))
    session.add(user)
    session.flush()
    log(session, user, "auth.setup", f"Set up the app as admin {user.email}", request=request)
    return signed_in(request, session, user, 201)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, session: Session = Depends(get_session)) -> Response:
    email = body.email.strip().lower()
    # One attempt at a time per account, so parallel guesses can't get past the limit.
    auth.hold(session, auth.login_key(email))
    if auth.recent_failed_logins(session, email) >= auth.MAX_FAILED_LOGINS:
        session.rollback()
        raise HTTPException(429, f"Too many failed sign-ins. Try again in "
                                 f"{auth.LOCKOUT_MINUTES} minutes.")
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not user.active or not auth.verify_password(body.password,
                                                                    user.password_hash):
        log(session, None, "auth.login_failed", f"Failed sign-in for {email}", request=request)
        session.commit()
        raise HTTPException(401, "Email or password is wrong.")
    log(session, user, "auth.login", "Signed in", request=request)
    return signed_in(request, session, user)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, session: Session = Depends(get_session)) -> Response:
    token = request.cookies.get(COOKIE)
    user = auth.user_for_token(session, token)
    auth.end_session(session, token)
    if user:
        log(session, user, "auth.logout", "Signed out", request=request)
    session.commit()
    res = Response(status_code=204)
    res.delete_cookie(COOKIE, path="/")
    return res


@router.get("/auth/me")
def me(user: User = Depends(signed_in_user)) -> dict:
    return user_out(user)


@router.put("/auth/language", status_code=204)
def set_language(body: LanguageIn, user: User = Depends(signed_in_user),
                 session: Session = Depends(get_session)) -> Response:
    """Remember the language this user picked, so the app opens in it on any device."""
    user_id = user.id
    if user.settings is None:
        user.settings = UserSetting(language=body.language)
    else:
        user.settings.language = body.language
    try:
        session.commit()
    except IntegrityError:  # another request saved this user's first choice at the same moment
        session.rollback()
        session.expunge_all()  # drop our unsaved row so it isn't sent again
        session.execute(update(UserSetting).where(UserSetting.user_id == user_id)
                        .values(language=body.language))
        session.commit()
    return Response(status_code=204)


@router.post("/auth/password", status_code=204)
def change_password(body: PasswordIn, request: Request, user: User = Depends(signed_in_user),
                    session: Session = Depends(get_session)) -> Response:
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Your current password is wrong.")
    auth.check_password_strength(body.new_password)
    user.password_hash = auth.hash_password(body.new_password)
    log(session, user, "user.password", "Changed own password", request=request)
    session.commit()
    return Response(status_code=204)


# --- Users (admin) ------------------------------------------------------------


@router.get("/users")
def list_users(_: User = Depends(require("admin")),
               session: Session = Depends(get_session)) -> list[dict]:
    return [user_out(u) for u in session.scalars(select(User).order_by(User.name))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, request: Request, admin: User = Depends(require("admin")),
                session: Session = Depends(get_session)) -> dict:
    auth.check_password_strength(body.password)
    user = User(email=clean_email(body.email), name=body.name.strip(), role=body.role,
                password_hash=auth.hash_password(body.password))
    client = link_client(session, user, body.role, body.client_id)
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, f"There is already a user with email {user.email}.") from exc
    log(session, admin, "user.create", f"Added {user.name} ({user.email}) as {ROLES[user.role]}"
        + (f" of {client}" if client else ""),
        details={"user_id": user.id, "client_id": user.client_id}, request=request)
    session.commit()
    return user_out(user)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request,
                admin: User = Depends(require("admin")),
                session: Session = Depends(get_session)) -> dict:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    changes = []
    if (body.role and body.role != "admin") or body.active is False or body.client_id is not None:
        # Two admins demoting each other at once must not leave no admin (nor two requests
        # both link one user to a client).
        auth.hold(session, "admins")
        session.expire_all()
        user = session.get(User, user_id)
        if user.role == "admin" and user.active and active_admins(session) <= 1:
            raise HTTPException(400, "Keep at least one active admin.")
    if body.name and body.name.strip() != user.name:
        changes.append(f"name to {body.name.strip()}")
        user.name = body.name.strip()
    role = body.role or user.role
    if role != user.role or body.client_id is not None:
        before = user.client_link.client if user.client_link else None
        client = link_client(session, user, role, body.client_id)
        if client and user.client_link.client is not before:
            changes.append(f"client to {client}")
    if body.role and body.role != user.role:
        changes.append(f"role from {ROLES[user.role]} to {ROLES[body.role]}")
        if (body.role == "client") != (user.role == "client"):
            auth.end_user_sessions(session, user)  # a different app now; sign in again
        if body.role == "client":
            left = session.scalars(select(EngagementMember).where(
                EngagementMember.user_id == user.id)).all()
            for m in left:
                session.delete(m)
            if left:
                changes.append(f"taken off {len(left)} engagements")
        user.role = body.role
    if body.active is not None and body.active != user.active:
        changes.append("reactivated" if body.active else "deactivated")
        user.active = body.active
        if not body.active:
            auth.end_user_sessions(session, user)
    if body.password:
        auth.check_password_strength(body.password)
        user.password_hash = auth.hash_password(body.password)
        auth.end_user_sessions(session, user)
        changes.append("password reset")
    if changes:
        log(session, admin, "user.update", f"Changed {user.email}: {', '.join(changes)}",
            details={"user_id": user.id, "role": user.role,
                     "client_id": user.client_link.client.id if user.client_link else None},
            request=request)
    session.commit()
    return user_out(user)


# --- Activity -----------------------------------------------------------------


@router.get("/activity")
def activity(engagement_id: int | None = None, user_id: int | None = None, action: str = "",
             limit: int = 200, user: User = Depends(current_user),
             session: Session = Depends(get_session)) -> list[dict]:
    """The activity log. An engagement's team sees its log; the full log is for admins."""
    engagement = None
    if engagement_id is None:
        auth.ensure(user, "admin")
    else:
        engagement = session.get(Engagement, engagement_id)
        if not auth.can(user, "admin") and (
                engagement is None or not auth.on_engagement(session, user, engagement_id)):
            raise HTTPException(404, "Engagement not found.")
    q = select(Activity).order_by(Activity.id.desc()).limit(max(1, min(limit, 1000)))
    if engagement_id is not None:
        q = q.where(Activity.engagement_id == engagement_id)
    if engagement is not None:
        # Databases created before ids stopped being reused can hand a deleted engagement's
        # id to a new one; its history starts when it was created.
        q = q.where(Activity.at >= engagement.created_at)
    if user_id is not None:
        q = q.where(Activity.user_id == user_id)
    if action:
        q = q.where(Activity.action.startswith(action))
    return [auth.activity_out(a) for a in session.scalars(q)]
