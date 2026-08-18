from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import AppResearchReport, VerificationResult
from app.db.models import AppReport, ReportEvidence, ResearchRun, slugify, utcnow
from app.db.session import session_scope

logger = logging.getLogger(__name__)


class ResearchRepository:
    async def start_run(self, run_id: str, app_name: str) -> None:
        async with session_scope() as session:
            session.add(
                ResearchRun(
                    id=run_id,
                    app_name=app_name,
                    app_slug=slugify(app_name),
                    status="running",
                )
            )

    async def fail_run(self, run_id: str, error: str) -> None:
        async with session_scope() as session:
            run = await session.get(ResearchRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.error = error[:2000]
            run.completed_at = utcnow()

    async def get_fresh_report(
        self, app_name: str, *, max_age_hours: int
    ) -> tuple[AppResearchReport, int] | None:
        cutoff = utcnow() - timedelta(hours=max_age_hours)
        async with session_scope() as session:
            stmt = (
                select(AppReport)
                .where(AppReport.app_slug == slugify(app_name), AppReport.created_at >= cutoff)
                .order_by(desc(AppReport.created_at))
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        try:
            return AppResearchReport.model_validate(row.payload), row.id
        except Exception:
            logger.warning("Cached report %s failed validation; ignoring cache", row.id)
            return None

    async def finish_cached_run(
        self,
        *,
        run_id: str,
        app_name: str,
        source_report_id: int | None,
        provider: dict[str, Any],
        trace: list[dict[str, Any]],
    ) -> None:
        async with session_scope() as session:
            run = await session.get(ResearchRun, run_id)
            if run is None:
                run = ResearchRun(id=run_id, app_name=app_name, app_slug=slugify(app_name))
                session.add(run)
            run.status = "completed"
            run.completed_at = utcnow()
            run.served_from_cache = True
            run.source_report_id = source_report_id
            run.provider = provider
            run.trace = trace
            run.usage = {"calls": 0, "cost_usd": 0.0}

    async def save_run(
        self,
        *,
        run_id: str,
        app_name: str,
        report: AppResearchReport,
        verification: VerificationResult | None,
        usage: dict[str, Any],
        tool_stats: dict[str, Any],
        provider: dict[str, Any],
        trace: list[dict[str, Any]],
        retries: int,
    ) -> None:
        async with session_scope() as session:
            run = await session.get(ResearchRun, run_id)
            if run is None:
                run = ResearchRun(id=run_id, app_name=app_name, app_slug=slugify(app_name))
                session.add(run)

            run.status = "completed"
            run.completed_at = utcnow()
            run.usage = usage
            run.tool_stats = tool_stats
            run.provider = provider
            run.trace = trace
            run.retries = retries
            run.verification = (
                verification.model_dump(mode="json") if verification is not None else None
            )

            stored = AppReport(
                run_id=run_id,
                app_name=app_name,
                app_slug=slugify(app_name),
                canonical_name=report.canonical_name,
                category=report.category.value,
                access_tier=report.access_tier.value,
                verdict=report.verdict.value,
                mcp_status=report.mcp_status.value,
                human_review_needed=report.human_review_needed,
                payload=report.model_dump(mode="json"),
            )
            stored.evidence = [
                ReportEvidence(url=e.url, title=e.title, quote=e.quote) for e in report.evidence
            ]
            session.add(stored)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with session_scope() as session:
            run = await session.get(ResearchRun, run_id)
            if run is None:
                return None
            report = await _resolve_report(session, run)
            return _run_to_dict(run, report)

    async def latest_report(self, app_name: str) -> dict[str, Any] | None:
        async with session_scope() as session:
            stmt = (
                select(AppReport)
                .where(AppReport.app_slug == slugify(app_name))
                .order_by(desc(AppReport.created_at))
                .limit(1)
            )
            report = (await session.execute(stmt)).scalar_one_or_none()
            if report is None:
                return None
            run = await session.get(ResearchRun, report.run_id)
            return _run_to_dict(run, report) if run else None

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        async with session_scope() as session:
            stmt = select(ResearchRun).order_by(desc(ResearchRun.created_at)).limit(limit)
            runs = list((await session.execute(stmt)).scalars())
            run_ids = [r.id for r in runs] or [""]
            source_ids = [r.source_report_id for r in runs if r.source_report_id] or [-1]
            rows = list(
                (
                    await session.execute(
                        select(AppReport).where(
                            (AppReport.run_id.in_(run_ids)) | (AppReport.id.in_(source_ids))
                        )
                    )
                ).scalars()
            )
            by_run = {r.run_id: r for r in rows}
            by_id = {r.id: r for r in rows}
        return [
            _run_summary(run, by_run.get(run.id) or by_id.get(run.source_report_id or -1))
            for run in runs
        ]


async def _resolve_report(session: AsyncSession, run: ResearchRun) -> AppReport | None:
    stmt = select(AppReport).where(AppReport.run_id == run.id).limit(1)
    report = (await session.execute(stmt)).scalar_one_or_none()
    if report is None and run.source_report_id is not None:
        report = await session.get(AppReport, run.source_report_id)
    return report


def _run_summary(run: ResearchRun, report: AppReport | None) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "app_name": run.app_name,
        "status": run.status,
        "error": run.error,
        "retries": run.retries,
        "served_from_cache": run.served_from_cache,
        "usage": run.usage or {},
        "tool_stats": run.tool_stats or {},
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "verdict": report.verdict if report else None,
        "access_tier": report.access_tier if report else None,
        "human_review_needed": report.human_review_needed if report else None,
    }


def _run_to_dict(run: ResearchRun, report: AppReport | None) -> dict[str, Any]:
    data = _run_summary(run, report)
    data["report"] = report.payload if report else None
    data["verification"] = run.verification
    data["provider"] = run.provider or {}
    data["trace"] = run.trace or []
    return data
