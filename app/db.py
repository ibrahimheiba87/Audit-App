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
    exists,
    select,
    text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
    undefer,
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
    team: Mapped[list[EngagementMember]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    risks: Mapped[list[Risk]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Risk.id"
    )
    bank_reconciliations: Mapped[list[BankReconciliation]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="BankReconciliation.id"
    )
    ai_drafts: Mapped[list[AiDraft]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="AiDraft.id"
    )
    lock: Mapped[EngagementLock | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    acceptance: Mapped[AcceptanceChecklist | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    declarations: Mapped[list[IndependenceDeclaration]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="IndependenceDeclaration.id"
    )
    cross_matches: Mapped[list[CrossMatch]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="CrossMatch.id"
    )
    disclosures: Mapped[DisclosureChecklist | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    confirmations: Mapped[list[Confirmation]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Confirmation.id"
    )
    controls: Mapped[list[Control]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Control.id"
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Task.id"
    )
    time_budgets: Mapped[list[TimeBudget]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="TimeBudget.area"
    )
    time_entries: Mapped[list[TimeEntry]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="TimeEntry.id"
    )
    completion: Mapped[CompletionChecklist | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    subsequent_events: Mapped[list[SubsequentEvent]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="SubsequentEvent.id"
    )
    audit_report: Mapped[AuditReport | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    related_parties: Mapped[list[RelatedParty]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="RelatedParty.id"
    )
    document_summaries: Mapped[list[DocumentSummary]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="DocumentSummary.id"
    )


class Upload(Base):
    """A file uploaded to an engagement, kept so runs can be repeated and traced."""

    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    kind: Mapped[str] = mapped_column(String(20))  # engagements.UPLOAD_KINDS
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)  # loaded when read
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
    result: Mapped[dict] = mapped_column(JSON, deferred=True)  # every finding; loaded when read
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="runs")
    upload: Mapped[Upload] = relationship()
    register_sync: Mapped[RunSync | None] = relationship(cascade="all, delete-orphan")
    summary: Mapped[RunSummary | None] = relationship(cascade="all, delete-orphan",
                                                      lazy="selectin")


class RunSummary(Base):
    """A run's counts, kept apart from its result so listing runs doesn't read every finding.

    A separate table so databases made before it get it without a migration; init() fills it
    in for runs saved before it existed.
    """

    __tablename__ = "run_summaries"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    rows: Mapped[int] = mapped_column(Integer)
    findings_by_severity: Mapped[dict] = mapped_column(JSON)

    @staticmethod
    def of(result: dict) -> dict:
        s = result["summary"]
        return {"rows": s["rows"], "findings_by_severity": s["findings_by_severity"]}


class TestTemplate(Base):
    """Firm-wide settings for the journal entry checks, such as the words to look for, that a
    team picks instead of typing them in each time (templates.py)."""

    __tablename__ = "test_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    settings: Mapped[dict] = mapped_column(JSON)  # cutoff_days, round_threshold, weekend, keywords
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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


class CrossMatch(Base):
    """Two files matched on chosen columns, such as vendors against employees on bank
    account and address, with the matching rows (crossmatch.py)."""

    __tablename__ = "cross_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    left_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    right_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    title: Mapped[str] = mapped_column(String(200), default="")
    settings: Mapped[dict] = mapped_column(JSON)
    totals: Mapped[dict] = mapped_column(JSON)  # the counts, kept apart from the rows
    result: Mapped[dict] = mapped_column(JSON, deferred=True)  # loaded when viewed
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="cross_matches")
    left_upload: Mapped[Upload] = relationship(foreign_keys=[left_upload_id])
    right_upload: Mapped[Upload] = relationship(foreign_keys=[right_upload_id])


class BankReconciliation(Base):
    """A bank statement matched to the cash account in a general ledger (bankrec.py)."""

    __tablename__ = "bank_reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    bank_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    gl_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    settings: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="bank_reconciliations")
    bank_upload: Mapped[Upload] = relationship(foreign_keys=[bank_upload_id])
    gl_upload: Mapped[Upload] = relationship(foreign_keys=[gl_upload_id])


class AiDraft(Base):
    """A risk assessment or management letter the AI drafted for the auditor (ai_writer.py)."""

    __tablename__ = "ai_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # risk | letter
    language: Mapped[str] = mapped_column(String(5), default="en")
    content: Mapped[dict] = mapped_column(JSON)
    sent: Mapped[dict] = mapped_column(JSON, default=dict)  # what the AI was given, in counts
    model: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="ai_drafts")


class EngagementLock(Base):
    """A locked engagement: its file is final and read-only (locking.py). Archived ones are
    also left out of the engagement list unless asked for."""

    __tablename__ = "engagement_locks"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    locked_by: Mapped[str] = mapped_column(String(200), default="")
    locked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    note: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_by: Mapped[str] = mapped_column(String(200), default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    engagement: Mapped[Engagement] = relationship(back_populates="lock")


class AcceptanceChecklist(Base):
    """The acceptance and continuance checklist for an engagement, and the partner's decision
    (acceptance.py)."""

    __tablename__ = "acceptance_checklists"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # {question: {answer, comment}}
    conclusion: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)  # accept | decline
    decision_note: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(200), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    engagement: Mapped[Engagement] = relationship(back_populates="acceptance")


class DisclosureChecklist(Base):
    """The disclosure checklist for an engagement's financial statements, with the AI's
    reading of the draft statements kept beside the team's answers (disclosures.py)."""

    __tablename__ = "disclosure_checklists"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # {item: {status, reference, comment}}
    ai: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # the latest AI review
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="disclosures")


class IndependenceDeclaration(Base):
    """A team member's confirmation of their independence from the client, or the threats
    they declare and the safeguards against them. One per person, replaced when renewed."""

    __tablename__ = "independence_declarations"
    __table_args__ = (UniqueConstraint("engagement_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    user_name: Mapped[str] = mapped_column(String(200), default="")
    independent: Mapped[bool] = mapped_column(Boolean)
    threats: Mapped[str] = mapped_column(Text, default="")
    safeguards: Mapped[str] = mapped_column(Text, default="")
    declared_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="declarations")


class Task(Base):
    """A piece of work on an engagement, who it is for and when it is due (workplan.py)."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    area: Mapped[str] = mapped_column(String(30), default="other")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True,
                                                    index=True)
    assignee_name: Mapped[str] = mapped_column(String(200), default="")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    done_by: Mapped[str] = mapped_column(String(200), default="")
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    engagement: Mapped[Engagement] = relationship(back_populates="tasks")


class TimeBudget(Base):
    """The hours budgeted for one area of an engagement (workplan.py)."""

    __tablename__ = "time_budgets"
    __table_args__ = (UniqueConstraint("engagement_id", "area"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    area: Mapped[str] = mapped_column(String(30))
    hours: Mapped[float] = mapped_column(Float, default=0.0)

    engagement: Mapped[Engagement] = relationship(back_populates="time_budgets")


class TimeEntry(Base):
    """Hours someone worked on an engagement on one day (workplan.py)."""

    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True,
                                                index=True)
    user_name: Mapped[str] = mapped_column(String(200), default="")
    worked_on: Mapped[date] = mapped_column(Date)
    area: Mapped[str] = mapped_column(String(30), default="other")
    hours: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # not a key: tasks go
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="time_entries")
class CompletionChecklist(Base):
    """The completion procedures for an engagement: subsequent events, going concern, written
    representations and the final review, with the partner's sign-off (completion.py)."""

    __tablename__ = "completion_checklists"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # {question: {answer, comment}}
    going_concern: Mapped[str | None] = mapped_column(String(30), nullable=True)
    going_concern_note: Mapped[str] = mapped_column(Text, default="")
    reps_requested_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reps_dated: Mapped[date | None] = mapped_column(Date, nullable=True)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    signed_by: Mapped[str] = mapped_column(String(200), default="")
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_note: Mapped[str] = mapped_column(Text, default="")

    engagement: Mapped[Engagement] = relationship(back_populates="completion")


class SubsequentEvent(Base):
    """An event after the period end found during the completion work (ISA 560)."""

    __tablename__ = "subsequent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="non_adjusting")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    treatment: Mapped[str] = mapped_column(String(20), default="pending")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="subsequent_events")


class AuditReport(Base):
    """The draft independent auditor's report for an engagement (report_draft.py)."""

    __tablename__ = "audit_reports"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    opinion: Mapped[str] = mapped_column(String(20), default="unmodified")
    basis: Mapped[str] = mapped_column(Text, default="")  # why the opinion is modified
    entity_name: Mapped[str] = mapped_column(String(300), default="")
    addressee: Mapped[str] = mapped_column(String(300), default="")
    framework_name: Mapped[str] = mapped_column(String(300), default="")
    emphasis: Mapped[list] = mapped_column(JSON, default=list)  # [text]
    key_matters: Mapped[list] = mapped_column(JSON, default=list)  # [{title, why, how}]
    other_matter: Mapped[str] = mapped_column(Text, default="")
    firm_name: Mapped[str] = mapped_column(String(300), default="")
    partner_name: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(200), default="")
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="audit_report")


class RelatedParty(Base):
    """A related party of the client (ISA 550): a shareholder, director, family member or
    group company, with other names it may appear under in the ledger (related_parties.py)."""

    __tablename__ = "related_parties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    relationship_kind: Mapped[str] = mapped_column(String(30), default="other")
    aliases: Mapped[list] = mapped_column(JSON, default=list)  # other names to look for
    note: Mapped[str] = mapped_column(Text, default="")
    # From the last ledger scan, so the dashboard needn't read the ledger.
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    matches: Mapped[int] = mapped_column(Integer, default=0)
    matched_amount: Mapped[float] = mapped_column(Float, default=0.0)  # debits plus credits
    # Which ledger was scanned, and a fingerprint of the lines found, so a later scan that
    # finds different lines (even as many) needs a fresh review. Added after the table.
    scan_upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    conclusion: Mapped[str] = mapped_column(Text, default="")  # on the transactions found
    reviewed_by: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disclosed: Mapped[str] = mapped_column(String(20), default="not_checked")
    source: Mapped[str] = mapped_column(String(50), default="")  # "rolled:<id>", "summary:<id>"
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="related_parties")


class DocumentSummary(Base):
    """A contract or board minutes the AI summarised for the auditor (summaries.py). The file
    itself isn't kept, only its name, size and fingerprint."""

    __tablename__ = "document_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # contract | minutes | other
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(5), default="en")
    content: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(Text, default="")  # the auditor's own note
    reviewed_by: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="document_summaries")


class Confirmation(Base):
    """A request for external confirmation (ISA 505) sent to a bank, customer, supplier,
    lawyer or other party, and its reply (confirmations.py)."""

    __tablename__ = "confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # bank | receivable | payable | legal | other
    party: Mapped[str] = mapped_column(String(300))
    reference: Mapped[str] = mapped_column(String(200), default="")  # account or customer code
    contact: Mapped[str] = mapped_column(Text, default="")
    form: Mapped[str] = mapped_column(String(20), default="positive")  # positive|negative|blank
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)  # per the client's books
    status: Mapped[str] = mapped_column(String(20), default="to_send")
    sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reminder_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmed: Mapped[float | None] = mapped_column(Float, nullable=True)  # per the reply
    difference_note: Mapped[str] = mapped_column(Text, default="")
    alternative: Mapped[str] = mapped_column(Text, default="")  # procedures when no reply
    conclusion: Mapped[str] = mapped_column(Text, default="")
    workpaper: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(50), default="")  # "upload:<id>" if listed
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="confirmations")


class Control(Base):
    """An internal control on the engagement's controls register: its design and
    implementation, and the test of its operating effectiveness (controls.py)."""

    __tablename__ = "controls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    area: Mapped[str] = mapped_column(String(50))  # as risks.area
    process: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    # A risk on the register it addresses. Not a foreign key: removing the risk clears it.
    risk_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="preventive")
    nature: Mapped[str] = mapped_column(String(20), default="manual")
    frequency: Mapped[str] = mapped_column(String(20), default="monthly")
    key: Mapped[bool] = mapped_column(Boolean, default=True)
    owner: Mapped[str] = mapped_column(String(200), default="")
    design: Mapped[str] = mapped_column(String(20), default="not_assessed")
    design_note: Mapped[str] = mapped_column(Text, default="")
    walkthrough: Mapped[str] = mapped_column(String(200), default="")  # workpaper reference
    # The test of operating effectiveness: plan, items picked, and result.
    method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # statistical|frequency
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 90 or 95 (%)
    tolerable_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # %
    expected_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # %
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected: Mapped[list | None] = mapped_column(JSON, nullable=True)  # item numbers picked
    tested: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deviations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deviation_note: Mapped[str] = mapped_column(Text, default="")
    test_conclusion: Mapped[str] = mapped_column(Text, default="")
    test_workpaper: Mapped[str] = mapped_column(String(200), default="")
    tested_by: Mapped[str] = mapped_column(String(200), default="")
    tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="")  # "rolled:<id>" if carried
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="controls")


class Risk(Base):
    """A risk of material misstatement on the engagement's risk register."""

    __tablename__ = "risks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"))
    area: Mapped[str] = mapped_column(String(50))  # a statement line id, or entity/fraud/...
    title: Mapped[str] = mapped_column(String(300))
    level: Mapped[str] = mapped_column(String(20))
    assertions: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(50), default="")  # "ai-draft:<id>" if imported
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="risks")
    procedures: Mapped[list[RiskProcedure]] = relationship(
        back_populates="risk", cascade="all, delete-orphan", order_by="RiskProcedure.id"
    )


class RiskProcedure(Base):
    """An audit procedure planned in response to a risk, and its result once done."""

    __tablename__ = "risk_procedures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    risk_id: Mapped[int] = mapped_column(ForeignKey("risks.id"))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="planned")
    conclusion: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(String(200), default="")  # the workpaper
    done_by: Mapped[str] = mapped_column(String(200), default="")
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    risk: Mapped[Risk] = relationship(back_populates="procedures")

    @property
    def engagement_id(self) -> int:
        return self.risk.engagement_id


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
    evaluation: Mapped[SampleEvaluation | None] = relationship(
        back_populates="sample", cascade="all, delete-orphan", uselist=False
    )


class SampleEvaluation(Base):
    """The results of testing a sample's items, and the misstatement projected from them."""

    __tablename__ = "sample_evaluations"

    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), primary_key=True)
    tested: Mapped[list] = mapped_column(JSON)  # [{row, audited_amount, notes}]
    tolerable_misstatement: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sample: Mapped[Sample] = relationship(back_populates="evaluation")


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
    settings: Mapped[UserSetting | None] = relationship(cascade="all, delete-orphan")

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


class UserSetting(Base):
    """A user's own preferences, such as the language the app shows in.

    A separate table so databases made before it get it without a migration.
    """

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(10), default="en")


class EngagementMember(Base):
    """A member of the audit team assigned to an engagement. Only admins see engagements
    they aren't assigned to (auth.on_engagement)."""

    __tablename__ = "engagement_members"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="team")
    user: Mapped[User] = relationship()


class AuthSession(Base):
    """A signed-in browser. Only a hash of the cookie token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    user: Mapped[User] = relationship()


class SignupRequest(Base):
    """Someone who asked for an account on the sign-in page. They can't sign in until an admin
    approves the request, which turns it into a user; rejecting it deletes it.

    A separate table so databases made before sign-up get it without a migration.
    """

    __tablename__ = "signup_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")


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
    _summarize_runs()


# Columns added to a table after it first shipped: create_all doesn't add them.
ADDED_COLUMNS = {
    "related_parties": {"scan_upload_id": "INTEGER",
                        "scan_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''"},
}


def _upgrade() -> None:
    """Changes create_all can't make to tables an earlier version created."""
    with engine.begin() as conn:
        for table, columns in ADDED_COLUMNS.items():
            have = {c["name"] for c in sa_inspect(conn).get_columns(table)}
            for name, ddl in columns.items():
                if name not in have:
                    # IF NOT EXISTS: another worker starting at the same time may have added it.
                    exists_ok = " IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN{exists_ok} {name} {ddl}"))
    if engine.dialect.name == "postgresql":  # SQLite doesn't enforce VARCHAR lengths
        with engine.begin() as conn:
            for table in ("account_mappings", "adjusting_lines"):  # account was VARCHAR(100)
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN account TYPE VARCHAR(200)"))


def _summarize_runs() -> None:
    """Give runs saved before run_summaries existed their summary, a few at a time."""
    with Session(engine) as session:
        missing = list(session.scalars(select(Run.id).where(
            ~exists().where(RunSummary.run_id == Run.id)).order_by(Run.id)))
        for start in range(0, len(missing), 50):
            ids = missing[start:start + 50]
            runs = session.scalars(select(Run).where(Run.id.in_(ids))
                                   .options(undefer(Run.result)))
            for run in runs:
                session.add(RunSummary(run_id=run.id, **RunSummary.of(run.result)))
            try:
                session.commit()
            except IntegrityError:  # another worker starting at the same time did these
                session.rollback()
            session.expunge_all()


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
