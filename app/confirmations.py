"""External confirmations (ISA 505): requests to banks, customers, suppliers, lawyers and
others, tracked from sending to the reply.

Requests can be added by hand, or picked from a receivables or payables listing (the largest
balances, or every balance above an amount). Each one moves from "to send" to "sent" (with any
reminders) to a reply or no reply. A reply that differs from the books by more than the clearly
trivial threshold needs an explanation; a positive request with no reply needs alternative
procedures. Every change is logged, and the list exports to Excel.
"""

from __future__ import annotations

import io
import math
from datetime import date
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy.orm import Session

from . import aging, auth
from .auth import current_user, log, require
from .db import Confirmation, Engagement, Upload, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, load_upload, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

KINDS = {"bank": "Bank", "receivable": "Customer (receivable)",
         "payable": "Supplier (payable)", "legal": "Lawyer", "other": "Other"}
FORMS = {"positive": "Positive", "negative": "Negative", "blank": "Blank (no balance shown)"}
STATUSES = {"to_send": "To send", "sent": "Sent", "replied": "Replied", "no_reply": "No reply"}
OUTCOMES = {
    "to_send": "Not sent yet",
    "waiting": "Waiting for a reply",
    "overdue": "No reply yet: send a reminder",
    "agreed": "Agreed",
    "explained": "Difference explained",
    "difference": "Difference to explain",
    "alternative_needed": "Alternative procedures needed",
    "alternative_done": "Alternative procedures done",
    "negative_no_reply": "No exception reported",
}
# Outcomes that still need work, and those that count as done.
OPEN = ("to_send", "waiting", "overdue", "difference", "alternative_needed")
LISTING_KINDS = {"ar": "receivable", "ap": "payable"}
REPLY_DAYS = 14  # days after sending (or the last reminder) before a reply is chased
MAX_CONFIRMATIONS = 1000
MAX_FROM_LISTING = 200
Party = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


def finite(v: float | None) -> float | None:
    if v is not None and not math.isfinite(v):
        raise ValueError("Enter an amount.")
    return v


class ConfirmationIn(BaseModel):
    kind: Literal["bank", "receivable", "payable", "legal", "other"]
    party: Party
    reference: Short = ""
    contact: Long = ""
    form: Literal["positive", "negative", "blank"] = "positive"
    balance: float | None = None

    _balance = field_validator("balance")(classmethod(lambda cls, v: finite(v)))


class ConfirmationPatch(BaseModel):
    kind: Literal["bank", "receivable", "payable", "legal", "other"] | None = None
    party: Party | None = None
    reference: Short | None = None
    contact: Long | None = None
    form: Literal["positive", "negative", "blank"] | None = None
    balance: float | None = None
    status: Literal["to_send", "sent", "replied", "no_reply"] | None = None
    sent_on: date | None = None
    reminder_on: date | None = None
    received_on: date | None = None
    confirmed: float | None = None
    difference_note: Long | None = None
    alternative: Long | None = None
    conclusion: Long | None = None
    workpaper: Short | None = None

    _amounts = field_validator("balance", "confirmed")(classmethod(lambda cls, v: finite(v)))


class FromListingIn(BaseModel):
    upload_id: int
    min_balance: float | None = Field(default=None, ge=0)
    largest: int | None = Field(default=None, ge=1, le=MAX_FROM_LISTING)

    _min = field_validator("min_balance")(classmethod(lambda cls, v: finite(v)))


# --- State ---------------------------------------------------------------------------

def difference(c: Confirmation) -> float | None:
    if c.status != "replied" or c.balance is None or c.confirmed is None:
        return None
    return round(c.confirmed - c.balance, 2)


def outcome(c: Confirmation, trivial: float | None, today: date | None = None) -> str:
    today = today or date.today()
    if c.status == "to_send":
        return "to_send"
    if c.status == "sent":
        if c.form == "negative":  # silence is the expected answer to a negative request
            return "negative_no_reply" if (today - c.sent_on).days > REPLY_DAYS else "waiting"
        last = max((d for d in (c.sent_on, c.reminder_on) if d is not None), default=today)
        return "overdue" if (today - last).days > REPLY_DAYS else "waiting"
    if c.status == "no_reply":
        if c.form == "negative":
            return "negative_no_reply"
        return "alternative_done" if c.alternative.strip() else "alternative_needed"
    diff = difference(c)
    if diff is None or abs(diff) < 0.005:
        return "agreed"
    if c.difference_note.strip() or abs(diff) <= (trivial or 0):
        return "explained"
    return "difference"


def confirmation_out(c: Confirmation, trivial: float | None) -> dict:
    o = outcome(c, trivial)
    return {
        "id": c.id, "engagement_id": c.engagement_id, "kind": c.kind,
        "kind_label": KINDS[c.kind], "party": c.party, "reference": c.reference,
        "contact": c.contact, "form": c.form, "form_label": FORMS[c.form],
        "balance": c.balance, "status": c.status, "status_label": STATUSES[c.status],
        "sent_on": c.sent_on.isoformat() if c.sent_on else None,
        "reminder_on": c.reminder_on.isoformat() if c.reminder_on else None,
        "received_on": c.received_on.isoformat() if c.received_on else None,
        "confirmed": c.confirmed, "difference": difference(c),
        "difference_note": c.difference_note, "alternative": c.alternative,
        "conclusion": c.conclusion, "workpaper": c.workpaper,
        "outcome": o, "outcome_label": OUTCOMES[o], "open": o in OPEN,
        "source": c.source, "created_by": c.created_by,
        "updated_by": c.updated_by, "updated_at": c.updated_at.isoformat(),
    }


def summary(e: Engagement, today: date | None = None) -> dict:
    trivial = e.trivial_threshold
    items = [(c, outcome(c, trivial, today)) for c in e.confirmations]
    asked = [c for c, _ in items if c.status != "to_send"]
    replied = [c for c, _ in items if c.status == "replied"]
    by_kind = []
    for kind, label in KINDS.items():
        mine = [(c, o) for c, o in items if c.kind == kind]
        if not mine:
            continue
        balance = sum(abs(c.balance or 0) for c, _ in mine)
        covered = sum(abs(c.balance or 0) for c, o in mine
                      if o in ("agreed", "explained", "alternative_done", "negative_no_reply"))
        by_kind.append({"kind": kind, "label": label, "requests": len(mine),
                        "replied": sum(1 for c, _ in mine if c.status == "replied"),
                        "balance": round(balance, 2), "covered": round(covered, 2),
                        "covered_pct": round(100 * covered / balance, 1) if balance else None})
    counts = {o: sum(1 for _, x in items if x == o) for o in OUTCOMES}
    return {
        "requests": len(items), "sent": len(asked), "replied": len(replied),
        "response_rate": round(100 * len(replied) / len(asked), 1) if asked else None,
        "outcomes": counts, "open": sum(counts[o] for o in OPEN),
        "differences": round(sum(difference(c) or 0 for c in replied), 2),
        "by_kind": by_kind,
    }


def sorted_confirmations(e: Engagement) -> list[Confirmation]:
    kinds = list(KINDS)
    return sorted(e.confirmations, key=lambda c: (kinds.index(c.kind), -abs(c.balance or 0),
                                                   c.party.lower(), c.id))


def list_out(e: Engagement) -> dict:
    return {"summary": summary(e), "reply_days": REPLY_DAYS,
            "trivial_threshold": e.trivial_threshold,
            "confirmations": [confirmation_out(c, e.trivial_threshold)
                              for c in sorted_confirmations(e)]}


# --- Routes ------------------------------------------------------------------------

@router.get("/confirmations/options")
def options() -> dict:
    return {"kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
            "forms": [{"id": k, "label": v} for k, v in FORMS.items()],
            "statuses": [{"id": k, "label": v} for k, v in STATUSES.items()],
            "reply_days": REPLY_DAYS, "max_from_listing": MAX_FROM_LISTING}


@router.get("/engagements/{engagement_id}/confirmations")
def list_confirmations(engagement_id: int, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> dict:
    return list_out(get_or_404(session, Engagement, engagement_id, user))


def held(session: Session, user: User, confirmation_id: int) -> Confirmation:
    engagement_id = get_or_404(session, Confirmation, confirmation_id, user).engagement_id
    auth.hold(session, f"confirmations:{engagement_id}")
    session.expire_all()
    return get_or_404(session, Confirmation, confirmation_id, user)


def room_for(e: Engagement, n: int) -> None:
    if len(e.confirmations) + n > MAX_CONFIRMATIONS:
        raise HTTPException(400, f"An engagement can have at most {MAX_CONFIRMATIONS} "
                                 "confirmations.")


@router.post("/engagements/{engagement_id}/confirmations", status_code=201)
def create_confirmation(engagement_id: int, body: ConfirmationIn,
                        user: User = Depends(require("prepare")),
                        session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"confirmations:{engagement_id}")
    room_for(e, 1)
    c = Confirmation(engagement=e, created_by=user.name, updated_by=user.name,
                     **body.model_dump())
    session.add(c)
    session.flush()
    log(session, user, "confirmation.create",
        f"Added a confirmation request to {c.party}", e,
        details={"confirmation_id": c.id, "kind": c.kind, "balance": c.balance})
    session.commit()
    return confirmation_out(c, e.trivial_threshold)


@router.post("/engagements/{engagement_id}/confirmations/from-listing", status_code=201)
def from_listing(engagement_id: int, body: FromListingIn,
                 user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    """Add a request for each customer or vendor on a listing picked by balance: those above
    `min_balance`, the `largest` few, or the largest few above the amount. Parties already
    on the list are skipped."""
    if body.min_balance is None and body.largest is None:
        raise HTTPException(400, "Say which balances to confirm: those above an amount, the "
                                 "largest few, or both.")
    e = get_or_404(session, Engagement, engagement_id, user)
    upload = get_or_404(session, Upload, body.upload_id, user)
    if upload.engagement_id != e.id or upload.kind not in LISTING_KINDS:
        raise HTTPException(400, "Pick a receivables or payables listing from this "
                                 "engagement.")
    kind = LISTING_KINDS[upload.kind]
    try:  # read before holding the guard, as a large listing takes a while
        aged = aging.age(load_upload(upload).df, upload.kind, e.period_end)
    except aging.AgingError as exc:
        raise HTTPException(400, str(exc)) from exc
    parties = sorted((p for p in aged["parties"] if p["balance"] > 0),
                     key=lambda p: -p["balance"])
    if body.min_balance is not None:
        parties = [p for p in parties if p["balance"] >= body.min_balance]
    if body.largest is None and len(parties) > MAX_FROM_LISTING:
        raise HTTPException(400, f"{len(parties)} balances are that large, and at most "
                                 f"{MAX_FROM_LISTING} can be added at once. Raise the amount, "
                                 "or add the largest few.")
    parties = parties[:min(body.largest or MAX_FROM_LISTING, MAX_FROM_LISTING)]
    e = held_engagement(session, user, engagement_id, f"confirmations:{engagement_id}")
    have = {(c.kind, (c.reference or c.party).strip().lower()) for c in e.confirmations}
    new = []
    for p in parties:
        key = (kind, str(p["party"]).strip().lower())
        if key in have:
            continue
        have.add(key)
        new.append(p)
    room_for(e, len(new))
    for p in new:
        name = str(p["name"] or p["party"])[:300]
        session.add(Confirmation(engagement=e, kind=kind, party=name,
                                 reference=str(p["party"])[:200], balance=p["balance"],
                                 source=f"upload:{upload.id}", created_by=user.name,
                                 updated_by=user.name))
    session.flush()
    log(session, user, "confirmation.import",
        f"Added {len(new)} confirmation requests from {upload.filename}", e,
        details={"upload_id": upload.id, "sha256": upload.sha256, "added": len(new),
                 "skipped": len(parties) - len(new), "min_balance": body.min_balance,
                 "largest": body.largest,
                 "balance": round(sum(p["balance"] for p in new), 2)})
    session.commit()
    return {"added": len(new), "skipped": len(parties) - len(new), **list_out(e)}


@router.patch("/confirmations/{confirmation_id}")
def update_confirmation(confirmation_id: int, body: ConfirmationPatch,
                        user: User = Depends(require("prepare")),
                        session: Session = Depends(get_session)) -> dict:
    c = held(session, user, confirmation_id)
    changes = body.model_dump(exclude_unset=True)
    # Amounts and dates can be cleared; text and choices left null stay as they are.
    clearable = {"balance", "confirmed", "sent_on", "reminder_on", "received_on"}
    changes = {k: v for k, v in changes.items() if v is not None or k in clearable}
    was = c.status
    for field, value in changes.items():
        setattr(c, field, value)
    today = date.today()
    if c.status in ("sent", "replied", "no_reply") and c.sent_on is None:
        c.sent_on = today
    if c.status == "replied" and c.received_on is None:
        c.received_on = today
    if c.status == "to_send":
        c.sent_on = c.reminder_on = c.received_on = None
    if c.status != "replied":
        c.received_on = None
    for later, name in ((c.reminder_on, "reminder"), (c.received_on, "reply")):
        if later is not None and c.sent_on is not None and later < c.sent_on:
            raise HTTPException(400, f"The {name} can't be dated before the request was sent.")
    if c.status == "replied" and c.kind not in ("legal", "other") and c.confirmed is None \
            and (c.balance is not None or c.form == "blank"):
        raise HTTPException(400, "Enter the balance the reply confirms.")
    c.updated_by, c.updated_at = user.name, utcnow()
    o = outcome(c, c.engagement.trivial_threshold)
    what = (f"Marked the confirmation from {c.party} as {STATUSES[c.status].lower()}"
            if c.status != was else f"Changed the confirmation request to {c.party}")
    log(session, user, "confirmation.update", what, c.engagement,
        details={"confirmation_id": c.id, "changed": sorted(changes), "status": c.status,
                 "outcome": o, "difference": difference(c)})
    session.commit()
    return confirmation_out(c, c.engagement.trivial_threshold)


@router.delete("/confirmations/{confirmation_id}", status_code=204)
def delete_confirmation(confirmation_id: int, user: User = Depends(require("prepare")),
                        session: Session = Depends(get_session)) -> Response:
    c = held(session, user, confirmation_id)
    if c.status != "to_send" and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can remove a request that has been sent.")
    log(session, user, "confirmation.delete", f"Removed the confirmation request to {c.party}",
        c.engagement, details={"confirmation_id": c.id, "status": c.status,
                               "balance": c.balance, "confirmed": c.confirmed})
    session.delete(c)
    session.commit()
    return Response(status_code=204)


# --- Excel -------------------------------------------------------------------------

def to_excel(e: Engagement, context: dict) -> bytes:
    s = summary(e)
    about = list(context.items()) + [
        ("Requests", s["requests"]), ("Sent", s["sent"]), ("Replied", s["replied"]),
        ("Response rate %", s["response_rate"]),
        ("Clearly trivial threshold", e.trivial_threshold),
        ("Still open", s["open"]), ("Net differences in replies", s["differences"]),
    ]
    rows = []
    for n, c in enumerate(sorted_confirmations(e), 1):
        out = confirmation_out(c, e.trivial_threshold)
        rows.append({
            "Ref": f"K{n}", "Type": out["kind_label"], "Party": c.party,
            "Account or code": c.reference, "Contact": c.contact, "Form": out["form_label"],
            "Balance per books": c.balance, "Status": out["status_label"],
            "Sent": out["sent_on"] or "", "Reminder": out["reminder_on"] or "",
            "Reply received": out["received_on"] or "", "Confirmed balance": c.confirmed,
            "Difference": out["difference"], "Outcome": out["outcome_label"],
            "Explanation of difference": c.difference_note,
            "Alternative procedures": c.alternative, "Conclusion": c.conclusion,
            "Workpaper": c.workpaper})
    coverage = [{"Type": k["label"], "Requests": k["requests"], "Replied": k["replied"],
                 "Balance asked about": k["balance"], "Balance covered": k["covered"],
                 "Covered %": k["covered_pct"]} for k in s["by_kind"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(coverage, columns=["Type", "Requests", "Replied", "Balance asked about",
                                        "Balance covered", "Covered %"]).to_excel(
            xl, sheet_name="Coverage", index=False)
        pd.DataFrame(rows, columns=[
            "Ref", "Type", "Party", "Account or code", "Contact", "Form", "Balance per books",
            "Status", "Sent", "Reminder", "Reply received", "Confirmed balance", "Difference",
            "Outcome", "Explanation of difference", "Alternative procedures", "Conclusion",
            "Workpaper"]).to_excel(xl, sheet_name="Confirmations", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.get("/engagements/{engagement_id}/confirmations/export")
def export_confirmations(engagement_id: int, user: User = Depends(current_user),
                         session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "confirmation.download", "Downloaded the confirmations workpaper", e,
        details={"confirmations": len(e.confirmations)})
    session.commit()
    name, content = workbook(e)
    return Response(content, media_type=XLSX, headers=attachment(name))


def workbook(e: Engagement) -> tuple[str, bytes]:
    """The confirmations workpaper as an Excel file, and its name."""
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat()}
    stem = safe_stem(f"{e.client.name}-{e.name}-confirmations")
    return f"{stem}.xlsx", to_excel(e, context)
