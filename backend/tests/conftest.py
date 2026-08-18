from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from app.agent.llm import Usage
from app.agent.schemas import (
    AppResearchReport,
    Confidence,
    LaneFindings,
    ResolvedApp,
    VerificationResult,
)

DOCS_URL = "https://developer.example.com/docs/authentication"
PRICING_URL = "https://example.com/pricing"


@dataclass
class FakeTool:
    name: str = "COMPOSIO_SEARCH_TAVILY_SEARCH"
    calls: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] | None = None

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.calls.append(args)
        body = self.payload or {
            "results": [
                {
                    "url": DOCS_URL,
                    "title": "Authentication",
                    "content": "The API supports OAuth 2.0 and API keys. Free developer accounts "
                    "can create an API key from the dashboard.",
                },
                {"url": PRICING_URL, "title": "Pricing", "content": "API access on all plans."},
            ]
        }
        return json.dumps(body)


@dataclass
class FakeProvider:
    tool: FakeTool = field(default_factory=FakeTool)

    @property
    def tools(self) -> list[Any]:
        return [self.tool]

    def describe(self) -> dict[str, Any]:
        return {"provider": "fake", "tools_resolved": [self.tool.name]}


class BrokenProvider:
    @property
    def tools(self) -> list[Any]:
        raise RuntimeError("no toolkits connected")

    def describe(self) -> dict[str, Any]:
        return {"provider": "broken"}


def _usage(inp: int = 400, out: int = 120, cache_read: int = 0) -> Usage:
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cache_read,
        calls=1,
        cost_usd=0.001,
        by_model={"fake": 1},
    )


SAMPLE_FINDINGS = LaneFindings(
    findings=[
        {
            "claim": "The API supports OAuth 2.0 and API keys.",
            "evidence": {
                "url": DOCS_URL,
                "title": "Authentication",
                "quote": "The API supports OAuth 2.0 and API keys.",
            },
            "confidence": Confidence.HIGH,
        }
    ],
    unresolved=[],
)


def sample_report(**overrides: Any) -> AppResearchReport:
    base: dict[str, Any] = {
        "canonical_name": "Example App",
        "vendor": "Example Inc",
        "homepage": "https://example.com",
        "docs_url": DOCS_URL,
        "category": "developer_infra_data",
        "one_liner": "An example product used in tests.",
        "auth_methods": ["oauth2", "api_key"],
        "auth_notes": "OAuth 2.0 for user-scoped access; API keys for server-to-server.",
        "access_tier": "self_serve_free",
        "access_notes": "Free developer accounts can mint an API key from the dashboard.",
        "api_styles": ["rest"],
        "api_breadth": "moderate",
        "api_notes": "Versioned REST API with public reference docs.",
        "mcp_status": "none",
        "mcp_url": None,
        "verdict": "buildable_today",
        "blocker": None,
        "integration_notes": "Straightforward: OAuth app registration then REST calls.",
        "evidence": [{"url": DOCS_URL, "title": "Authentication", "quote": "supports OAuth 2.0"}],
        "confidence": {
            "auth": "high",
            "access": "high",
            "api": "medium",
            "mcp": "low",
            "verdict": "high",
        },
        "unknowns": [],
        "human_review_needed": False,
        "human_review_reason": None,
    }
    base.update(overrides)
    return AppResearchReport.model_validate(base)


class FakeLLM:
    def __init__(
        self,
        *,
        report: AppResearchReport | None = None,
        verifications: list[VerificationResult] | None = None,
        findings: LaneFindings | None = None,
        search_first: bool = True,
    ) -> None:
        self.report = report or sample_report()
        self.verifications = verifications or [
            VerificationResult(passed=True, summary="All claims supported.")
        ]
        self.findings = findings or SAMPLE_FINDINGS
        self.search_first = search_first
        self.structured_calls: list[str] = []
        self.turns: int = 0
        self._verify_index = 0

    async def structured_call(self, tier, schema, system, human, *, max_tokens=4096):
        self.structured_calls.append(schema.__name__)
        if schema is ResolvedApp:
            return (
                ResolvedApp(
                    canonical_name="Example App",
                    vendor="Example Inc",
                    homepage="https://example.com",
                    likely_docs_urls=[DOCS_URL],
                    category="developer_infra_data",
                    one_liner="An example product used in tests.",
                ),
                _usage(200, 80),
            )
        if schema is AppResearchReport:
            return self.report, _usage(900, 400)
        if schema is VerificationResult:
            index = min(self._verify_index, len(self.verifications) - 1)
            self._verify_index += 1
            return self.verifications[index], _usage(700, 150)
        raise AssertionError(f"unexpected schema {schema}")

    async def tool_call_turn(self, tier, tools, messages, *, max_tokens=2048, tool_choice=None):
        self.turns += 1
        prior_ai = sum(1 for m in messages if isinstance(m, AIMessage))
        if self.search_first and prior_ai == 0 and tool_choice is None:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "COMPOSIO_SEARCH_TAVILY_SEARCH",
                        "args": {"query": "example app api authentication"},
                        "id": f"call_{self.turns}",
                    }
                ],
            )
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "LaneFindings",
                        "args": self.findings.model_dump(mode="json"),
                        "id": f"call_{self.turns}",
                    }
                ],
            )
        return message, _usage(500, 100)


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    llm = FakeLLM()
    monkeypatch.setattr("app.agent.nodes.structured_call", llm.structured_call)
    monkeypatch.setattr("app.agent.nodes.tool_call_turn", llm.tool_call_turn)
    return llm


@pytest.fixture
def patch_llm(monkeypatch):

    def _install(llm: FakeLLM) -> FakeLLM:
        monkeypatch.setattr("app.agent.nodes.structured_call", llm.structured_call)
        monkeypatch.setattr("app.agent.nodes.tool_call_turn", llm.tool_call_turn)
        return llm

    return _install
