"""Management responses and action plans, as in TeamMate+ and AuditBoard: the auditors send a
finding to the client with their recommendation; management answers in the client portal
with their response, the action they will take, who owns it and by when; the auditors later
follow up whether it was done. The team can also record a response that came by email or in a
meeting.

Client users see only the findings sent to them, for their own client's engagements, and only
what the auditors chose to show: the finding, its severity and the recommendation, never the
notes, evidence or review trail. Everything is logged; a locked file takes no more answers.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, register
from .auth import current_user, log, require, signed_in_user
from .db import Engagement, Finding, FindingResponse, User, get_session, utcnow
from .engagements import get_or_404, safe_stem
from .exports import attachment, finish_workbook
from .portal import client_user, is_client

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FOLLOW_UP = {
    "open": "Not followed up yet",
    "implemented": "Implemented",
    "partly": "Partly implemented",
    "not_implemented": "Not implemented",
}
MISSING = "Finding not found."

Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class AskIn(BaseModel):
    recommendation: Long = ""


class AnswerIn(BaseModel):
    response: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                               max_length=5000)]
    action_plan: Long = ""
    owner: str = Field(default="", max_length=200)
    due_on: date | None = None


class FollowUpIn(BaseModel):
    status: Literal["open", "implemented", "partly", "not_implemented"]
    note: Long = ""


def state(r: FindingResponse) -> str:
    if not r.response:
        return "waiting"
    return "overdue" if (r.follow_up == "open" and r.due_on and r.due_on < date.today()) \
        else "answered"


STATES = {"waiting": "Waiting for the client", "answered": "Answered",
          "overdue": "Action overdue"}


def response_out(r: FindingResponse | None) -> dict | None:
    if r is None:
        return None
    s = state(r)
    return {"finding_id": r.finding_id, "recommendation": r.recommendation,
            "asked_by": r.asked_by, "asked_at": r.asked_at.isoformat(),
            "response": r.response, "action_plan": r.action_plan, "owner": r.owner,
            "due_on": r.due_on.isoformat() if r.due_on else None,
            "responded_by": r.responded_by,
            "responded_at": r.responded_at.isoformat() if r.responded_at else None,
            "from_client": r.from_client, "state": s, "state_label": STATES[s],
            "follow_up": r.follow_up, "follow_up_label": FOLLOW_UP[r.follow_up],
            "follow_up_note": r.follow_up_note, "followed_up_by": r.followed_up_by,
            "followed_up_at": r.followed_up_at.isoformat() if r.followed_up_at else None}


# What a client user sees of the response: not the auditors' follow-up note or who asked.
CLIENT_FIELDS = ("finding_id", "recommendation", "asked_at", "response", "action_plan", "owner",
                 "due_on", "responded_by", "responded_at", "from_client", "state",
                 "state_label", "follow_up", "follow_up_label")


def finding_view(f: Finding) -> dict:
    return {"id": f.id, "severity": f.severity, "check_name": f.check_name,
            "source": f.source, "detail": f.detail, "description": f.description,
            "account": f.account, "date": f.date, "amount": f.amount, "entry_id": f.entry_id}


def client_view(f: Finding) -> dict:
    """What a client user sees of a finding sent to them."""
    r = response_out(f.response)
    return {**finding_view(f), **{k: r[k] for k in CLIENT_FIELDS}}


def staff_finding(session: Session, user: User, finding_id: int) -> Finding:
    """The finding, held so changes to its response run one at a time."""
    get_or_404(session, Finding, finding_id, user)
    auth.hold(session, f"finding:{finding_id}")
    session.expire_all()
    return get_or_404(session, Finding, finding_id, user)


@router.put("/findings/{finding_id}/response-request")
def ask_client(finding_id: int, body: AskIn, user: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> dict:
    """Send the finding to the client for their response (or change the recommendation)."""
    f = staff_finding(session, user, finding_id)
    r = f.response
    if r is None:
        f.response = r = FindingResponse(engagement_id=f.engagement_id, asked_by=user.name,
                                         asked_at=utcnow())
    r.recommendation = body.recommendation
    register.touch(f)
    log(session, user, "finding.ask_client",
        f"Sent finding #{f.id} to the client for a management response", f.engagement,
        details={"finding_id": f.id, "recommendation": body.recommendation})
    session.commit()
    return response_out(f.response)


@router.delete("/findings/{finding_id}/response-request", status_code=204)
def withdraw(finding_id: int, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> Response:
    f = staff_finding(session, user, finding_id)
    if f.response is None:
        raise HTTPException(404, "This finding hasn't been sent to the client.")
    if f.response.response:
        raise HTTPException(409, "The client has already answered, so the request stays on "
                                 "file.")
    log(session, user, "finding.withdraw_client",
        f"Took finding #{f.id} back from the client", f.engagement,
        details={"finding_id": f.id})
    f.response = None
    register.touch(f)
    session.commit()
    return Response(status_code=204)


@router.put("/findings/{finding_id}/response")
def answer(finding_id: int, body: AnswerIn, user: User = Depends(signed_in_user),
           session: Session = Depends(get_session)) -> dict:
    """Management's response: from a client user in the portal, or recorded by the team."""
    if is_client(user):
        row = session.execute(select(Engagement.client_id).join(Finding)
                              .where(Finding.id == finding_id)).first()
        if row is None or user.client_id is None or row.client_id != user.client_id:
            raise HTTPException(404, MISSING)
        auth.hold(session, f"finding:{finding_id}")
        session.expire_all()
        f = session.get(Finding, finding_id)
        if f is None or f.response is None:
            raise HTTPException(404, MISSING)
    else:
        auth.ensure(user, "prepare")
        f = staff_finding(session, user, finding_id)
        if f.response is None:
            raise HTTPException(400, "Send the finding to the client first.")
    r = f.response
    if r.follow_up != "open":
        raise HTTPException(409, "The auditors have already followed this up, so the response "
                                 "can't be changed.")
    changed = bool(r.response)
    r.response, r.action_plan, r.owner = body.response, body.action_plan, body.owner.strip()
    r.due_on = body.due_on
    r.responded_by, r.responded_at, r.from_client = user.name, utcnow(), is_client(user)
    register.touch(f)
    log(session, user, "finding.response",
        f"{'Changed' if changed else 'Gave'} the management response to finding #{f.id}"
        + ("" if is_client(user) else " (recorded by the team)"), f.engagement,
        details={"finding_id": f.id, "owner": r.owner, "due_on": body.due_on.isoformat()
                 if body.due_on else None, "from_client": r.from_client})
    session.commit()
    return client_view(f) if is_client(user) else response_out(r)


@router.put("/findings/{finding_id}/follow-up")
def follow_up(finding_id: int, body: FollowUpIn, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    f = staff_finding(session, user, finding_id)
    r = f.response
    if r is None or not r.response:
        raise HTTPException(400, "There is no management response to follow up yet.")
    if body.status != "open" and not body.note:
        raise HTTPException(400, "Say what you checked to follow it up.")
    r.follow_up, r.follow_up_note = body.status, body.note
    r.followed_up_by, r.followed_up_at = (user.name, utcnow()) if body.status != "open" \
        else ("", None)
    register.touch(f)
    log(session, user, "finding.follow_up",
        f"Followed up the action on finding #{f.id}: {FOLLOW_UP[body.status]}", f.engagement,
        details={"finding_id": f.id, "status": body.status})
    session.commit()
    return response_out(r)


def engagement_rows(session: Session, e: Engagement) -> list[dict]:
    q = (select(Finding).join(FindingResponse).where(Finding.engagement_id == e.id)
         .order_by(Finding.id))
    return [{**finding_view(f), **response_out(f.response), "status": f.status}
            for f in session.scalars(q)]


@router.get("/engagements/{engagement_id}/responses")
def list_responses(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    rows = engagement_rows(session, e)
    count = {s: sum(1 for r in rows if r["state"] == s) for s in STATES}
    return {"rows": rows, "summary": {"total": len(rows), **count}}


@router.get("/engagements/{engagement_id}/responses/export")
def export_responses(engagement_id: int, user: User = Depends(current_user),
                     session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "finding.response_download", "Downloaded the management responses", e)
    session.commit()
    name, content = workbook(session, e)
    return Response(content, media_type=XLSX, headers=attachment(name))


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    rows = engagement_rows(session, e)
    table = pd.DataFrame([{
        "Finding": r["id"], "Severity": r["severity"], "What was found": r["check_name"],
        "Detail": r["detail"], "Account": r["account"], "Amount": r["amount"],
        "Recommendation": r["recommendation"], "Management response": r["response"],
        "Action plan": r["action_plan"], "Owner": r["owner"], "Due": r["due_on"],
        "Answered by": r["responded_by"] + (" (client)" if r["from_client"] else ""),
        "State": r["state_label"], "Follow-up": r["follow_up_label"],
        "Follow-up note": r["follow_up_note"], "Followed up by": r["followed_up_by"]}
        for r in rows], columns=["Finding", "Severity", "What was found", "Detail", "Account",
                                 "Amount", "Recommendation", "Management response",
                                 "Action plan", "Owner", "Due", "Answered by", "State",
                                 "Follow-up", "Follow-up note", "Followed up by"])
    about = pd.DataFrame([("Client", e.client.name), ("Engagement", e.name),
                          ("Period end", e.period_end.isoformat()),
                          ("Findings sent to the client", len(rows)),
                          *[(label, sum(1 for r in rows if r["state"] == s))
                            for s, label in STATES.items()]], columns=["Item", "Value"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        about.to_excel(xl, sheet_name="About", index=False)
        table.to_excel(xl, sheet_name="Responses", index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-management-responses')}.xlsx", buf.getvalue()


@router.get("/portal/findings")
def portal_findings(user: User = Depends(client_user),
                    session: Session = Depends(get_session)) -> dict:
    """The findings sent to a client user's client, engagement by engagement."""
    link = user.client_link
    if link is None:
        return {"engagements": []}
    out = []
    for e in sorted(link.client.engagements, key=lambda e: e.period_end, reverse=True):
        q = (select(Finding).join(FindingResponse).where(Finding.engagement_id == e.id)
             .order_by(Finding.id))
        found = [client_view(f) for f in session.scalars(q)]
        if found:
            out.append({"id": e.id, "name": e.name, "period_end": e.period_end.isoformat(),
                        "locked": e.lock is not None, "findings": found})
    return {"engagements": out}
