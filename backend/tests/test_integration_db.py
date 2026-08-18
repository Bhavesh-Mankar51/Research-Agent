from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select

from app.agent.graph import build_graph
from app.agent.service import ResearchService
from app.db.models import AppReport, ReportEvidence, ResearchRun, slugify
from app.db.session import init_models, session_scope
from tests.conftest import FakeLLM, FakeProvider, sample_report

pytestmark = pytest.mark.asyncio


async def _database_available() -> bool:
    try:
        await init_models()
        async with session_scope() as session:
            await session.execute(select(func.count()).select_from(ResearchRun))
        return True
    except Exception:
        return False


@pytest.fixture
async def db():
    if not await _database_available():
        pytest.skip("Postgres not reachable; run `docker compose up -d postgres`")
    app_name = f"IntegrationApp-{uuid.uuid4().hex[:8]}"
    yield app_name
    async with session_scope() as session:
        await session.execute(delete(ResearchRun).where(ResearchRun.app_slug == slugify(app_name)))


async def test_report_round_trips_through_postgres(db, patch_llm):
    app_name = db
    from app.db.repository import ResearchRepository

    repo = ResearchRepository()
    service = ResearchService(repo=repo, graph=build_graph(), provider=FakeProvider())
    patch_llm(FakeLLM())

    result = await service.execute(app_name)
    run_id = result["run_id"]

    stored = await repo.get_run(run_id)
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored["served_from_cache"] is False
    assert stored["report"]["canonical_name"] == "Example App"
    assert stored["report"]["auth_methods"] == ["oauth2", "api_key"]
    assert stored["usage"]["calls"] == 9
    assert stored["verification"]["passed"] is True

    async with session_scope() as session:
        report_id = (
            await session.execute(select(AppReport.id).where(AppReport.run_id == run_id))
        ).scalar_one()
        evidence_count = (
            await session.execute(
                select(func.count())
                .select_from(ReportEvidence)
                .where(ReportEvidence.report_id == report_id)
            )
        ).scalar_one()
    assert evidence_count == 1


async def test_second_run_is_served_from_cache_without_duplicating_the_report(db, patch_llm):
    app_name = db
    from app.db.repository import ResearchRepository

    repo = ResearchRepository()
    service = ResearchService(repo=repo, graph=build_graph(), provider=FakeProvider())

    llm = patch_llm(FakeLLM(report=sample_report(canonical_name="Cached App")))
    first = await service.execute(app_name)
    calls_after_first = len(llm.structured_calls)

    second = await service.execute(app_name)

    assert second["served_from_cache"] is True
    assert second["run_id"] != first["run_id"]
    assert len(llm.structured_calls) == calls_after_first, "cache hit must not call a model"

    async with session_scope() as session:
        report_rows = (
            await session.execute(
                select(func.count())
                .select_from(AppReport)
                .where(AppReport.app_slug == slugify(app_name))
            )
        ).scalar_one()
    assert report_rows == 1, "a cache hit must not write a second report row"

    stored = await repo.get_run(second["run_id"])
    assert stored["status"] == "completed"
    assert stored["served_from_cache"] is True
    assert stored["report"]["canonical_name"] == "Cached App"


async def test_force_refresh_writes_a_second_report(db, patch_llm):
    app_name = db
    from app.db.repository import ResearchRepository

    repo = ResearchRepository()
    service = ResearchService(repo=repo, graph=build_graph(), provider=FakeProvider())
    patch_llm(FakeLLM())

    await service.execute(app_name)
    await service.execute(app_name, force_refresh=True)

    async with session_scope() as session:
        report_rows = (
            await session.execute(
                select(func.count())
                .select_from(AppReport)
                .where(AppReport.app_slug == slugify(app_name))
            )
        ).scalar_one()
    assert report_rows == 2, "history is append-only; a forced re-run adds a row"
