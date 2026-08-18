from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel

from app.agent.llm import Usage
from app.agent.schemas import AppResearchReport, LaneFindings, ResolvedApp, VerificationResult


class LaneResult(BaseModel):
    lane: str
    label: str
    findings: LaneFindings
    tool_calls: int = 0
    error: str | None = None


def merge_usage(left: Usage | None, right: Usage | None) -> Usage:
    total = Usage()
    for part in (left, right):
        if part is not None:
            total.merge(part)
    return total


def merge_lane_results(
    left: list[LaneResult] | None, right: list[LaneResult] | None
) -> list[LaneResult]:
    merged: dict[str, LaneResult] = {r.lane: r for r in (left or [])}
    for result in right or []:
        merged[result.lane] = result
    return list(merged.values())


class ResearchState(TypedDict, total=False):
    run_id: str
    app_name: str
    force_refresh: bool

    resolved: ResolvedApp | None
    lane_results: Annotated[list[LaneResult], merge_lane_results]
    report: AppResearchReport | None
    verification: VerificationResult | None

    fabricated_urls: list[str]
    focus_lanes: list[str]
    followup_queries: list[str]
    retries: int

    usage: Annotated[Usage, merge_usage]
    served_from_cache: bool
    cached_report_id: int | None
    trace: Annotated[list[dict[str, Any]], operator.add]
    error: str | None
