from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "unknown"


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_name: Mapped[str] = mapped_column(String(200), nullable=False)
    app_slug: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    served_from_cache: Mapped[bool] = mapped_column(default=False, nullable=False)
    source_report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    tool_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[dict] = mapped_column(JSON, default=dict)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trace: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    report: Mapped[AppReport | None] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan"
    )


class AppReport(Base):
    __tablename__ = "app_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )

    app_name: Mapped[str] = mapped_column(String(200), nullable=False)
    app_slug: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)

    category: Mapped[str] = mapped_column(String(48), nullable=False)
    access_tier: Mapped[str] = mapped_column(String(48), nullable=False)
    verdict: Mapped[str] = mapped_column(String(48), nullable=False)
    mcp_status: Mapped[str] = mapped_column(String(24), nullable=False)
    human_review_needed: Mapped[bool] = mapped_column(default=False, nullable=False)

    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[ResearchRun] = relationship(back_populates="report")
    evidence: Mapped[list[ReportEvidence]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_app_reports_slug_created", "app_slug", "created_at"),)


class ReportEvidence(Base):
    __tablename__ = "report_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("app_reports.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote: Mapped[str] = mapped_column(Text, nullable=False)

    report: Mapped[AppReport] = relationship(back_populates="evidence")
