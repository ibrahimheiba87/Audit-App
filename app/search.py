"""Search: one box in the header that finds engagements (by their own or their client's
name), findings, tasks, client requests, risks, controls, related parties, review notes,
files and the words inside attached documents (doctext.py), across the engagements the user
may see. Each hit opens its engagement at the right panel. Searches aren't logged, as they
only read.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import auth, doctext
from .auth import require
from .db import (
    Client,
    Control,
    DocumentRequest,
    Engagement,
    Finding,
    RelatedParty,
    ReviewNote,
    Risk,
    Task,
    Upload,
    User,
    get_session,
)

router = APIRouter(prefix="/api")
PER_KIND = 6
SNIPPET = 90


# (kind, label, model, the columns searched, the title, the panel a hit opens)
KINDS = [
    ("engagement", "Engagements", Engagement, [Engagement.name, Client.name], None, None),
    ("finding", "Findings", Finding,
     [Finding.description, Finding.detail, Finding.account, Finding.entry_id],
     lambda f: f.description or f.entry_id or f.account or f.detail, "reg-panel"),
    ("task", "Tasks", Task, [Task.title, Task.note], lambda t: t.title, "wp-panel"),
    ("request", "Client requests", DocumentRequest,
     [DocumentRequest.title, DocumentRequest.details], lambda r: r.title, "rq-panel"),
    ("risk", "Risks", Risk, [Risk.title, Risk.description, Risk.response],
     lambda r: r.title, "rr-panel"),
    ("control", "Controls", Control, [Control.title, Control.description, Control.owner],
     lambda c: c.title, "ct-panel"),
    ("party", "Related parties", RelatedParty, [RelatedParty.name, RelatedParty.note],
     lambda p: p.name, "rpt-panel"),
    ("note", "Review notes", ReviewNote, [ReviewNote.text], lambda n: n.text, "rv-panel"),
    ("file", "Files", Upload, [Upload.filename], lambda u: u.filename, "files-panel"),
]


def snippet(texts: list[str | None], q: str, title: str) -> str:
    """The text around the first match that isn't the title itself."""
    low = q.lower()
    for t in texts:
        if not t or t == title:
            continue
        at = t.lower().find(low)
        if at < 0:
            continue
        start = max(at - SNIPPET // 3, 0)
        piece = t[start:start + SNIPPET].strip()
        return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(t) else "")
    return ""


def search(session: Session, user: User, q: str) -> list[dict]:
    seen = None if auth.sees_all_engagements(user) else auth.assigned_engagement_ids(user)
    groups = []
    for kind, label, model, cols, title_of, panel in KINDS:
        match = or_(*[c.icontains(q, autoescape=True) for c in cols])
        if model is Engagement:
            stmt = select(Engagement).join(Client).where(match)
            if seen is not None:
                stmt = stmt.where(Engagement.id.in_(seen))
            stmt = stmt.order_by(Engagement.period_end.desc(), Engagement.id.desc())
        else:
            stmt = select(model).join(Engagement, model.engagement_id == Engagement.id).where(match)
            if seen is not None:
                stmt = stmt.where(model.engagement_id.in_(seen))
            stmt = stmt.order_by(model.id.desc())
        rows = list(session.scalars(stmt.limit(PER_KIND + 1)))
        if not rows:
            continue
        items = []
        for r in rows[:PER_KIND]:
            e = r if model is Engagement else r.engagement
            title = f"{e.client.name} · {e.name}" if model is Engagement else (title_of(r) or "")
            texts = [] if model is Engagement else [getattr(r, c.key) for c in cols]
            item = {"kind": kind, "id": r.id, "engagement_id": e.id, "title": title[:200],
                    "snippet": snippet(texts, q, title), "panel": panel,
                    "engagement": f"{e.client.name} · {e.name}",
                    "period_end": e.period_end.isoformat(),
                    "archived": bool(e.lock and e.lock.archived)}
            if model is Finding:
                item |= {"check_name": r.check_name, "severity": r.severity, "status": r.status}
            items.append(item)
        groups.append({"kind": kind, "label": label, "items": items,
                       "more": len(rows) > PER_KIND})
    inside = doctext.search(session, seen, q, PER_KIND, SNIPPET)
    if inside:
        groups.append(inside)
    return groups


@router.get("/search")
def find(q: str = Query(min_length=2, max_length=100), user: User = Depends(require("prepare")),
         session: Session = Depends(get_session)) -> dict:
    q = q.strip()
    groups = search(session, user, q) if len(q) >= 2 else []
    return {"q": q, "groups": groups, "total": sum(len(g["items"]) for g in groups)}
