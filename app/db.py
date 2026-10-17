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
from sqlalchemy.exc import IntegrityError, OperationalError
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
    ticks: Mapped[list[WorkpaperTick]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="WorkpaperTick.id"
    )
    translation: Mapped[CurrencyTranslation | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    components: Mapped[list[GroupComponent]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        foreign_keys="GroupComponent.engagement_id", order_by="GroupComponent.id"
    )
    # The groups this engagement is a component of; deleting it takes it out of them.
    component_of: Mapped[list[GroupComponent]] = relationship(
        back_populates="component", cascade="all, delete-orphan",
        foreign_keys="GroupComponent.component_engagement_id"
    )
    consolidation_entries: Mapped[list[ConsolidationEntry]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="ConsolidationEntry.id"
    )
    vat_setup: Mapped[VatSetup | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    vat_returns: Mapped[list[VatReturn]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="(VatReturn.period_start, VatReturn.id)"
    )
    plan: Mapped[EngagementPlan | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    bookings: Mapped[list[StaffBooking]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="(StaffBooking.starts_on, StaffBooking.id)"
    )
    leases: Mapped[list[Lease]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="(Lease.start_date, Lease.id)"
    )
    ecl_setup: Mapped[EclSetup | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
    )
    loans: Mapped[list[Loan]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Loan.id"
    )
    covenants: Mapped[list[Covenant]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Covenant.id"
    )
    estimates: Mapped[list[AccountingEstimate]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="AccountingEstimate.id"
    )
    checklists: Mapped[list[EngagementChecklist]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan",
        order_by="EngagementChecklist.id"
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
    quality_review: Mapped[QualityReview | None] = relationship(
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
    expectations: Mapped[list[Expectation]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="Expectation.id"
    )
    prior_points: Mapped[list[PriorPoint]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="PriorPoint.id"
    )
    stock_count: Mapped[StockCount | None] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", uselist=False
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


class QualityReview(Base):
    """The engagement quality review (ISQM 2): whether one is needed and why, the partner
    named to do it, their checklist and conclusion, and their sign-off (quality.py)."""

    __tablename__ = "quality_reviews"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason_note: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewer_name: Mapped[str] = mapped_column(String(200), default="")
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # {question: {answer, comment}}
    conclusion: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    signed_by: Mapped[str] = mapped_column(String(200), default="")
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_note: Mapped[str] = mapped_column(Text, default="")

    engagement: Mapped[Engagement] = relationship(back_populates="quality_review")


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


class Expectation(Base):
    """A substantive analytical procedure (ISA 520) or proof in total: what a statement line
    should be, worked out from inputs checked elsewhere, against what it is (expectations.py)."""

    __tablename__ = "expectations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="analytic")  # expectations.KINDS
    title: Mapped[str] = mapped_column(String(200), default="")
    line: Mapped[str | None] = mapped_column(String(50), nullable=True)  # statements.LINE
    method: Mapped[str] = mapped_column(String(20), default="prior_change")
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    basis: Mapped[str] = mapped_column(Text, default="")  # where the inputs come from
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)  # else perf. mat.
    # When the line holds more than what is tested, the actual figure entered by hand.
    actual_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_note: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")  # of a difference over it
    misstatement: Mapped[bool] = mapped_column(Boolean, default=False)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The expected, actual and threshold the review saw: other figures need a fresh look.
    reviewed_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(50), default="")  # "rolled:<id>" if carried
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="expectations")


class PriorPoint(Base):
    """A point raised last year (a management letter point, a finding added by hand, or a
    point still open the year before) and what was found when following it up this year
    (prior_points.py)."""

    __tablename__ = "prior_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # prior_points.ORIGINS
    # Where it came from, so the same point isn't brought in twice: "letter:<draft>:<n>",
    # "finding:<id>" or "point:<id>"; blank for points added by hand.
    ref: Mapped[str] = mapped_column(String(100), default="")
    from_engagement_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_name: Mapped[str] = mapped_column(String(200), default="")
    first_raised: Mapped[str] = mapped_column(String(200), default="")  # engagement name
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    management_response: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="to_follow_up")
    follow_up: Mapped[str] = mapped_column(Text, default="")  # what was done to check
    finding_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # raised this year
    checked_by: Mapped[str] = mapped_column(String(200), default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="prior_points")


class StockCount(Base):
    """Attendance at the client's inventory count (ISA 501): the items picked from the
    inventory listing to count from the books to the floor, and those picked on the floor to
    trace to the books (stock_count.py). One per engagement."""

    __tablename__ = "stock_counts"
    __table_args__ = (UniqueConstraint("engagement_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    # The inventory listing the items were picked from; kept as it was if the file is deleted.
    upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    columns: Mapped[dict] = mapped_column(JSON, default=dict)
    selection: Mapped[dict] = mapped_column(JSON, default=dict)  # how many, the seed
    population_value: Mapped[float] = mapped_column(Float, default=0.0)
    population_lines: Mapped[int] = mapped_column(Integer, default=0)
    listing_checks: Mapped[dict] = mapped_column(JSON, default=dict)  # odd lines found
    count_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    location: Mapped[str] = mapped_column(String(200), default="")
    observations: Mapped[str] = mapped_column(Text, default="")  # how the count was run
    conclusion: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="stock_count")
    items: Mapped[list[StockCountItem]] = relationship(
        back_populates="count", cascade="all, delete-orphan", order_by="StockCountItem.id"
    )


class StockCountItem(Base):
    """One item test counted: the quantity in the books against the quantity on the floor."""

    __tablename__ = "stock_count_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count_id: Mapped[int] = mapped_column(ForeignKey("stock_counts.id"), index=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10), default="to_floor")  # or "to_book"
    reason: Mapped[str] = mapped_column(String(20), default="")  # largest, random, floor
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)  # in the listing
    item: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(String(500), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    book_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    counted_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")  # condition, tag number, damage
    explanation: Mapped[str] = mapped_column(Text, default="")  # of a difference
    counted_by: Mapped[str] = mapped_column(String(200), default="")
    counted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    count: Mapped[StockCount] = relationship(back_populates="items")


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
    vouchings: Mapped[list[Vouching]] = relationship(
        back_populates="sample", cascade="all, delete-orphan", order_by="Vouching.row"
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


class TickMark(Base):
    """A tick mark in the firm's legend: its symbol and what it means (ticks.py)."""

    __tablename__ = "tick_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(8), unique=True)
    meaning: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkpaperTick(Base):
    """A tick mark on an account in the lead sheets, with its cross-reference: free text
    (a workpaper number) and/or a link to a workpaper in the app (ticks.py). The symbol's
    meaning is copied, so changing the legend later doesn't change what was ticked."""

    __tablename__ = "workpaper_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    account: Mapped[str] = mapped_column(String(200))
    symbol: Mapped[str] = mapped_column(String(8))
    meaning: Mapped[str] = mapped_column(String(300))
    reference: Mapped[str] = mapped_column(String(200), default="")
    link_kind: Mapped[str] = mapped_column(String(30), default="")  # ticks.LINKS
    link_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_label: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="ticks")


class Vouching(Base):
    """A sample item checked against its source document (an invoice, receipt or contract):
    what the AI read from the document and how it compares with the ledger line
    (vouching.py). One per item; vouching it again replaces it."""

    __tablename__ = "vouchings"
    __table_args__ = (UniqueConstraint("sample_id", "row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), index=True)
    row: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)  # loaded on download
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSON)  # what was read from it
    checks: Mapped[list] = mapped_column(JSON)   # [{check, ok, text}]
    status: Mapped[str] = mapped_column(String(20))  # vouching.STATUSES
    suggested_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sample: Mapped[Sample] = relationship(back_populates="vouchings")


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
    response: Mapped[FindingResponse | None] = relationship(
        back_populates="finding", cascade="all, delete-orphan", uselist=False
    )


class FindingResponse(Base):
    """A finding sent to the client for their response (responses.py): the auditors'
    recommendation, management's response and action plan with an owner and a due date, and
    the auditors' follow-up of whether the action was taken."""

    __tablename__ = "finding_responses"

    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    asked_by: Mapped[str] = mapped_column(String(200), default="")
    asked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    response: Mapped[str] = mapped_column(Text, default="")
    action_plan: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(200), default="")
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    responded_by: Mapped[str] = mapped_column(String(200), default="")
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    from_client: Mapped[bool] = mapped_column(Boolean, default=False)
    follow_up: Mapped[str] = mapped_column(String(20), default="open")  # responses.FOLLOW_UP
    follow_up_note: Mapped[str] = mapped_column(Text, default="")
    followed_up_by: Mapped[str] = mapped_column(String(200), default="")
    followed_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    finding: Mapped[Finding] = relationship(back_populates="response")


class CurrencyTranslation(Base):
    """How an engagement's statements are translated from the entity's functional currency
    into the presentation currency (translation.py). A rate is the number of presentation
    currency units for one functional currency unit."""

    __tablename__ = "currency_translations"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    functional: Mapped[str] = mapped_column(String(3))
    presentation: Mapped[str] = mapped_column(String(3))
    closing_rate: Mapped[float] = mapped_column(Float)
    average_rate: Mapped[float] = mapped_column(Float)
    equity_rate: Mapped[float] = mapped_column(Float)
    # Retained earnings and reserves at the start of the year, already in the presentation
    # currency; when left out, they are translated at the equity rate.
    opening_reserves: Mapped[float | None] = mapped_column(Float, nullable=True)
    opening_translation_reserve: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="translation")


class GroupComponent(Base):
    """A component of a group audit (consolidation.py): another engagement in the app whose
    statements are added into the group's, with the group's share of it and the materiality
    the group team sets for its auditors."""

    __tablename__ = "group_components"
    __table_args__ = (UniqueConstraint("engagement_id", "component_engagement_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    component_engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"),
                                                         index=True)
    ownership_pct: Mapped[float] = mapped_column(Float, default=100.0)
    materiality: Mapped[float | None] = mapped_column(Float, nullable=True)
    scope: Mapped[str] = mapped_column(String(20), default="full")  # consolidation.SCOPES
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="components",
                                                  foreign_keys=[engagement_id])
    component: Mapped[Engagement] = relationship(back_populates="component_of",
                                                 foreign_keys=[component_engagement_id])


class ConsolidationEntry(Base):
    """An elimination or other consolidation adjustment, by statement line."""

    __tablename__ = "consolidation_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    description: Mapped[str] = mapped_column(Text)
    lines: Mapped[list] = mapped_column(JSON)  # [{"line", "debit", "credit"}]
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="consolidation_entries")


class VatSetup(Base):
    """How VAT is reconciled for an engagement (vat.py): the ledger, the VAT rate and the
    ledger accounts for output and input VAT."""

    __tablename__ = "vat_setups"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # not a key: the
    # ledger can be deleted, and the reconciliation then asks for another
    rate_pct: Mapped[float] = mapped_column(Float, default=14.0)
    output_accounts: Mapped[list] = mapped_column(JSON, default=list)
    input_accounts: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="vat_setup")


class VatReturn(Base):
    """One VAT return as filed: its period and the figures declared."""

    __tablename__ = "vat_returns"

    id: Mapped[int] = mapped_column(primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    taxable_sales: Mapped[float] = mapped_column(Float, default=0.0)
    output_vat: Mapped[float] = mapped_column(Float, default=0.0)
    input_vat: Mapped[float] = mapped_column(Float, default=0.0)
    reference: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="vat_returns")


class EngagementPlan(Base):
    """The engagement letter's terms and the planning memo's own text (planning.py). The rest
    of both documents comes from the file: materiality, risks, team and budget."""

    __tablename__ = "engagement_plans"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    addressee: Mapped[str] = mapped_column(String(300), default="")
    firm_name: Mapped[str] = mapped_column(String(300), default="")
    partner_name: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(200), default="")
    letter_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="")
    fee_basis: Mapped[str] = mapped_column(String(20), default="fixed")  # fixed | hourly
    report_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    other_services: Mapped[str] = mapped_column(Text, default="")
    accepted_by: Mapped[str] = mapped_column(String(200), default="")
    accepted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    business: Mapped[str] = mapped_column(Text, default="")
    approach: Mapped[str] = mapped_column(String(20), default="substantive")
    timetable: Mapped[str] = mapped_column(Text, default="")
    other_notes: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="plan")


class StaffBooking(Base):
    """Someone booked to work on an engagement for a stretch of days (schedule.py)."""

    __tablename__ = "staff_bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    hours: Mapped[float] = mapped_column(Float)  # spread evenly over the days
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="bookings")
    user: Mapped[User] = relationship()


class Lease(Base):
    """A lease the client holds, recalculated under IFRS 16 (leases.py)."""

    __tablename__ = "leases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[date] = mapped_column(Date)
    term_months: Mapped[int] = mapped_column(Integer)
    payment: Mapped[float] = mapped_column(Float)
    frequency: Mapped[str] = mapped_column(String(20), default="monthly")  # leases.FREQUENCIES
    timing: Mapped[str] = mapped_column(String(20), default="arrears")  # advance | arrears
    rate_pct: Mapped[float] = mapped_column(Float)
    direct_costs: Mapped[float] = mapped_column(Float, default=0.0)
    # The client's own figures at the period end, to compare with.
    client_liability: Mapped[float | None] = mapped_column(Float, nullable=True)
    client_rou: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="leases")


class EclSetup(Base):
    """The provision matrix for expected credit losses on receivables (ecl.py): the listing,
    the loss rate for each age band and the allowance the client booked."""

    __tablename__ = "ecl_setups"

    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), primary_key=True)
    upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # not a key: the
    # listing can be deleted, and the matrix then asks for another
    terms_days: Mapped[int] = mapped_column(Integer, default=30)
    rates: Mapped[dict] = mapped_column(JSON, default=dict)  # age band -> loss rate %
    forward_pct: Mapped[float] = mapped_column(Float, default=0.0)
    booked: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="ecl_setup")


class Loan(Base):
    """A loan on the borrowings schedule (loans.py): the year's movements and the client's
    figures to compare with."""

    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    lender: Mapped[str] = mapped_column(String(200), default="")
    opening: Mapped[float] = mapped_column(Float, default=0.0)
    drawn: Mapped[float] = mapped_column(Float, default=0.0)
    repaid: Mapped[float] = mapped_column(Float, default=0.0)
    client_closing: Mapped[float | None] = mapped_column(Float, nullable=True)
    rate_pct: Mapped[float] = mapped_column(Float, default=0.0)
    client_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    client_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    maturity: Mapped[date | None] = mapped_column(Date, nullable=True)
    security: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="loans")


class Covenant(Base):
    """A loan covenant, tested on the draft statements (loans.py)."""

    __tablename__ = "covenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(30))  # loans.METRICS
    operator: Mapped[str] = mapped_column(String(10), default="min")  # min | max
    limit: Mapped[float] = mapped_column("limit_value", Float)
    actual: Mapped[float | None] = mapped_column(Float, nullable=True)  # for "other" only
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="covenants")
    loan: Mapped[Loan | None] = relationship()


class AccountingEstimate(Base):
    """An accounting estimate and how it was tested (estimates.py, ISA 540)."""

    __tablename__ = "accounting_estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    line: Mapped[str | None] = mapped_column(String(50), nullable=True)
    management: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(Text, default="")
    assumptions: Mapped[str] = mapped_column(Text, default="")
    uncertainty: Mapped[str] = mapped_column(String(20), default="moderate")
    approach: Mapped[str] = mapped_column(String(20), default="test_method")
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    prior_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    prior_outcome: Mapped[float | None] = mapped_column(Float, nullable=True)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="estimates")


class FirmChecklist(Base):
    """A checklist the firm wrote, that teams add to their engagements (checklists.py)."""

    __tablename__ = "firm_checklists"
    __table_args__ = {"sqlite_autoincrement": True}  # engagements' copies point at old ids

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    items: Mapped[list] = mapped_column(JSON)  # the questions, in order
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EngagementChecklist(Base):
    """An engagement's own copy of a firm checklist, and its answers (checklists.py)."""

    __tablename__ = "engagement_checklists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engagement_id: Mapped[int] = mapped_column(ForeignKey("engagements.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # not a key: the
    # firm's checklist can be deleted, and the engagement keeps its copy
    name: Mapped[str] = mapped_column(String(200))
    items: Mapped[list] = mapped_column(JSON)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # {index: {answer, comment}}
    conclusion: Mapped[str] = mapped_column(Text, default="")
    completed_by: Mapped[str] = mapped_column(String(200), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    engagement: Mapped[Engagement] = relationship(back_populates="checklists")


class RequestTemplate(Base):
    """A firm list of documents to ask clients for (request_lists.py)."""

    __tablename__ = "request_templates"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    items: Mapped[list] = mapped_column(JSON)  # [{title, details, due_days}]
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    # When the notifications were last looked at: later ones show as new.
    notifications_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    "user_settings": {"notifications_seen_at": "TIMESTAMP"},
    "related_parties": {"scan_upload_id": "INTEGER",
                        "scan_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''"},
}


def _upgrade() -> None:
    """Changes create_all can't make to tables an earlier version created."""
    for table, columns in ADDED_COLUMNS.items():
        with engine.connect() as conn:
            have = {c["name"] for c in sa_inspect(conn).get_columns(table)}
        for name, ddl in columns.items():
            if name in have:
                continue
            # Another worker starting at the same time may add it first: PostgreSQL skips it
            # with IF NOT EXISTS, SQLite (which hasn't that) says it's a duplicate.
            exists_ok = " IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN{exists_ok} {name} {ddl}"))
            except OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    _backfill_scans()
    if engine.dialect.name == "postgresql":  # SQLite doesn't enforce VARCHAR lengths
        with engine.begin() as conn:
            for table in ("account_mappings", "adjusting_lines"):  # account was VARCHAR(100)
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN account TYPE VARCHAR(200)"))


def _backfill_scans() -> None:
    """Related parties scanned before scan_upload_id existed: point them at the ledger the
    scan would have read (the latest one uploaded by then). With no such ledger left, the
    scan is forgotten so a fresh one is asked for, except on a locked file, which can't be
    scanned again and keeps what it has."""
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE related_parties SET scan_upload_id = (
                SELECT u.id FROM uploads u
                WHERE u.engagement_id = related_parties.engagement_id AND u.kind = 'gl'
                  AND u.uploaded_at <= related_parties.scanned_at
                ORDER BY u.uploaded_at DESC, u.id DESC LIMIT 1)
            WHERE scan_upload_id IS NULL AND scanned_at IS NOT NULL"""))
        conn.execute(text("""
            UPDATE related_parties SET scanned_at = NULL, matches = 0, matched_amount = 0,
                scan_fingerprint = '', reviewed_by = '', reviewed_at = NULL
            WHERE scan_upload_id IS NULL AND scanned_at IS NOT NULL
              AND engagement_id NOT IN (SELECT engagement_id FROM engagement_locks)"""))


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
