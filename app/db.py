"""Database models and session handling (SQLAlchemy, SQLite by default).

Set AUDIT_DB_URL to use another database, e.g. postgresql+psycopg://user:pw@host/audit.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
)
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


class Engagement(Base):
    __tablename__ = "engagements"

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


def _make_engine(url: str):
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
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


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
