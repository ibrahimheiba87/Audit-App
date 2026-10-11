"""Database models and session handling (SQLAlchemy, SQLite by default).

Set AUDIT_DB_URL to use another database, e.g. postgresql+psycopg://user:pw@host/audit.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    industry: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagements: Mapped[list[Engagement]] = relationship(
        back_populates="client", cascade="all, delete-orphan", order_by="Engagement.period_end"
    )
    portal_users: Mapped[list[ClientUser]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Engagement(Base):
    __tablename__ = "engagements"
    # Never reuse a deleted engagement's id: the activity log keeps it (SQLite only).
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(200))
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date] = mapped_column(Date)
    framework: Mapped[str] = mapped_column(String(50), default="IFRS")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Materiality (see materiality.py). Amounts are stored so they survive a change of method.
    benchmark: Mapped[str | None] = mapped_column(String(50), nullable=True)
    benchmark_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trivial_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    overall_materiality: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_materiality: Mapped[float | None] = mapped_column(Float, nullable=True)
    trivial_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    materiality_rationale: Mapped[str] = mapped_column(Text, default="")

    client: Mapped[Client] = relationship(back_populates="engagements")
    uploads: Mapped[list[Upload]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Upload.id"
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Run.id"
    )
    comparisons: Mapped[list[Comparison]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Comparison.id"
    )
    samples: Mapped[list[Sample]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Sample.id"
    )
    findings: Mapped[list[Finding]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Finding.id"
    )
    account_mappings: Mapped[list[AccountMapping]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    adjusting_entries: Mapped[list[AdjustingEntry]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="AdjustingEntry.id"
    )
    signoffs: Mapped[list[SignOff]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    review_notes: Mapped[list[ReviewNote]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="ReviewNote.id"
    )
    document_requests: Mapped[list[DocumentRequest]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="DocumentRequest.number"
    )


class Upload(Base):
    """A file uploaded to an engagement, kept so runs can be repeated and traced."""

    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    kind: Mapped[str] = mapped_column(String(20))  # "gl", "tb" or "prior_tb"
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    import_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="uploads")


class Run(Base):
    """One execution of the audit checks against an upload, with its settings and result."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    settings: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="runs")
    upload: Mapped[Upload] = relationship()
    register_sync: Mapped[RunSync | None] = relationship(cascade="all, delete-orphan")


class RunSync(Base):
    """Marks a run whose findings have been put on the register (even if none were new).

    A separate table so databases made before the register get it without a migration.
    """

    __tablename__ = "run_register_sync"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)


class Comparison(Base):
    """A prior-year comparison between two uploads, with its thresholds and result."""

    __tablename__ = "comparisons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    current_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    prior_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    settings: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="comparisons")
    current_upload: Mapped[Upload] = relationship(foreign_keys=[current_upload_id])
    prior_upload: Mapped[Upload] = relationship(foreign_keys=[prior_upload_id])


class Sample(Base):
    """An audit sample drawn from an upload, with the seed needed to reproduce it."""

    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    method: Mapped[str] = mapped_column(String(20))
    seed: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="samples")
    upload: Mapped[Upload] = relationship()


class Finding(Base):
    """An item on the engagement's findings register, raised by a check run or added by hand.

    A finding raised by a run is keyed by (upload, check, row, entry), so running the checks
    again on the same file keeps its status, notes and evidence instead of duplicating it.
    """

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("uploads.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # run | manual
    check: Mapped[str] = mapped_column(String(50), default="manual")
    check_name: Mapped[str] = mapped_column(String(200), default="Added by hand")
    severity: Mapped[str] = mapped_column(String(10))
    row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    account: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="findings")
    upload: Mapped[Upload | None] = relationship()
    notes: Mapped[list[FindingNote]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", order_by="FindingNote.id"
    )
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", order_by="Evidence.id"
    )


class FindingNote(Base):
    """A comment on a finding, or a record of a status change (status_from/status_to set)."""

    __tablename__ = "finding_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"))
    text: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    status_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    finding: Mapped[Finding] = relationship(back_populates="notes")


class Evidence(Base):
    """A file attached to a finding (invoice, confirmation, screenshot...)."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"))
    filename: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)  # loaded on download
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(200), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    finding: Mapped[Finding] = relationship(back_populates="evidence")


class AccountMapping(Base):
    """Which financial statement line a trial balance account rolls up into (statements.LINES)."""

    __tablename__ = "account_mappings"
    __table_args__ = (UniqueConstraint("engagement_id", "account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    account: Mapped[str] = mapped_column(String(200))
    line: Mapped[str] = mapped_column(String(50))
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="account_mappings")


class AdjustingEntry(Base):
    """A journal entry the auditor proposes: booked (adjusting, reclassification) or not."""

    __tablename__ = "adjusting_entries"
    __table_args__ = (UniqueConstraint("engagement_id", "kind", "number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # see statements.ENTRY_KINDS
    number: Mapped[int] = mapped_column(Integer)  # per engagement and kind: AJE-1, AJE-2...
    description: Mapped[str] = mapped_column(Text)
    date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="adjusting_entries")
    lines: Mapped[list[AdjustingLine]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="AdjustingLine.id"
    )


class AdjustingLine(Base):
    __tablename__ = "adjusting_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("adjusting_entries.id"), index=True)
    account: Mapped[str] = mapped_column(String(200))
    debit: Mapped[float] = mapped_column(Float, default=0.0)
    credit: Mapped[float] = mapped_column(Float, default=0.0)
    memo: Mapped[str] = mapped_column(String(500), default="")

    entry: Mapped[AdjustingEntry] = relationship(back_populates="lines")


class SignOff(Base):
    """Who prepared or reviewed a section of the engagement file, and when (review.SECTIONS)."""

    __tablename__ = "signoffs"
    __table_args__ = (UniqueConstraint("engagement_id", "section", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    section: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))  # prepared | reviewed
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_name: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="signoffs")


class ReviewNote(Base):
    """A reviewer's question on a section, answered by the preparer and cleared by a reviewer."""

    __tablename__ = "review_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    section: Mapped[str] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | responded | cleared
    author_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cleared_by: Mapped[str] = mapped_column(String(200), default="")
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    engagement: Mapped[Engagement] = relationship(back_populates="review_notes")
    replies: Mapped[list[ReviewReply]] = relationship(
        back_populates="note", cascade="all, delete-orphan", order_by="ReviewReply.id"
    )


class ReviewReply(Base):
    __tablename__ = "review_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("review_notes.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    note: Mapped[ReviewNote] = relationship(back_populates="replies")


class DocumentRequest(Base):
    """A document the auditor asks the client for (portal.py). The client sees all of it."""

    __tablename__ = "document_requests"
    __table_args__ = (UniqueConstraint("engagement_id", "number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)  # per engagement: request 1, 2... never reused
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str] = mapped_column(Text, default="")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # see portal.STATUSES
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="document_requests")
    files: Mapped[list[RequestFile]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="RequestFile.id"
    )
    messages: Mapped[list[RequestMessage]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="RequestMessage.id"
    )


class RequestFile(Base):
    """A file sent in answer to a document request, by the client or by the audit team."""

    __tablename__ = "request_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("document_requests.id"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)  # loaded on download
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str] = mapped_column(String(200), default="")
    uploaded_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_client: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[DocumentRequest] = relationship(back_populates="files")


class RequestMessage(Base):
    """A message on a document request, or a record of a status change (status_to set)."""

    __tablename__ = "request_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("document_requests.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    author_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_client: Mapped[bool] = mapped_column(Boolean, default=False)
    status_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[DocumentRequest] = relationship(back_populates="messages")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # see auth.ROLES
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client_link: Mapped[ClientUser | None] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def client_id(self) -> int | None:
        """The client a client-role user works for, or None."""
        return self.client_link.client_id if self.client_link else None


class ClientUser(Base):
    """Which client a client-role user belongs to: they only see that client's requests.

    A separate table so databases made before the portal get it without a migration.
    """

    __tablename__ = "client_users"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)

    user: Mapped[User] = relationship(back_populates="client_link")
    client: Mapped[Client] = relationship(back_populates="portal_users")


class AuthSession(Base):
    """A signed-in browser. Only a hash of the cookie token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship()


class Activity(Base):
    """Who did what and when. Names are copied in so the log survives deletions."""

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_name: Mapped[str] = mapped_column(String(200), default="")
    user_role: Mapped[str] = mapped_column(String(20), default="")
    action: Mapped[str] = mapped_column(String(50), index=True)
    engagement_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    engagement_label: Mapped[str] = mapped_column(String(400), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


class Guard(Base):
    """A row per invariant that requests must check and change one at a time.

    Writing to a guard row inside a transaction holds a database write lock on it until the
    commit, so concurrent requests that guard the same key run one after another.
    See auth.hold().
    """

    __tablename__ = "guards"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, default=0)


SUPPORTED_DIALECTS = ("sqlite", "postgresql")


def _make_engine(url: str):
    # auth.hold() needs INSERT ... ON CONFLICT, so only databases that have it are allowed.
    dialect = make_url(url).get_backend_name()
    if dialect not in SUPPORTED_DIALECTS:
        raise RuntimeError(f"AUDIT_DB_URL uses {dialect}; the app supports SQLite and "
                           "PostgreSQL only.")
    kwargs = {"connect_args": {"check_same_thread": False}} if dialect == "sqlite" else {}
    return create_engine(url, **kwargs)


engine = _make_engine(os.environ.get("AUDIT_DB_URL", "sqlite:///./audit.db"))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def configure(url: str) -> None:
    """Point the app at a different database (used by tests)."""
    global engine
    engine = _make_engine(url)
    SessionLocal.configure(bind=engine)
    init()


def init() -> None:
    Base.metadata.create_all(engine)
    _upgrade()


def _upgrade() -> None:
    """Changes create_all can't make to tables an earlier version created."""
    if engine.dialect.name == "postgresql":  # SQLite doesn't enforce VARCHAR lengths
        with engine.begin() as conn:
            for table in ("account_mappings", "adjusting_lines"):  # account was VARCHAR(100)
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN account TYPE VARCHAR(200)"))


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
