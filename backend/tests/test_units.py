from __future__ import annotations

import pytest

from app.agent.llm import Usage, _salvage, usage_from_message
from app.agent.nodes import _QUOTE_LIMIT, _coerce_findings
from app.agent.schemas import AppResearchReport, LaneFindings
from app.agent.state import LaneResult, merge_lane_results, merge_usage
from app.agent.tools import EvidencePool, _toolkit_is_reachable
from app.db.models import slugify
from tests.conftest import DOCS_URL, FakeTool


class _RawWithArgs:
    def __init__(self, args: dict) -> None:
        self.tool_calls = [{"name": "x", "args": args, "id": "1"}]


class _FakeComposio:
    def __init__(self, auth_modes: list[str]) -> None:
        self._modes = auth_modes

    @property
    def toolkits(self):
        modes = self._modes

        class _Toolkits:
            @staticmethod
            def get(slug: str):
                return type(
                    "Meta",
                    (),
                    {"auth_config_details": [type("S", (), {"mode": m})() for m in modes]},
                )()

        return _Toolkits()


def _lane(key: str, n_findings: int = 0) -> LaneResult:
    return LaneResult(
        lane=key,
        label=key.title(),
        findings=LaneFindings(unresolved=["x"] * n_findings),
    )


class TestLaneReducer:
    def test_parallel_lanes_fan_in(self):
        merged = merge_lane_results([_lane("identity")], [_lane("auth_access")])
        assert {r.lane for r in merged} == {"identity", "auth_access"}

    def test_rerun_lane_replaces_rather_than_stacks(self):
        first = [_lane("auth_access", n_findings=1), _lane("identity")]
        rerun = [_lane("auth_access", n_findings=3)]
        merged = merge_lane_results(first, rerun)

        assert len(merged) == 2, "a re-run lane must replace its earlier result"
        auth = next(r for r in merged if r.lane == "auth_access")
        assert len(auth.findings.unresolved) == 3, "newest result wins"
        assert any(r.lane == "identity" for r in merged), "untouched lanes are preserved"

    def test_handles_empty_sides(self):
        assert merge_lane_results(None, None) == []
        assert len(merge_lane_results(None, [_lane("identity")])) == 1


class TestUsageReducer:
    def test_sums_without_mutating_inputs(self):
        left = Usage(input_tokens=100, output_tokens=10, calls=1, cost_usd=0.1)
        right = Usage(input_tokens=50, output_tokens=5, calls=1, cost_usd=0.05)

        total = merge_usage(left, right)

        assert total.input_tokens == 150
        assert total.calls == 2
        assert total.cost_usd == pytest.approx(0.15)
        assert left.input_tokens == 100

    def test_usage_from_message_excludes_cached_tokens_from_billable_input(self):
        class Msg:
            usage_metadata = {
                "input_tokens": 1000,
                "output_tokens": 200,
                "input_token_details": {"cache_read": 700, "cache_creation": 100},
            }

        usage = usage_from_message(Msg(), "claude-haiku-4-5")

        assert usage.input_tokens == 200, "cached reads must not bill at full input rate"
        assert usage.cache_read_tokens == 700
        assert usage.cache_write_tokens == 100
        assert usage.cost_usd == pytest.approx((395 * 1.0 + 200 * 5.0) / 1_000_000)


class TestEvidencePool:
    async def test_dedupes_identical_calls(self):
        pool = EvidencePool(max_chars=10_000)
        tool = FakeTool()

        first = await pool.run(tool, {"query": "same"})
        second = await pool.run(tool, {"query": "same"})

        assert len(tool.calls) == 1, "a repeated query must not be paid for twice"
        assert first.cached is False
        assert second.cached is True
        assert pool.stats()["cache_hits"] == 1

    async def test_different_args_are_separate_calls(self):
        pool = EvidencePool(max_chars=10_000)
        tool = FakeTool()

        await pool.run(tool, {"query": "a"})
        await pool.run(tool, {"query": "b"})

        assert len(tool.calls) == 2

    async def test_truncates_oversized_sources(self):
        pool = EvidencePool(max_chars=50)
        tool = FakeTool(payload={"results": [{"url": "https://x.dev", "content": "y" * 5_000}]})

        result = await pool.run(tool, {"query": "big"})

        assert len(result.content) == 50
        assert result.truncated is True

    async def test_records_urls_and_flags_fabricated_citations(self):
        pool = EvidencePool()
        await pool.run(FakeTool(), {"query": "auth"})

        assert DOCS_URL in pool.seen_urls
        assert pool.unknown_urls([DOCS_URL.upper() + "/"]) == []
        assert pool.unknown_urls(["https://invented.example/never-fetched"]) == [
            "https://invented.example/never-fetched"
        ]

    async def test_tool_failure_is_reported_not_raised(self):
        class Exploding(FakeTool):
            async def ainvoke(self, args):
                raise RuntimeError("upstream 503")

        pool = EvidencePool()
        result = await pool.run(Exploding(), {"query": "x"})

        assert result.error is not None
        assert "503" in result.content


def test_slugify_is_stable_across_formatting():
    assert slugify("Zoho CRM") == slugify("  zoho   crm ") == "zoho-crm"
    assert slugify("Monday.com") == "monday-com"
    assert slugify("!!!") == "unknown"


class TestLengthCapSalvage:
    def test_over_long_quote_keeps_the_finding(self):
        findings = _coerce_findings(
            {
                "findings": [
                    {
                        "claim": "Stripe ships an official MCP server.",
                        "evidence": {"url": DOCS_URL, "quote": "q" * 900},
                        "confidence": "high",
                    }
                ],
                "unresolved": [],
            }
        )

        assert len(findings.findings) == 1
        assert findings.findings[0].evidence.url == DOCS_URL
        assert len(findings.findings[0].evidence.quote) == _QUOTE_LIMIT

    def test_one_broken_finding_does_not_discard_its_siblings(self):
        findings = _coerce_findings(
            {
                "findings": [
                    {
                        "claim": "good",
                        "evidence": {"url": DOCS_URL, "quote": "short"},
                        "confidence": "high",
                    },
                    {"claim": "no evidence at all", "confidence": "high"},
                ],
                "unresolved": [],
            }
        )

        assert [f.claim for f in findings.findings] == ["good"]
        assert any("malformed" in u for u in findings.unresolved)

    def test_report_rejected_only_for_wordiness_is_recovered(self):
        payload = {
            "canonical_name": "Stripe",
            "vendor": "Stripe, Inc.",
            "homepage": "https://stripe.com",
            "docs_url": DOCS_URL,
            "category": "finance_and_fintech",
            "one_liner": "Payments API.",
            "auth_methods": ["api_key"],
            "auth_notes": "Secret keys.",
            "access_tier": "self_serve_free",
            "access_notes": "Sign up free.",
            "api_styles": ["rest"],
            "api_breadth": "broad",
            "api_notes": "Large surface.",
            "mcp_status": "official",
            "mcp_url": "https://mcp.stripe.com",
            "verdict": "buildable_today",
            "blocker": None,
            "integration_notes": "n" * 1_000,
            "evidence": [{"url": DOCS_URL, "title": None, "quote": "The Stripe API is REST."}],
            "confidence": {
                "auth": "high",
                "access": "high",
                "api": "high",
                "mcp": "high",
                "verdict": "high",
            },
            "unknowns": [],
            "human_review_needed": False,
        }

        recovered = _salvage(AppResearchReport, _RawWithArgs(payload))

        assert recovered is not None
        assert recovered.canonical_name == "Stripe"
        assert recovered.mcp_url == "https://mcp.stripe.com"
        assert len(recovered.integration_notes) == 800

    def test_a_substantive_error_is_not_papered_over(self):
        recovered = _salvage(AppResearchReport, _RawWithArgs({"canonical_name": "Stripe"}))

        assert recovered is None


class TestToolkitReachability:
    def test_toolkit_needing_auth_without_a_connection_is_skipped(self):
        client = _FakeComposio(auth_modes=["API_KEY"])

        reachable, reason = _toolkit_is_reachable(client, "FIRECRAWL", connected=set())

        assert reachable is False
        assert "API_KEY" in reason

    def test_no_auth_toolkit_is_reachable_without_a_connection(self):
        client = _FakeComposio(auth_modes=["NO_AUTH"])

        reachable, _ = _toolkit_is_reachable(client, "COMPOSIO_SEARCH", connected=set())

        assert reachable is True

    def test_connected_toolkit_is_reachable(self):
        client = _FakeComposio(auth_modes=["API_KEY"])

        reachable, _ = _toolkit_is_reachable(client, "EXA", connected={"EXA"})

        assert reachable is True

    def test_a_failed_probe_does_not_strip_the_agent_of_web_reach(self):
        class Broken:
            @property
            def toolkits(self):
                raise RuntimeError("metadata endpoint down")

        reachable, _ = _toolkit_is_reachable(Broken(), "EXA", connected=set())

        assert reachable is True
