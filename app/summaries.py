"""AI summaries of contracts and board minutes.

The auditor uploads a contract, the minutes of a board or shareholders' meeting, or another
document as a PDF or photo, and the AI lists what matters for the audit: the parties, key
terms, amounts and dates, decisions, and points to follow up (commitments, litigation, related
parties, going concern, events after the period end). Like the other AI tools, this is off
without ANTHROPIC_API_KEY, the answer is checked and trimmed before it is kept, and every
request is logged. The file itself isn't kept. Names are matched against the related parties
register, and dates after the period end are flagged for the subsequent events review.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy.orm import Session

from . import assistant, auth, related_parties
from .ai_tools import (
    DATA_ONLY,
    DOCUMENT_TIMEOUT,
    MAX_DOCUMENT_BYTES,
    _date,
    _list,
    _number,
    _text,
    ask,
    document_block,
    require_ai,
)
from .auth import current_user, log, require
from .db import DocumentSummary, Engagement, User, get_session, utcnow
from .engagements import get_or_404, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KINDS = {"contract": "Contract or agreement", "minutes": "Minutes of a meeting",
         "other": "Other document"}
AREAS = {
    "related_parties": "Related parties", "commitments": "Commitments",
    "contingencies": "Litigation and contingencies", "going_concern": "Going concern",
    "subsequent_events": "Events after the period end", "revenue": "Revenue",
    "borrowing": "Borrowing and security", "compliance": "Fraud or non-compliance",
    "other": "Other",
}
MAX_ITEMS = 30
MAX_SUMMARIES = 200
SUMMARY_TOKENS = 8000
LANGUAGE = {"en": "", "ar": " Write every text value in Modern Standard Arabic, keeping names, "
                         "figures and dates as they are in the document."}

SYSTEM = (
    "You help a financial statement auditor read a client's contracts and the minutes of its "
    "board and shareholder meetings. " + DATA_ONLY +
    "Say only what the document says; never guess a name, amount or date. Answer with JSON "
    "only, no other text, in exactly this shape: "
    '{"title": "<what the document is>", "document_date": "YYYY-MM-DD or null", '
    '"parties": ["<people and organisations named as parties or attendees>"], '
    '"summary": "<two to four sentences>", '
    '"key_terms": [{"term": "<a few words>", "detail": "<one sentence>"}], '
    '"amounts": [{"what": "...", "amount": <number or null>, "currency": "..."}], '
    '"dates": [{"date": "YYYY-MM-DD", "what": "..."}], '
    '"decisions": ["<each decision or resolution, for minutes>"], '
    '"audit_points": [{"area": "<area id>", "point": "<what the auditor should look at, '
    'one sentence>"}]}. '
    f"Area ids: {', '.join(AREAS)}. At most {MAX_ITEMS} items in each list. Numbers use a dot "
    "for decimals and no thousands separators.")
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class SummaryPatch(BaseModel):
    note: Note | None = None
    reviewed: bool | None = None


def _str(v, limit: int = 500) -> str:
    """Text the AI gave; anything that isn't text or a number counts as nothing."""
    return _text(v, limit) if isinstance(v, (str, int, float)) else ""


def clean(answer: dict) -> dict:
    """What the AI said, trimmed to the expected shape; ValueError if there's nothing."""
    def items(key: str) -> list:
        return [x for x in _list(answer.get(key)) if x][:MAX_ITEMS]

    out = {
        "title": _str(answer.get("title"), 300),
        "document_date": _date(answer.get("document_date")),
        "parties": [t for t in (_str(x, 300) for x in items("parties")) if t],
        "summary": _str(answer.get("summary"), 2000),
        "key_terms": [{"term": _str(x.get("term"), 200), "detail": _str(x.get("detail"))}
                      for x in items("key_terms") if isinstance(x, dict)],
        "amounts": [{"what": _str(x.get("what"), 300), "amount": _number(x.get("amount")),
                     "currency": _str(x.get("currency"), 20)}
                    for x in items("amounts") if isinstance(x, dict)],
        "dates": [{"date": d, "what": _str(x.get("what"), 300)}
                  for x in items("dates") if isinstance(x, dict)
                  for d in [_date(x.get("date"))] if d],
        "decisions": [t for t in (_str(x) for x in items("decisions")) if t],
        "audit_points": [{"area": x.get("area") if isinstance(x.get("area"), str)
                          and x.get("area") in AREAS else "other",
                          "point": _str(x.get("point"))}
                         for x in items("audit_points")
                         if isinstance(x, dict) and _str(x.get("point"))],
    }
    if not (out["summary"] or out["title"]):
        raise ValueError("The AI couldn't read the document. Check it is a clear PDF or photo.")
    out["document_date"] = out["document_date"].isoformat() if out["document_date"] else None
    for d in out["dates"]:
        d["date"] = d["date"].isoformat()
    return out


def summary_out(s: DocumentSummary, e: Engagement, known: dict[str, str]) -> dict:
    c = s.content or {}
    end = e.period_end.isoformat()
    return {
        "id": s.id, "kind": s.kind, "kind_label": KINDS.get(s.kind, s.kind),
        "filename": s.filename, "size": s.size, "language": s.language, "model": s.model,
        "content": c, "note": s.note, "reviewed_by": s.reviewed_by,
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "created_by": s.created_by, "created_by_id": s.created_by_id,
        "created_at": s.created_at.isoformat(),
        # Names in the register, and names that may be related parties not yet in it.
        "related": [{"name": n, "party": related_parties.find_party(known, n)}
                    for n in c.get("parties", [])],
        "after_period_end": [d for d in c.get("dates", []) if d["date"] > end],
    }


def state(e: Engagement) -> dict:
    known = related_parties.known_names(e)
    items = [summary_out(s, e, known) for s in reversed(e.document_summaries)]
    return {"summaries": items, "enabled": assistant.enabled(),
            "kinds": [{"id": k, "label": v} for k, v in KINDS.items()],
            "areas": AREAS,
            "to_review": sum(1 for s in e.document_summaries if not s.reviewed_at)}


@router.get("/engagements/{engagement_id}/document-summaries")
def list_summaries(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    return state(get_or_404(session, Engagement, engagement_id, user))


@router.post("/engagements/{engagement_id}/document-summaries", status_code=201)
def summarise(  # not async: FastAPI runs it in a thread, so waiting on the AI blocks nothing
    engagement_id: int,
    file: UploadFile = File(...),
    kind: Literal["contract", "minutes", "other"] = Form("contract"),
    language: Literal["en", "ar"] = Form("en"),
    user: User = Depends(require("prepare")),
    session: Session = Depends(get_session),
) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    require_ai()
    if len(e.document_summaries) >= MAX_SUMMARIES:
        raise HTTPException(400, f"An engagement can have at most {MAX_SUMMARIES} summaries.")
    content = file.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "The file is larger than 15 MB. Split it into smaller files.")
    block = document_block(content)
    filename = Path(file.filename or "document").name[:255]
    sha = hashlib.sha256(content).hexdigest()
    label = KINDS[kind].lower()
    answer = ask(session, user, e.id, "summary.create",
                 f"Asked the AI assistant to summarise the {label} {filename}",
                 {"filename": filename, "sha256": sha, "size": len(content), "kind": kind,
                  "language": language},
                 SYSTEM + LANGUAGE[language],
                 [block, {"type": "text", "text": f"Summarise this {label} for the audit."}],
                 check=clean, max_tokens=SUMMARY_TOKENS, timeout=DOCUMENT_TIMEOUT)
    e = get_or_404(session, Engagement, engagement_id, user)
    s = DocumentSummary(engagement=e, kind=kind, filename=filename, sha256=sha,
                        size=len(content), language=language, content=answer,
                        model=assistant.model(), created_by=user.name, created_by_id=user.id)
    session.add(s)
    session.commit()
    return state(e)


def held(session: Session, user: User, summary_id: int) -> DocumentSummary:
    engagement_id = get_or_404(session, DocumentSummary, summary_id, user).engagement_id
    auth.hold(session, f"summaries:{engagement_id}")
    session.expire_all()
    return get_or_404(session, DocumentSummary, summary_id, user)


@router.patch("/document-summaries/{summary_id}")
def update_summary(summary_id: int, body: SummaryPatch, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    s = held(session, user, summary_id)
    if body.note is not None:
        s.note = body.note
    if body.reviewed is True:
        s.reviewed_by, s.reviewed_at = user.name, utcnow()
    elif body.reviewed is False:
        s.reviewed_by, s.reviewed_at = "", None
    log(session, user, "summary.update", f"Updated the AI summary of {s.filename}",
        s.engagement, details={"summary_id": s.id, "reviewed": bool(s.reviewed_at),
                               "note": body.note is not None})
    session.commit()
    return state(s.engagement)


@router.delete("/document-summaries/{summary_id}")
def delete_summary(summary_id: int, user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    s = held(session, user, summary_id)
    if s.created_by_id != user.id and not auth.can(user, "review"):
        raise HTTPException(403, "Only whoever asked for the summary or a reviewer can remove "
                                 "it.")
    e = s.engagement
    log(session, user, "summary.delete", f"Removed the AI summary of {s.filename}", e,
        details={"summary_id": s.id, "sha256": s.sha256})
    session.delete(s)
    session.commit()
    session.refresh(e)
    return state(e)


# --- Excel -------------------------------------------------------------------------

def to_excel(e: Engagement) -> bytes:
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()),
             ("Documents summarised", len(e.document_summaries)),
             ("Note", "Written by the AI assistant from each document; check against the "
                      "document itself.")]
    docs, points, terms = [], [], []
    for n, s in enumerate(e.document_summaries, 1):
        c = s.content or {}
        ref = f"D{n}"
        docs.append({"Ref": ref, "Type": KINDS.get(s.kind, s.kind), "File": s.filename,
                     "Title": c.get("title", ""), "Dated": c.get("document_date") or "",
                     "Parties": ", ".join(c.get("parties", [])),
                     "Summary": c.get("summary", ""),
                     "Decisions": "\n".join(c.get("decisions", [])),
                     "Auditor's note": s.note, "Reviewed by": s.reviewed_by,
                     "Summarised by": s.created_by, "Model": s.model, "SHA-256": s.sha256})
        points += [{"Ref": ref, "Area": AREAS.get(p["area"], p["area"]), "Point": p["point"]}
                   for p in c.get("audit_points", [])]
        terms += [{"Ref": ref, "Term": t["term"], "Detail": t["detail"]}
                  for t in c.get("key_terms", [])]
        terms += [{"Ref": ref, "Term": a["what"],
                   "Detail": f"{a['amount'] if a['amount'] is not None else ''} "
                             f"{a['currency']}".strip()} for a in c.get("amounts", [])]
        terms += [{"Ref": ref, "Term": d["what"], "Detail": d["date"]}
                  for d in c.get("dates", [])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(docs, columns=["Ref", "Type", "File", "Title", "Dated", "Parties",
                                    "Summary", "Decisions", "Auditor's note", "Reviewed by",
                                    "Summarised by", "Model", "SHA-256"]).to_excel(
            xl, sheet_name="Documents", index=False)
        pd.DataFrame(points, columns=["Ref", "Area", "Point"]).to_excel(
            xl, sheet_name="Audit points", index=False)
        pd.DataFrame(terms, columns=["Ref", "Term", "Detail"]).to_excel(
            xl, sheet_name="Terms, amounts and dates", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def workbook(e: Engagement) -> tuple[str, bytes]:
    stem = safe_stem(f"{e.client.name}-{e.name}-document-summaries")
    return f"{stem}.xlsx", to_excel(e)


@router.get("/engagements/{engagement_id}/document-summaries/export")
def export_summaries(engagement_id: int, user: User = Depends(current_user),
                     session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(e)
    log(session, user, "summary.download", "Downloaded the document summaries", e,
        details={"summaries": len(e.document_summaries)})
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))


def summary(e: Engagement) -> dict:
    return {"documents": len(e.document_summaries),
            "to_review": sum(1 for s in e.document_summaries if not s.reviewed_at),
            "after_period_end": sum(1 for s in e.document_summaries
                                    for d in (s.content or {}).get("dates", [])
                                    if d["date"] > e.period_end.isoformat())}
