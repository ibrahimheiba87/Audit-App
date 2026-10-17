"""Vouching sample items to their source documents (ISA 500), the way DataSnipper does it:
the auditor attaches the invoice, receipt or contract behind a sampled ledger line, the AI
assistant reads it, and the app compares what it says with the line: the amount, the date
(and which side of the period end it falls), the other party and the document number.

Nothing the model reads is trusted as it stands: the comparison only marks the item as
agreeing or not and suggests an audited amount, which the auditor still types or accepts and
saves with the sample's results. The document is kept with the sample as evidence, goes in the
sample's workpaper and the audit file pack, and each read is logged.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import assistant, auth, doctext
from .ai_tools import (
    DATA_ONLY,
    MAX_DOCUMENT_BYTES,
    _date,
    _number,
    _text,
    ask,
    document_block,
    require_ai,
)
from .auth import current_user, log, require
from .db import Sample, User, Vouching, get_session, utcnow
from .engagements import get_or_404
from .exports import attachment

router = APIRouter(prefix="/api")

DATE_DAYS = 31        # a document dated further than this from the entry is worth a look
CENT = 0.005
STATUSES = {
    "agrees": "Agrees with the document",
    "differs": "Differs from the document",
    "check": "Check by hand",
}
VOUCH_SYSTEM = (
    "You read the source documents behind ledger entries (invoices, receipts, credit notes, "
    "contracts, delivery notes, payment advices) for a financial statement auditor. "
    + DATA_ONLY +
    "Copy what the document says; never guess or fill in a figure that isn't there. "
    "Answer with JSON only, no other text, in exactly this shape: "
    '{"document_type": "<invoice, receipt, credit note, contract, ...>", '
    '"issuer": "<who issued it>", "recipient": "<who it is addressed to>", '
    '"number": "<document number or null>", "date": "YYYY-MM-DD or null", '
    '"currency": "...", "net": <number or null>, "tax": <number or null>, '
    '"total": <number or null>, "summary": "<one short sentence on what it is for>"}. '
    "Numbers are positive, use a dot for decimals and no thousands separators. Write the date "
    "the way the document's own dates show it is written (day first or month first).")
LANGUAGE = {"ar": " Write the summary in Modern Standard Arabic."}
WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
STOP = {"limited", "company", "trading", "group", "holding", "holdings", "services",
        "international", "general", "invoice", "bank"}


def item_of(x: Sample, row: int) -> dict:
    item = next((i for i in x.result["items"] if i["row"] == row), None)
    if item is None:
        raise HTTPException(404, f"Row {row} isn't in sample #{x.id}.")
    return item


def clean(answer: dict) -> dict:
    return {"document_type": _text(answer.get("document_type"), 100),
            "issuer": _text(answer.get("issuer"), 200),
            "recipient": _text(answer.get("recipient"), 200),
            "number": _text(answer.get("number"), 100),
            "date": (d.isoformat() if (d := _date(answer.get("date"))) else None),
            "currency": _text(answer.get("currency"), 20),
            "net": _number(answer.get("net")), "tax": _number(answer.get("tax")),
            "total": _number(answer.get("total")),
            "summary": _text(answer.get("summary"), 500)}


def words(value) -> set[str]:
    return {w.lower() for w in WORD.findall(str(value or ""))} - STOP


def compare(item: dict, doc: dict, period_end: date,
            client: str = "") -> tuple[list[dict], str, float | None]:
    """The checks of a ledger line against its document, the item's status, and the audited
    amount the document supports (None when it can't tell). `client` is the audited entity,
    which is on one side of most documents, so its name doesn't count as the other party."""
    checks: list[dict] = []
    book = abs(float(item["amount"]))
    amounts = [(k, abs(doc[k])) for k in ("total", "net") if doc[k] is not None]
    suggested = None
    if not amounts:
        checks.append({"check": "amount", "ok": None,
                       "text": "No amount could be read from the document."})
    else:
        same = next((k for k, v in amounts if abs(v - book) < CENT), None)
        if same:
            checks.append({"check": "amount", "ok": True,
                           "text": "The amount agrees with the document's total."
                           if same == "total" else
                           "The amount agrees with the document's amount before tax."})
            suggested = book
        else:
            k, v = amounts[0]
            suggested = round(v, 2)
            checks.append({"check": "amount", "ok": False,
                           "text": f"The ledger shows {book:,.2f}; the document's "
                                   f"{'total' if k == 'total' else 'amount'} is {v:,.2f}, "
                                   f"a difference of {book - v:,.2f}."})
    booked = _date(item.get("date"))
    dated = _date(doc["date"])
    if dated is None or booked is None:
        checks.append({"check": "date", "ok": None,
                       "text": "No date could be read from the document."
                       if dated is None else "The ledger line has no date to compare."})
    elif (booked <= period_end) != (dated <= period_end):
        checks.append({"check": "cutoff", "ok": False,
                       "text": f"Cut-off: the entry is dated {booked.isoformat()} and the "
                               f"document {dated.isoformat()}, on the other side of the period "
                               f"end ({period_end.isoformat()})."})
    elif abs((booked - dated).days) > DATE_DAYS:
        checks.append({"check": "date", "ok": False,
                       "text": f"The document is dated {dated.isoformat()}, "
                               f"{abs((booked - dated).days)} days from the entry."})
    else:
        checks.append({"check": "date", "ok": True,
                       "text": f"The document is dated {dated.isoformat()}, in the same period "
                               "as the entry."})
    ledger_text = " ".join(str(item.get(k) or "") for k in ("description", "account", "entry_id"))
    ledger_words = words(ledger_text)
    own = words(client)
    issuer, recipient = words(doc["issuer"]) - own, words(doc["recipient"]) - own
    # The other party: whichever side of the document isn't the client.
    party = (doc["recipient"] if doc["issuer"] and not issuer and recipient
             else doc["issuer"] or doc["recipient"])
    named = (issuer | recipient) & ledger_words
    if named:
        checks.append({"check": "party", "ok": True,
                       "text": f"The entry names the other party ({', '.join(sorted(named))})."})
    elif party:
        checks.append({"check": "party", "ok": None,
                       "text": f"The entry doesn't name {party}; check it is the right party."})
    number = doc["number"].lower()
    if len(number) >= 3 and re.search(rf"(?<![0-9a-z]){re.escape(number)}(?![0-9a-z])",
                                      ledger_text.lower()):
        checks.append({"check": "number", "ok": True,
                       "text": f"The entry gives the document number {doc['number']}."})
    if any(c["ok"] is False for c in checks):
        status = "differs"
    elif any(c["check"] == "amount" and c["ok"] for c in checks) and any(
            c["check"] == "date" and c["ok"] for c in checks):
        status = "agrees"
    else:
        status = "check"
    return checks, status, suggested


def vouching_out(v: Vouching) -> dict:
    return {"id": v.id, "sample_id": v.sample_id, "row": v.row, "filename": v.filename,
            "size": v.size, "sha256": v.sha256, "document": v.document, "checks": v.checks,
            "status": v.status, "status_label": STATUSES[v.status],
            "suggested_amount": v.suggested_amount, "created_by": v.created_by,
            "created_at": v.created_at.isoformat()}


def vouchings_out(x: Sample) -> list[dict]:
    return [vouching_out(v) for v in x.vouchings]


@router.post("/samples/{sample_id}/items/{row}/vouch", status_code=201)
def vouch(  # not async: FastAPI runs it in a thread, so waiting on the AI blocks nothing
    sample_id: int,
    row: int,
    file: UploadFile = File(...),
    language: Literal["en", "ar"] = Form("en"),
    user: User = Depends(require("prepare")),
    session: Session = Depends(get_session),
) -> dict:
    """Read the document behind a sample item and compare it with the ledger line."""
    x = get_or_404(session, Sample, sample_id, user)
    item_of(x, row)
    require_ai()
    content = file.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "The file is larger than 15 MB.")
    block = document_block(content)
    filename = Path(file.filename or "document").name[:500]
    sha = hashlib.sha256(content).hexdigest()
    eid = x.engagement_id
    answer = ask(session, user, eid, "sample.vouch_read",
                 f"Asked the AI assistant to read {filename} for row {row} of sample #{sample_id}",
                 {"sample_id": sample_id, "row": row, "filename": filename, "sha256": sha,
                  "size": len(content)},
                 VOUCH_SYSTEM + LANGUAGE.get(language, ""),
                 [block, {"type": "text", "text": "Read this document."}],
                 max_tokens=2000)
    doc = clean(answer)
    # One change at a time per file, in turn with saving results and deleting the file.
    x = get_or_404(session, Sample, sample_id, user)
    auth.hold(session, f"upload:{x.upload_id}")
    session.expire_all()
    x = get_or_404(session, Sample, sample_id, user)
    item = item_of(x, row)
    checks, status, suggested = compare(item, doc, x.engagement.period_end,
                                        x.engagement.client.name)
    old = session.scalar(select(Vouching).where(Vouching.sample_id == x.id, Vouching.row == row))
    if old is not None:
        session.delete(old)
        session.flush()
    v = Vouching(engagement_id=x.engagement_id, sample_id=x.id, row=row, filename=filename,
                 content_type=block["source"]["media_type"], content=content,
                 size=len(content), sha256=sha, document=doc, checks=checks, status=status,
                 suggested_amount=suggested, created_by=user.name, created_at=utcnow())
    session.add(v)
    session.flush()
    log(session, user, "sample.vouch",
        f"Vouched row {row} of sample #{x.id} to {filename} ({STATUSES[status]})",
        x.engagement, details={"sample_id": x.id, "row": row, "vouching_id": v.id,
                               "filename": filename, "sha256": sha, "status": status,
                               "replaced": old is not None, "model": assistant.model()})
    session.commit()
    doctext.start_backfill()  # reads it for search
    return vouching_out(v)


@router.get("/samples/{sample_id}/vouchings")
def list_vouchings(sample_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> list[dict]:
    return vouchings_out(get_or_404(session, Sample, sample_id, user))


@router.get("/vouchings/{vouching_id}/document")
def download_document(vouching_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> Response:
    v = get_or_404(session, Vouching, vouching_id, user)
    log(session, user, "sample.vouch_download",
        f"Downloaded {v.filename}, vouched to row {v.row} of sample #{v.sample_id}",
        v.sample.engagement, details={"vouching_id": v.id, "sha256": v.sha256})
    session.commit()
    return Response(v.content, media_type=v.content_type, headers=attachment(v.filename))


@router.delete("/vouchings/{vouching_id}", status_code=204)
def delete_vouching(vouching_id: int, user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> Response:
    v = get_or_404(session, Vouching, vouching_id, user)
    auth.hold(session, f"upload:{v.sample.upload_id}")
    session.expire_all()
    v = get_or_404(session, Vouching, vouching_id, user)
    log(session, user, "sample.unvouch",
        f"Removed {v.filename} from row {v.row} of sample #{v.sample_id}", v.sample.engagement,
        details={"vouching_id": v.id, "sample_id": v.sample_id, "row": v.row,
                 "sha256": v.sha256})
    session.delete(v)
    session.commit()
    return Response(status_code=204)


def sheet_rows(x: Sample) -> list[dict]:
    """The vouching of a sample's items, for its workpaper."""
    items = {i["row"]: i for i in x.result["items"]}
    out = []
    for v in x.vouchings:
        i, d = items.get(v.row, {}), v.document
        out.append({
            "Row": v.row, "Entry": i.get("entry_id"), "Date": i.get("date"),
            "Description": i.get("description"), "Amount": i.get("amount"),
            "Document": v.filename, "SHA-256": v.sha256, "Type": d.get("document_type"),
            "Issued by": d.get("issuer"), "Addressed to": d.get("recipient"),
            "Number": d.get("number"), "Document date": d.get("date"),
            "Currency": d.get("currency"), "Net": d.get("net"), "Tax": d.get("tax"),
            "Total": d.get("total"), "Result": STATUSES[v.status],
            "Checks": "\n".join(c["text"] for c in v.checks),
            "Suggested audited amount": v.suggested_amount,
            "Vouched by": v.created_by, "Vouched (UTC)": f"{v.created_at:%Y-%m-%d %H:%M}"})
    return out
