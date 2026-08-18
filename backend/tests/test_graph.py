from __future__ import annotations

from typing import Any

import pytest

from app.agent.graph import build_graph
from app.agent.lanes import LANES
from app.agent.schemas import Confidence, FieldIssue, VerificationResult
from app.agent.service import ResearchService
from tests.conftest import BrokenProvider, FakeLLM, FakeProvider, sample_report


async def run_graph(
    llm: FakeLLM,
    *,
    provider: Any | None = None,
    repo: Any | None = None,
    app_name: str = "Example App",
    force_refresh: bool = False,
) -> dict[str, Any]:
    service = ResearchService(repo=repo, graph=build_graph(), provider=provider or FakeProvider())
    return await service.execute(app_name, force_refresh=force_refresh)


class TestHappyPath:
    async def test_produces_a_verified_report(self, patch_llm):
        llm = patch_llm(FakeLLM())
        result = await run_graph(llm)

        report = result["report"]
        assert report is not None
        assert report["canonical_name"] == "Example App"
        assert report["verdict"] == "buildable_today"
        assert result["verification"]["passed"] is True
        assert result["error"] is None

    async def test_all_lanes_run_in_parallel(self, patch_llm):
        llm = patch_llm(FakeLLM())
        result = await run_graph(llm)

        lanes_run = {entry["lane"] for entry in result["trace"] if entry.get("node") == "gather"}
        assert lanes_run == {lane.key for lane in LANES}

    async def test_usage_is_summed_across_every_call(self, patch_llm):
        llm = patch_llm(FakeLLM())
        result = await run_graph(llm)

        usage = result["usage"]
        assert usage["calls"] == 9
        assert usage["input_tokens"] > 0
        assert usage["cost_usd"] > 0

    async def test_shared_sources_are_fetched_once_across_lanes(self, patch_llm):
        llm = patch_llm(FakeLLM())
        provider = FakeProvider()
        result = await run_graph(llm, provider=provider)

        assert len(provider.tool.calls) == 1
        assert result["tool_stats"]["tool_calls"] == 3
        assert result["tool_stats"]["cache_hits"] == 2


class TestCitationIntegrity:
    async def test_fabricated_url_is_caught_deterministically(self, patch_llm):
        report = sample_report(
            evidence=[
                {
                    "url": "https://totally-invented.example/api",
                    "title": "Nope",
                    "quote": "made up",
                }
            ]
        )
        llm = patch_llm(FakeLLM(report=report))

        result = await run_graph(llm)

        assert result["fabricated_urls"] == ["https://totally-invented.example/api"]
        assert result["report"]["human_review_needed"] is True
        assert result["verification"]["passed"] is False

    async def test_genuine_citation_passes(self, patch_llm):
        llm = patch_llm(FakeLLM())
        result = await run_graph(llm)

        assert result["fabricated_urls"] == []
        assert result["report"]["human_review_needed"] is False


class TestVerificationLoop:
    async def test_failed_verification_triggers_targeted_re_research(self, patch_llm):
        llm = patch_llm(
            FakeLLM(
                verifications=[
                    VerificationResult(
                        passed=False,
                        issues=[
                            FieldIssue(
                                field="access_tier",
                                issue="No pricing page cited.",
                                severity=Confidence.HIGH,
                            )
                        ],
                        followup_queries=["Example App API pricing tier"],
                        summary="Access tier is unsupported.",
                    ),
                    VerificationResult(passed=True, summary="Resolved on re-check."),
                ]
            )
        )
        result = await run_graph(llm)

        assert result["retries"] == 1
        assert result["verification"]["passed"] is True

        gathers = [e for e in result["trace"] if e.get("node") == "gather"]
        assert len(gathers) == len(LANES) + 1
        assert gathers[-1]["lane"] == "auth_access"

    async def test_retry_budget_is_respected(self, patch_llm):
        always_failing = VerificationResult(
            passed=False,
            issues=[FieldIssue(field="auth", issue="unsupported", severity=Confidence.HIGH)],
            followup_queries=["more auth detail"],
            summary="Still unsupported.",
        )
        llm = patch_llm(FakeLLM(verifications=[always_failing]))

        result = await run_graph(llm)

        assert result["retries"] == 1
        assert result["verification"]["passed"] is False
        assert result["report"] is not None


class TestBudgets:
    async def test_lane_stops_at_the_search_budget(self, patch_llm, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("MAX_TOOL_CALLS_PER_LANE", "1")
        get_settings.cache_clear()
        try:
            assert get_settings().max_tool_calls_per_lane == 1

            llm = patch_llm(FakeLLM())
            result = await run_graph(llm, provider=FakeProvider())

            gathers = [e for e in result["trace"] if e.get("node") == "gather"]
            assert gathers, "lanes should have run"
            assert all(entry["tool_calls"] <= 1 for entry in gathers)
        finally:
            get_settings.cache_clear()


class TestFailureModes:
    async def test_unavailable_toolkits_degrade_instead_of_crashing(self, patch_llm):
        llm = patch_llm(FakeLLM())
        result = await run_graph(llm, provider=BrokenProvider())

        assert result["report"] is not None
        errors = [e for e in result["trace"] if e.get("node") == "gather" and e.get("error")]
        assert len(errors) == len(LANES)


class TestCache:
    async def test_fresh_cached_report_short_circuits_the_run(self, patch_llm):
        class CachingRepo:
            def __init__(self):
                self.saved = False
                self.closed: dict[str, Any] | None = None

            async def start_run(self, run_id, app_name):
                pass

            async def get_fresh_report(self, app_name, *, max_age_hours):
                return sample_report(), 42

            async def save_run(self, **kwargs):
                self.saved = True

            async def finish_cached_run(self, **kwargs):
                self.closed = kwargs

        llm = patch_llm(FakeLLM())
        repo = CachingRepo()
        provider = FakeProvider()
        result = await run_graph(llm, repo=repo, provider=provider)

        assert result["served_from_cache"] is True
        assert result["report"]["canonical_name"] == "Example App"
        assert llm.structured_calls == [], "a cache hit must not call a model at all"
        assert provider.tool.calls == [], "a cache hit must not hit the network either"
        assert result["usage"]["calls"] == 0

    async def test_cache_hit_closes_the_run_without_duplicating_the_report(self, patch_llm):

        class CachingRepo:
            def __init__(self):
                self.saved = False
                self.closed: dict[str, Any] | None = None

            async def start_run(self, run_id, app_name):
                pass

            async def get_fresh_report(self, app_name, *, max_age_hours):
                return sample_report(), 42

            async def save_run(self, **kwargs):
                self.saved = True

            async def finish_cached_run(self, **kwargs):
                self.closed = kwargs

        llm = patch_llm(FakeLLM())
        repo = CachingRepo()
        await run_graph(llm, repo=repo)

        assert repo.saved is False, "cache hits must not write a new report row"
        assert repo.closed is not None, "the run row must still be closed out"
        assert repo.closed["source_report_id"] == 42

    async def test_force_refresh_bypasses_the_cache(self, patch_llm):
        class CachingRepo:
            async def start_run(self, run_id, app_name):
                pass

            async def get_fresh_report(self, app_name, *, max_age_hours):
                raise AssertionError("cache must not be consulted on force_refresh")

            async def save_run(self, **kwargs):
                pass

        llm = patch_llm(FakeLLM())
        result = await run_graph(llm, repo=CachingRepo(), force_refresh=True)

        assert result["served_from_cache"] is False
        assert "AppResearchReport" in llm.structured_calls


class TestPersistence:
    async def test_completed_run_is_saved_with_its_cost(self, patch_llm):
        class RecordingRepo:
            def __init__(self):
                self.payload: dict[str, Any] | None = None

            async def start_run(self, run_id, app_name):
                pass

            async def get_fresh_report(self, app_name, *, max_age_hours):
                return None

            async def save_run(self, **kwargs):
                self.payload = kwargs

        llm = patch_llm(FakeLLM())
        repo = RecordingRepo()
        await run_graph(llm, repo=repo)

        assert repo.payload is not None
        assert repo.payload["report"].canonical_name == "Example App"
        assert repo.payload["usage"]["calls"] == 9
        assert repo.payload["tool_stats"]["tool_calls"] == 3


@pytest.mark.parametrize(
    "field,expected_lane",
    [
        ("auth_methods", "auth_access"),
        ("access_tier", "auth_access"),
        ("api_breadth", "api_mcp"),
        ("mcp_status", "api_mcp"),
        ("category", "identity"),
    ],
)
def test_issue_fields_route_to_the_lane_that_can_fix_them(field, expected_lane):
    from app.agent.nodes import _lanes_for_issues

    result = VerificationResult(
        passed=False,
        issues=[FieldIssue(field=field, issue="x", severity=Confidence.HIGH)],
        summary="",
    )
    assert _lanes_for_issues(result) == [expected_lane]
