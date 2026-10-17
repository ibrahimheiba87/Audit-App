"""Search inside documents: the words in PDFs, Word and Excel files and plain text attached to
an engagement, as DataSnipper and Caseware's document search find them. Finding evidence,
files sent for client requests and vouched documents are read in the background just after
they arrive, and files attached before this existed are read once when the app starts.

A file is read in a separate process with limits on its time and memory, outside any
database transaction, so a damaged or hostile file (a PDF that takes forever, a zip that
unpacks to gigabytes) is marked unreadable instead of holding up the app. One reader runs at a
time in each process; on PostgreSQL an advisory lock makes it one for the whole server.

Pictures and scanned PDFs have no text to read (there's no OCR), so they're found by their
name only. Text is stored in Unicode's compatibility form, without Arabic diacritics or
tatweel, so letters a PDF keeps in their joined shapes match the same letters typed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

from sqlalchemy import and_, delete, exists, or_, select, text
from sqlalchemy.orm import Session, selectinload, undefer

from . import db
from .db import DocumentText, Engagement, Evidence, RequestFile, Vouching
from .textread import TEXT_TYPES, normal

log = logging.getLogger(__name__)

CPU_SECONDS = 30
MEMORY = 1024 * 1024 * 1024
WALL_SECONDS = 60
BATCH = 20
MIN_QUERY = 3
LOCK_ID = 7_140_917  # the reader's PostgreSQL advisory lock
STATUSES = {"ok": "Read", "empty": "No text (a picture or a scan)",
            "unsupported": "Not a kind of file that can be read", "unreadable": "Couldn't be read"}
READER = Path(__file__).with_name("textread.py")


def _limits() -> None:  # in the reading process, before it starts
    import resource  # noqa: PLC0415 - Unix only

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))


def extract_safely(filename: str, content: bytes) -> tuple[str, str, int]:
    """extract() in a separate process limited in time and memory."""
    if not content.startswith((b"%PDF", b"PK")) and not filename.lower().endswith(TEXT_TYPES):
        return "unsupported", "", 0
    try:
        done = subprocess.run(
            [sys.executable, "-I", str(READER), filename], input=content,
            capture_output=True, timeout=WALL_SECONDS, check=True,
            preexec_fn=_limits if sys.platform != "win32" else None)  # noqa: PLW1509
        out = json.loads(done.stdout)
        return out["status"], out["text"], out["pages"]
    except (subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
        log.info("Couldn't read %s: %s", filename, exc)
        return "unreadable", "", 0


# Where attached files are kept: DocumentText.source names one of these tables.
SOURCES = {
    "evidence": Evidence,
    "request_file": RequestFile,
    "vouching": Vouching,
}
LABELS = {"evidence": "Finding evidence", "request_file": "Client request",
          "vouching": "Vouched document"}
PANELS = {"evidence": "reg-panel", "request_file": "rq-panel", "vouching": "smp-panel"}


def engagement_of(source: str, row) -> int | None:
    if source == "evidence":
        return row.finding.engagement_id if row.finding else None
    if source == "request_file":
        return row.request.engagement_id if row.request else None
    return row.engagement_id


def current(source: str):
    """Whether a stored text still belongs to a file that's there: same id and fingerprint
    (SQLite can give a deleted file's id to a new one)."""
    model = SOURCES[source]
    return and_(DocumentText.source == source,
                exists(select(model.id).where(model.id == DocumentText.source_id,
                                              model.sha256 == DocumentText.sha256)))


def still_there():
    return or_(*[current(s) for s in SOURCES])


def unread(session: Session, source: str, after: int, limit: int) -> list[int]:
    model = SOURCES[source]
    stmt = (select(model.id).outerjoin(DocumentText, and_(
        DocumentText.source == source, DocumentText.source_id == model.id,
        DocumentText.sha256 == model.sha256)).where(DocumentText.id.is_(None), model.id > after)
        .order_by(model.id).limit(limit))
    return list(session.scalars(stmt))


def read_one(source: str, file_id: int) -> None:
    """Read one file and keep its words. The file is loaded and the text saved in short
    transactions of their own; nothing is held open while it's read."""
    model = SOURCES[source]
    with db.SessionLocal() as session:
        row = session.get(model, file_id)
        if row is None:
            return
        eid, filename, sha, content = engagement_of(source, row), row.filename, row.sha256, \
            row.content
    if eid is None:
        return
    status, value, pages = extract_safely(filename, content)
    del content
    with db.SessionLocal() as session:
        # The file may have gone, or been replaced under the same id, while it was read.
        if session.scalar(select(model.id).where(model.id == file_id,
                                                 model.sha256 == sha)) is None:
            return
        session.execute(delete(DocumentText).where(DocumentText.source == source,
                                                   DocumentText.source_id == file_id))
        session.add(DocumentText(engagement_id=eid, source=source, source_id=file_id,
                                 filename=filename, sha256=sha, status=status, pages=pages,
                                 text=value))
        session.commit()


_running = threading.Lock()
_again = threading.Event()


def _server_lock(session: Session) -> bool:
    if session.bind.dialect.name != "postgresql":
        return True
    return bool(session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_ID}))


def backfill(limit: int | None = None, tidy: bool = True) -> int:
    """Read files that have no stored text yet and, when tidying, forget texts whose file is
    gone. Returns how many files were read. Only one reader runs at a time."""
    if not _running.acquire(blocking=False):
        _again.set()  # the running reader goes round again for files that just arrived
        return 0
    done = 0
    try:
        with db.SessionLocal() as guard:
            if not _server_lock(guard):
                return 0
            try:
                if tidy:
                    guard.execute(delete(DocumentText).where(~still_there())
                                  .execution_options(synchronize_session=False))
                    guard.commit()
                while True:
                    _again.clear()
                    for source in SOURCES:
                        after = 0
                        while limit is None or done < limit:
                            with db.SessionLocal() as session:
                                ids = unread(session, source, after, BATCH)
                            if not ids:
                                break
                            for i in ids:
                                after = i
                                try:
                                    read_one(source, i)
                                except Exception:  # one file mustn't stop the others
                                    log.exception("Couldn't keep the text of %s %s", source, i)
                                done += 1
                                if limit is not None and done >= limit:
                                    break
                    if not _again.is_set() or (limit is not None and done >= limit):
                        break
            finally:
                if guard.bind.dialect.name == "postgresql":
                    guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
                    guard.commit()
    finally:
        _running.release()
    return done


IN_BACKGROUND = True  # tests read files straight away


def start_backfill(tidy: bool = False) -> None:
    """Read waiting files in a background thread; tidy when the app starts."""
    def run() -> None:
        try:
            n = backfill(tidy=tidy)
            if n:
                log.info("Read the text of %d files for search", n)
        except Exception:
            log.exception("Reading files for search stopped")

    if not IN_BACKGROUND:
        run()
        return
    threading.Thread(target=run, name="doctext-reader", daemon=True).start()


def snippet(value: str, q: str, length: int) -> tuple[str, int | None]:
    """The text around the first match, and where it is."""
    hit = re.search(re.escape(q), value, re.IGNORECASE)
    if not hit:
        return "", None
    at = hit.start()
    start = max(at - 30, 0)
    piece = value[start:start + length].replace("\f", " ").replace("\n", " ").strip()
    return ("…" if start else "") + piece + ("…" if start + length < len(value) else ""), at


def search(session: Session, seen: list[int] | None, q: str, per_kind: int,
           length: int) -> dict | None:
    """The search box's group of files whose words (or name) match."""
    q = normal(q)
    if len(q) < MIN_QUERY:
        return None
    match = or_(DocumentText.text.icontains(q, autoescape=True),
                DocumentText.filename.icontains(q, autoescape=True))
    stmt = (select(DocumentText).join(Engagement).where(match, still_there())
            .options(undefer(DocumentText.text),
                     selectinload(DocumentText.engagement).selectinload(Engagement.client),
                     selectinload(DocumentText.engagement).selectinload(Engagement.lock))
            .order_by(DocumentText.id.desc()).limit(per_kind + 1))
    if seen is not None:
        stmt = stmt.where(DocumentText.engagement_id.in_(seen))
    rows = list(session.scalars(stmt))
    if not rows:
        return None
    evidence = [d.source_id for d in rows[:per_kind] if d.source == "evidence"]
    findings = dict(session.execute(select(Evidence.id, Evidence.finding_id)
                                    .where(Evidence.id.in_(evidence))).all()) if evidence else {}
    items = []
    for d in rows[:per_kind]:
        e = d.engagement
        value = d.text or ""
        piece, at = snippet(value, q, length)
        item = {"kind": "document", "id": d.id, "engagement_id": e.id, "title": d.filename,
                "snippet": piece,
                "page": value.count("\f", 0, at) + 1 if at is not None and d.pages > 1 else None,
                "source": d.source, "source_label": LABELS[d.source], "panel": PANELS[d.source],
                "engagement": f"{e.client.name} · {e.name}",
                "period_end": e.period_end.isoformat(),
                "archived": bool(e.lock and e.lock.archived)}
        if d.source == "evidence":
            item["finding_id"] = findings.get(d.source_id)
        items.append(item)
    return {"kind": "document", "label": "Inside documents", "items": items,
            "more": len(rows) > per_kind}

