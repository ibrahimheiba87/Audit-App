"""Client document requests: the audit team asks for documents, the client uploads them.

Client users (auth.ROLES) see only the requests of their own client's engagements, and only
through these endpoints. Everything on a request (title, details, files and messages) is
visible to the client.

Status flow:
    open       waiting for the client
    submitted  the client (or the team on their behalf) sent at least one file
    returned   the team sent it back with a message; the client can send more
    accepted   the team accepted the files; nothing more can be added or removed
"""

from __future__ import annotations

import hashlib
import io
from datetime import date
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth
from .auth import log, require, signed_in_user
from .db import (
    DocumentRequest,
    Engagement,
    Guard,
    RequestFile,
    RequestMessage,
    User,
    get_session,
    utcnow,
)
from .engagements import get_or_404, safe_stem
from .exports import attachment, csv_safe_frame, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_FILE_BYTES = 20 * 1024 * 1024

STATUSES = {"open": "Waiting for client", "submitted": "Submitted", "returned": "Returned to client",
            "accepted": "Accepted"}
OUTSTANDING = ("open", "returned")  # the client still owes something

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class RequestIn(BaseModel):
    title: Title
    details: str = Field(default="", max_length=5000)
    due_date: date | None = None


class RequestPatch(BaseModel):
    title: Title | None = None
    details: str | None = Field(default=None, max_length=5000)
    due_date: date | None = None


class StatusIn(BaseModel):
    status: Literal["accepted", "returned"]
    message: str = Field(default="", max_length=5000)


class MessageIn(BaseModel):
    text: Text


# --- Access ---------------------------------------------------------------------


def is_client(user: User) -> bool:
    return user.role == "client"


def client_user(user: User = Depends(signed_in_user)) -> User:
    """Dependency: a signed-in client user."""
    if not is_client(user):
        raise HTTPException(403, "This page is for client accounts.")
    return user


def check_access(user: User, engagement: Engagement, permission: str = "prepare") -> None:
    """Clients may act on their own client's engagements; the team needs `permission`."""
    if is_client(user):
        if user.client_id is None or engagement.client_id != user.client_id:
            # Same answer as a missing request, so clients can't probe other clients' ids.
            raise HTTPException(404, "Request not found.")
    else:
        auth.ensure(user, permission)


def lock_request(session: Session, user: User, request_id: int,
                 permission: str = "prepare") -> DocumentRequest:
    """The request, after holding its engagement's requests so changes run one at a time."""
    engagement_id = session.scalar(select(DocumentRequest.engagement_id)
                                   .where(DocumentRequest.id == request_id))
    if engagement_id is None:
        raise HTTPException(404, "Request not found.")
    auth.hold(session, f"requests:{engagement_id}")
    session.expire_all()
    req = session.get(DocumentRequest, request_id)
    if req is None:
        raise HTTPException(404, "Request not found.")
    check_access(user, req.engagement, permission)
    return req


def next_number(session: Session, engagement_id: int) -> int:
    """The next request number for the engagement. Numbers of deleted requests aren't reused.

    Holding the counter row also serializes request creation for the engagement.
    """
    key = f"request-number:{engagement_id}"
    auth.hold(session, key)
    counter = session.get(Guard, key)
    session.refresh(counter)
    highest = session.scalar(select(DocumentRequest.number).where(
        DocumentRequest.engagement_id == engagement_id).order_by(
        DocumentRequest.number.desc()).limit(1)) or 0
    counter.n = max(counter.n, highest + 1)
    session.flush()
    return counter.n


def set_status(req: DocumentRequest, user: User, status: str, text: str = "") -> None:
    req.status = status
    req.messages.append(RequestMessage(text=text, author=user.name, author_id=user.id,
                                       from_client=is_client(user), status_to=status))


def touch(req: DocumentRequest) -> None:
    req.updated_at = utcnow()


def label(req: DocumentRequest) -> str:
    return f"request {req.number} ({req.title})"


# --- Serialization ----------------------------------------------------------------


def file_out(f: RequestFile) -> dict:
    return {"id": f.id, "filename": f.filename, "size": f.size, "sha256": f.sha256,
            "uploaded_by": f.uploaded_by, "uploaded_by_id": f.uploaded_by_id,
            "from_client": f.from_client, "uploaded_at": f.uploaded_at.isoformat()}


def message_out(m: RequestMessage) -> dict:
    return {"id": m.id, "text": m.text, "author": m.author, "from_client": m.from_client,
            "status_to": m.status_to,
            "status_label": STATUSES[m.status_to] if m.status_to else None, "at": m.at.isoformat()}


def request_out(req: DocumentRequest) -> dict:
    today = date.today()
    return {"id": req.id, "engagement_id": req.engagement_id, "number": req.number,
            "title": req.title, "details": req.details,
            "due_date": req.due_date.isoformat() if req.due_date else None,
            "overdue": bool(req.due_date and req.due_date < today and req.status in OUTSTANDING),
            "status": req.status, "status_label": STATUSES[req.status],
            "created_by": req.created_by, "created_at": req.created_at.isoformat(),
            "updated_at": req.updated_at.isoformat(),
            "files": [file_out(f) for f in req.files],
            "messages": [message_out(m) for m in req.messages]}


def summary(requests: list[DocumentRequest]) -> dict:
    out = {s: 0 for s in STATUSES}
    for r in requests:
        out[r.status] += 1
    out["total"] = len(requests)
    out["overdue"] = sum(1 for r in requests if request_out(r)["overdue"])
    return out


# --- Audit team -------------------------------------------------------------------


@router.get("/engagements/{engagement_id}/requests")
def list_requests(engagement_id: int, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id)
    contacts = [{"name": cu.user.name, "email": cu.user.email, "active": cu.user.active}
                for cu in e.client.portal_users]
    return {"requests": [request_out(r) for r in e.document_requests],
            "summary": summary(e.document_requests), "statuses": STATUSES,
            "contacts": sorted(contacts, key=lambda c: c["name"].lower())}


@router.post("/engagements/{engagement_id}/requests", status_code=201)
def create_request(engagement_id: int, body: RequestIn, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    number = next_number(session, engagement_id)  # holds before any other read or write
    e = get_or_404(session, Engagement, engagement_id)
    req = DocumentRequest(engagement=e, number=number, title=body.title,
                          details=body.details.strip(), due_date=body.due_date,
                          created_by=user.name)
    session.add(req)
    session.flush()
    log(session, user, "request.create", f"Asked the client for {label(req)}", e,
        details={"request_id": req.id, "due_date": str(body.due_date or "")})
    session.commit()
    return request_out(req)


@router.patch("/requests/{request_id}")
def update_request(request_id: int, body: RequestPatch, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    req = lock_request(session, user, request_id)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is None:
        raise HTTPException(400, "A request needs a title.")
    if "details" in changes:
        changes["details"] = (changes["details"] or "").strip()
    changed = {k: v for k, v in changes.items() if getattr(req, k) != v}
    for key, value in changed.items():
        setattr(req, key, value)
    if changed:
        touch(req)
        log(session, user, "request.update", f"Edited {label(req)}", req.engagement,
            details={"request_id": req.id, **{k: str(v or "") for k, v in changed.items()}})
    session.commit()
    return request_out(req)


@router.post("/requests/{request_id}/status")
def change_status(request_id: int, body: StatusIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    req = lock_request(session, user, request_id)
    message = body.message.strip()
    if body.status == "accepted":
        if req.status != "submitted":
            raise HTTPException(400, "Only submitted requests can be accepted.")
        if not req.files:
            raise HTTPException(400, "There are no files to accept.")
        set_status(req, user, "accepted", message)
        log(session, user, "request.accept", f"Accepted the files for {label(req)}",
            req.engagement, details={"request_id": req.id,
                                     "files": [f.sha256 for f in req.files]})
    else:
        if req.status not in ("submitted", "accepted"):
            raise HTTPException(400, "Only submitted or accepted requests can be returned.")
        if not message:
            raise HTTPException(400, "Tell the client what's missing or wrong.")
        set_status(req, user, "returned", message)
        log(session, user, "request.return", f"Returned {label(req)} to the client: {message}",
            req.engagement, details={"request_id": req.id})
    touch(req)
    session.commit()
    return request_out(req)


@router.delete("/requests/{request_id}", status_code=204)
def delete_request(request_id: int, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> Response:
    req = lock_request(session, user, request_id)
    if req.files:
        auth.ensure(user, "review")  # removes documents the client sent
    log(session, user, "request.delete", f"Deleted {label(req)} with {len(req.files)} files",
        req.engagement, details={"request_id": req.id, "number": req.number,
                                 "files": [{"filename": f.filename, "sha256": f.sha256}
                                           for f in req.files]})
    session.delete(req)
    session.commit()
    return Response(status_code=204)


@router.get("/engagements/{engagement_id}/requests/export")
def export_requests(engagement_id: int, format: str = "xlsx",
                    user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> Response:
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "Format must be csv or xlsx.")
    e = get_or_404(session, Engagement, engagement_id)
    log(session, user, "request.download", f"Downloaded the client request list ({format})", e)
    session.commit()
    stem = safe_stem(f"{e.client.name}-{e.name}-requests")
    rows = pd.DataFrame([{
        "No.": r.number, "Request": r.title, "Details": r.details,
        "Due": r.due_date.isoformat() if r.due_date else "", "Status": STATUSES[r.status],
        "Overdue": "Yes" if request_out(r)["overdue"] else "",
        "Files": ", ".join(f.filename for f in r.files),
        "Asked by": r.created_by, "Asked (UTC)": f"{r.created_at:%Y-%m-%d %H:%M}",
        "Last updated (UTC)": f"{r.updated_at:%Y-%m-%d %H:%M}",
    } for r in e.document_requests], columns=[
        "No.", "Request", "Details", "Due", "Status", "Overdue", "Files", "Asked by",
        "Asked (UTC)", "Last updated (UTC)",
    ])
    if format == "csv":
        return Response(csv_safe_frame(rows).to_csv(index=False), media_type="text/csv",
                        headers=attachment(f"{stem}.csv"))
    files = pd.DataFrame([{
        "Request": r.number, "File": f.filename, "Size (bytes)": f.size, "SHA-256": f.sha256,
        "Sent by": f.uploaded_by + (" (client)" if f.from_client else ""),
        "Sent (UTC)": f"{f.uploaded_at:%Y-%m-%d %H:%M}",
    } for r in e.document_requests for f in r.files], columns=[
        "Request", "File", "Size (bytes)", "SHA-256", "Sent by", "Sent (UTC)",
    ])
    messages = pd.DataFrame([{
        "Request": r.number, "When (UTC)": f"{m.at:%Y-%m-%d %H:%M}",
        "Who": m.author + (" (client)" if m.from_client else ""),
        "Status change": STATUSES[m.status_to] if m.status_to else "", "Message": m.text,
    } for r in e.document_requests for m in r.messages], columns=[
        "Request", "When (UTC)", "Who", "Status change", "Message",
    ])
    counts = summary(e.document_requests)
    info = [("Client", e.client.name), ("Engagement", e.name),
            ("Period end", e.period_end.isoformat())] + [
        (STATUSES[s], counts[s]) for s in STATUSES] + [
        ("Overdue", counts["overdue"]), ("Total", counts["total"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(info, columns=["Item", "Value"]).to_excel(xl, sheet_name="Summary",
                                                               index=False)
        rows.to_excel(xl, sheet_name="Requests", index=False)
        files.to_excel(xl, sheet_name="Files", index=False)
        messages.to_excel(xl, sheet_name="Messages", index=False)
        finish_workbook(xl.book)
    return Response(buf.getvalue(), media_type=XLSX, headers=attachment(f"{stem}.xlsx"))


# --- Client portal ----------------------------------------------------------------


@router.get("/portal/requests")
def portal_requests(user: User = Depends(client_user),
                    session: Session = Depends(get_session)) -> dict:
    """Everything a client user can see: their client's engagements and requests."""
    link = user.client_link
    if link is None:
        return {"client": None, "engagements": []}
    engagements = [{
        "id": e.id, "name": e.name, "period_end": e.period_end.isoformat(),
        "requests": [request_out(r) for r in e.document_requests],
        "summary": summary(e.document_requests),
    } for e in sorted(link.client.engagements, key=lambda e: e.period_end, reverse=True)
        if e.document_requests]
    return {"client": link.client.name, "engagements": engagements}


# --- Both -------------------------------------------------------------------------


@router.post("/requests/{request_id}/files", status_code=201)
async def upload_file(request_id: int, file: UploadFile = File(...),
                      user: User = Depends(signed_in_user),
                      session: Session = Depends(get_session)) -> dict:
    content = await file.read()
    if not content:
        raise HTTPException(400, "The file is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(413, "Files can be up to 20 MB.")
    req = lock_request(session, user, request_id)
    if req.status == "accepted":
        raise HTTPException(400, "This request has been accepted, so no more files can be "
                                 "added.")
    f = RequestFile(filename=(file.filename or "document")[:500],
                    content_type=(file.content_type or "application/octet-stream")[:200],
                    content=content, size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(), uploaded_by=user.name,
                    uploaded_by_id=user.id, from_client=is_client(user))
    req.files.append(f)
    if req.status != "submitted":
        set_status(req, user, "submitted")
    touch(req)
    log(session, user, "request.upload", f"Sent {f.filename} for {label(req)}", req.engagement,
        details={"request_id": req.id, "sha256": f.sha256, "size": f.size})
    session.commit()
    return request_out(req)


@router.get("/request-files/{file_id}")
def download_file(file_id: int, user: User = Depends(signed_in_user),
                  session: Session = Depends(get_session)) -> Response:
    f = session.get(RequestFile, file_id)
    if f is None:
        raise HTTPException(404, "File not found.")
    req = f.request
    check_access(user, req.engagement)
    log(session, user, "request.file_download", f"Downloaded {f.filename} from {label(req)}",
        req.engagement, details={"request_id": req.id, "file_id": f.id, "sha256": f.sha256,
                                 "size": f.size})
    session.commit()
    # Served as a plain download whatever the sender called it, so an uploaded web page
    # can't run in the app's origin.
    return Response(f.content, media_type="application/octet-stream",
                    headers={**attachment(f.filename), "X-Content-Type-Options": "nosniff"})


@router.delete("/request-files/{file_id}", status_code=204)
def delete_file(file_id: int, user: User = Depends(signed_in_user),
                session: Session = Depends(get_session)) -> Response:
    request_id = session.scalar(select(RequestFile.request_id).where(RequestFile.id == file_id))
    if request_id is None:
        raise HTTPException(404, "File not found.")
    req = lock_request(session, user, request_id)
    f = session.get(RequestFile, file_id)
    if f is None or f.request_id != req.id:
        raise HTTPException(404, "File not found.")
    if req.status == "accepted":
        raise HTTPException(400, "Return the request to the client before removing its files.")
    if is_client(user):
        if f.uploaded_by_id != user.id:
            raise HTTPException(403, "You can only remove files you sent.")
    elif f.uploaded_by_id != user.id:
        auth.ensure(user, "review")  # someone else's document
    text = f"Removed {f.filename} (SHA-256 {f.sha256})"
    req.files.remove(f)
    req.messages.append(RequestMessage(text=text, author=user.name, author_id=user.id,
                                       from_client=is_client(user)))
    if not req.files and req.status == "submitted":
        set_status(req, user, "open")
    touch(req)
    log(session, user, "request.file_remove", f"{text} from {label(req)}", req.engagement,
        details={"request_id": req.id, "sha256": f.sha256})
    session.commit()
    return Response(status_code=204)


@router.post("/requests/{request_id}/messages", status_code=201)
def add_message(request_id: int, body: MessageIn, user: User = Depends(signed_in_user),
                session: Session = Depends(get_session)) -> dict:
    req = lock_request(session, user, request_id)
    req.messages.append(RequestMessage(text=body.text, author=user.name, author_id=user.id,
                                       from_client=is_client(user)))
    touch(req)
    log(session, user, "request.message", f"Message on {label(req)}: {body.text}",
        req.engagement, details={"request_id": req.id})
    session.commit()
    return request_out(req)
