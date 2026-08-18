from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from app.agent.lanes import LANES, LANES_BY_KEY, build_hints
from app.agent.llm import Tier, Usage, cached_system, structured_call, tool_call_turn
from app.agent.prompts import (
    LANE_PROMPT,
    RESOLVE_PROMPT,
    RUBRIC,
    SYNTHESIZE_PROMPT,
    VERIFY_PROMPT,
)
from app.agent.runtime import get_context
from app.agent.schemas import (
    AppResearchReport,
    Confidence,
    Evidence,
    Finding,
    LaneFindings,
    ResolvedApp,
    VerificationResult,
)
from app.agent.state import LaneResult, ResearchState
from app.config import get_settings

logger = logging.getLogger(__name__)

FINISH_TOOL = "LaneFindings"

_QUOTE_LIMIT = Evidence.model_fields["quote"].metadata[0].max_length

_FIELD_TO_LANE = {
    "auth": "auth_access",
    "access": "auth_access",
    "api": "api_mcp",
    "mcp": "api_mcp",
    "category": "identity",
    "one_liner": "identity",
    "docs_url": "identity",
    "homepage": "identity",
    "vendor": "identity",
    "verdict": "auth_access",
}


async def lookup_cache(state: ResearchState) -> dict[str, Any]:
    ctx = get_context(state["run_id"])
    settings = get_settings()
    await ctx.event("node_start", node="lookup_cache")

    if state.get("force_refresh") or settings.report_cache_ttl_hours <= 0 or ctx.repo is None:
        return {"served_from_cache": False, "trace": [{"node": "lookup_cache", "hit": False}]}

    cached = await ctx.repo.get_fresh_report(
        state["app_name"], max_age_hours=settings.report_cache_ttl_hours
    )
    if cached is None:
        return {"served_from_cache": False, "trace": [{"node": "lookup_cache", "hit": False}]}

    report, report_id = cached
    await ctx.event("cache_hit", app=state["app_name"])
    return {
        "report": report,
        "cached_report_id": report_id,
        "served_from_cache": True,
        "trace": [{"node": "lookup_cache", "hit": True, "report_id": report_id}],
    }


def route_after_cache(state: ResearchState) -> str:
    return "cached" if state.get("served_from_cache") else "research"


async def resolve(state: ResearchState) -> dict[str, Any]:
    ctx = get_context(state["run_id"])
    await ctx.event("node_start", node="resolve")

    resolved, usage = await structured_call(
        Tier.WORKER,
        ResolvedApp,
        cached_system(RUBRIC, RESOLVE_PROMPT),
        f"App name: {state['app_name']}",
        max_tokens=1024,
    )
    if resolved is None:
        resolved = ResolvedApp(
            canonical_name=state["app_name"],
            category="other",
            one_liner=f"Unresolved product: {state['app_name']}",
            ambiguous=True,
            disambiguation_note="Resolution step returned no structured result.",
        )

    await ctx.event(
        "resolved",
        canonical_name=resolved.canonical_name,
        category=resolved.category.value,
        ambiguous=resolved.ambiguous,
    )
    return {
        "resolved": resolved,
        "usage": usage,
        "trace": [{"node": "resolve", "canonical_name": resolved.canonical_name}],
    }


def fan_out(state: ResearchState) -> list[Any]:
    from langgraph.types import Send

    focus = state.get("focus_lanes") or [lane.key for lane in LANES]
    return [
        Send(
            "gather",
            {
                "run_id": state["run_id"],
                "app_name": state["app_name"],
                "resolved": state.get("resolved"),
                "lane": lane_key,
                "followup_queries": state.get("followup_queries") or [],
            },
        )
        for lane_key in focus
    ]


async def gather(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = get_context(payload["run_id"])
    settings = get_settings()
    lane = LANES_BY_KEY[payload["lane"]]
    resolved: ResolvedApp | None = payload.get("resolved")
    app = resolved.canonical_name if resolved else payload["app_name"]

    await ctx.event("lane_start", lane=lane.key, label=lane.label)

    hints = build_hints(lane, app, resolved.likely_docs_urls if resolved else [])
    hints += [
        f"Follow-up requested by verification: {q}" for q in payload.get("followup_queries", [])
    ]

    try:
        search_tools = ctx.provider.tools
    except Exception as exc:
        logger.exception("Tool provider unavailable for lane %s", lane.key)
        return {
            "lane_results": [
                LaneResult(
                    lane=lane.key,
                    label=lane.label,
                    findings=LaneFindings(unresolved=[lane.question]),
                    error=f"tool provider unavailable: {exc}",
                )
            ],
            "trace": [{"node": "gather", "lane": lane.key, "error": str(exc)[:200]}],
        }

    tools_by_name = {t.name: t for t in search_tools}
    bound_tools: list[Any] = [*search_tools, LaneFindings]

    budget = settings.max_tool_calls_per_lane
    messages: list[Any] = [
        cached_system(RUBRIC),
        HumanMessage(
            content=LANE_PROMPT.format(
                app=app,
                question=lane.question,
                hints="\n".join(f"- {h}" for h in hints),
                budget=budget,
            )
        ),
    ]

    usage = Usage()
    findings: LaneFindings | None = None
    tool_calls_made = 0

    for _ in range(budget + 1):
        response, turn_usage = await tool_call_turn(Tier.WORKER, bound_tools, messages)
        usage.merge(turn_usage)
        messages.append(response)

        calls = getattr(response, "tool_calls", None) or []
        finish = next((c for c in calls if c["name"] == FINISH_TOOL), None)
        if finish is not None:
            findings = _coerce_findings(finish.get("args"))
            break
        if not calls:
            break

        for call in calls:
            tool = tools_by_name.get(call["name"])
            if tool is None:
                messages.append(
                    ToolMessage(content=f"Unknown tool {call['name']}", tool_call_id=call["id"])
                )
                continue
            if tool_calls_made >= budget:
                messages.append(
                    ToolMessage(
                        content="Search budget exhausted. Emit LaneFindings with what you have.",
                        tool_call_id=call["id"],
                    )
                )
                continue

            result = await ctx.pool.run(tool, call.get("args") or {})
            if result.error is None:
                tool_calls_made += 1
            await ctx.event(
                "tool_call",
                lane=lane.key,
                tool=result.tool,
                cached=result.cached,
                urls=len(result.urls),
            )
            note = " [truncated]" if result.truncated else ""
            messages.append(
                ToolMessage(
                    content=f"[{result.tool}]{note}\n{result.content}", tool_call_id=call["id"]
                )
            )

    if findings is None:
        forced, forced_usage = await tool_call_turn(
            Tier.WORKER, [LaneFindings], messages, tool_choice=FINISH_TOOL
        )
        usage.merge(forced_usage)
        calls = getattr(forced, "tool_calls", None) or []
        findings = _coerce_findings(calls[0].get("args") if calls else None)

    await ctx.event(
        "lane_done",
        lane=lane.key,
        findings=len(findings.findings),
        unresolved=len(findings.unresolved),
    )
    return {
        "lane_results": [
            LaneResult(
                lane=lane.key, label=lane.label, findings=findings, tool_calls=tool_calls_made
            )
        ],
        "usage": usage,
        "trace": [
            {
                "node": "gather",
                "lane": lane.key,
                "tool_calls": tool_calls_made,
                "findings": len(findings.findings),
            }
        ],
    }


def _coerce_findings(args: Any) -> LaneFindings:
    if isinstance(args, LaneFindings):
        return args
    if not isinstance(args, dict):
        return LaneFindings(unresolved=["Lane produced no parseable findings."])

    try:
        return LaneFindings.model_validate(args)
    except Exception:
        pass

    kept: list[Finding] = []
    dropped = 0
    for raw in args.get("findings") or []:
        try:
            kept.append(Finding.model_validate(raw))
        except Exception:
            try:
                trimmed = dict(raw)
                evidence = dict(trimmed.get("evidence") or {})
                quote = evidence.get("quote")
                if isinstance(quote, str) and len(quote) > _QUOTE_LIMIT:
                    evidence["quote"] = quote[: _QUOTE_LIMIT - 1].rstrip() + "…"
                    trimmed["evidence"] = evidence
                    kept.append(Finding.model_validate(trimmed))
                    continue
            except Exception:
                pass
            dropped += 1

    unresolved = [u for u in (args.get("unresolved") or []) if isinstance(u, str)][:4]
    if dropped:
        logger.warning("Dropped %d unparseable finding(s); kept %d", dropped, len(kept))
        unresolved.append(f"{dropped} finding(s) from this lane were malformed and discarded.")
    if not kept and not unresolved:
        logger.warning("Malformed LaneFindings payload: %r", args)
        unresolved = ["Lane produced no parseable findings."]
    return LaneFindings(findings=kept[:8], unresolved=unresolved[:4])


def render_findings(lane_results: list[LaneResult]) -> tuple[str, str]:
    blocks: list[str] = []
    unresolved: list[str] = []
    for result in sorted(lane_results, key=lambda r: r.lane):
        lines = [f"## {result.label}"]
        if result.error:
            lines.append(f"(lane failed: {result.error})")
        for finding in result.findings.findings:
            lines.append(f"- ({finding.confidence.value}) {finding.claim}")
            lines.append(f'  source: {finding.evidence.url} — "{finding.evidence.quote}"')
        if not result.findings.findings and not result.error:
            lines.append("- (no findings)")
        blocks.append("\n".join(lines))
        unresolved += [f"[{result.label}] {u}" for u in result.findings.unresolved]
    return "\n\n".join(blocks), "\n".join(f"- {u}" for u in unresolved) or "- (none)"


async def synthesize(state: ResearchState) -> dict[str, Any]:
    ctx = get_context(state["run_id"])
    await ctx.event("node_start", node="synthesize")

    lane_results = state.get("lane_results") or []
    findings_text, unresolved_text = render_findings(lane_results)
    resolved = state.get("resolved")
    app = resolved.canonical_name if resolved else state["app_name"]

    report, usage = await structured_call(
        Tier.ORCHESTRATOR,
        AppResearchReport,
        cached_system(RUBRIC),
        SYNTHESIZE_PROMPT.format(app=app, findings=findings_text, unresolved=unresolved_text),
        max_tokens=4096,
    )
    if report is None:
        return {
            "error": "Synthesis returned no structured report.",
            "usage": usage,
            "trace": [{"node": "synthesize", "ok": False}],
        }

    fabricated = ctx.pool.unknown_urls([e.url for e in report.evidence])
    if fabricated:
        report.human_review_needed = True
        report.human_review_reason = (
            (report.human_review_reason or "")
            + f" Cited {len(fabricated)} URL(s) not present in any fetched source."
        ).strip()
        logger.warning("Run %s cited unseen URLs: %s", state["run_id"], fabricated)

    await ctx.event(
        "synthesized",
        verdict=report.verdict.value,
        evidence=len(report.evidence),
        fabricated_urls=len(fabricated),
    )
    return {
        "report": report,
        "fabricated_urls": fabricated,
        "usage": usage,
        "trace": [{"node": "synthesize", "fabricated_urls": len(fabricated)}],
    }


async def verify(state: ResearchState) -> dict[str, Any]:
    ctx = get_context(state["run_id"])
    await ctx.event("node_start", node="verify")

    report = state.get("report")
    if report is None:
        return {"trace": [{"node": "verify", "skipped": True}]}

    findings_text, _ = render_findings(state.get("lane_results") or [])
    result, usage = await structured_call(
        Tier.ORCHESTRATOR,
        VerificationResult,
        cached_system(RUBRIC),
        VERIFY_PROMPT.format(
            report=report.model_dump_json(indent=None, exclude_none=True),
            findings=findings_text,
        ),
        max_tokens=2048,
    )
    if result is None:
        result = VerificationResult(
            passed=False, summary="Verification step returned no structured result."
        )

    fabricated = state.get("fabricated_urls") or []
    if fabricated:
        result.passed = False
        result.issues.append(
            _issue("evidence", f"Uncited sources: {', '.join(fabricated[:3])}", Confidence.HIGH)
        )

    await ctx.event("verified", passed=result.passed, issues=len(result.issues))
    return {
        "verification": result,
        "usage": usage,
        "focus_lanes": _lanes_for_issues(result),
        "followup_queries": result.followup_queries,
        "trace": [{"node": "verify", "passed": result.passed, "issues": len(result.issues)}],
    }


def _issue(field: str, text: str, severity: Confidence):
    from app.agent.schemas import FieldIssue

    return FieldIssue(field=field, issue=text, severity=severity)


def _lanes_for_issues(result: VerificationResult) -> list[str]:
    lanes: list[str] = []
    for issue in result.issues:
        if issue.severity is Confidence.LOW:
            continue
        for prefix, lane_key in _FIELD_TO_LANE.items():
            if issue.field.lower().startswith(prefix) and lane_key not in lanes:
                lanes.append(lane_key)
    return lanes or ["auth_access"]


def route_after_verify(state: ResearchState) -> str:
    verification = state.get("verification")
    settings = get_settings()
    if verification is None or verification.passed:
        return "persist"
    if state.get("retries", 0) >= settings.max_verify_retries:
        return "persist"
    if not state.get("followup_queries"):
        return "persist"
    return "retry"


async def prepare_retry(state: ResearchState) -> dict[str, Any]:
    ctx = get_context(state["run_id"])
    await ctx.event("retry", lanes=state.get("focus_lanes"), queries=state.get("followup_queries"))
    return {
        "retries": state.get("retries", 0) + 1,
        "lane_results": [],
        "trace": [{"node": "prepare_retry", "focus": state.get("focus_lanes")}],
    }


async def persist(state: ResearchState) -> dict[str, Any]:
    ctx = get_context(state["run_id"])
    await ctx.event("node_start", node="persist")

    if ctx.repo is None or state.get("report") is None:
        return {"trace": [{"node": "persist", "skipped": True}]}

    if state.get("served_from_cache"):
        await ctx.repo.finish_cached_run(
            run_id=state["run_id"],
            app_name=state["app_name"],
            source_report_id=state.get("cached_report_id"),
            provider=ctx.provider.describe(),
            trace=state.get("trace") or [],
        )
        await ctx.event("persisted", cached=True)
        return {"trace": [{"node": "persist", "cached": True}]}

    usage = state.get("usage") or Usage()
    await ctx.repo.save_run(
        run_id=state["run_id"],
        app_name=state["app_name"],
        report=state["report"],
        verification=state.get("verification"),
        usage=usage.as_dict(),
        tool_stats=ctx.pool.stats(),
        provider=ctx.provider.describe(),
        trace=state.get("trace") or [],
        retries=state.get("retries", 0),
    )
    await ctx.event("persisted")
    return {"trace": [{"node": "persist", "ok": True}]}
