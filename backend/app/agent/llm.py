from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from app.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Tier(StrEnum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"


_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def merge(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.cost_usd += other.cost_usd
        for model, n in other.by_model.items():
            self.by_model[model] = self.by_model.get(model, 0) + n
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 6),
            "by_model": dict(self.by_model),
        }


def _price(model: str, usage: Usage) -> float:
    inp, out = _PRICES.get(model, (0.0, 0.0))
    uncached = usage.input_tokens
    return (
        uncached * inp
        + usage.cache_read_tokens * inp * _CACHE_READ_MULTIPLIER
        + usage.cache_write_tokens * inp * _CACHE_WRITE_MULTIPLIER
        + usage.output_tokens * out
    ) / 1_000_000


def usage_from_message(message: AIMessage, model: str) -> Usage:
    meta = getattr(message, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    usage = Usage(
        input_tokens=max(
            0,
            int(meta.get("input_tokens", 0))
            - int(details.get("cache_read", 0))
            - int(details.get("cache_creation", 0)),
        ),
        output_tokens=int(meta.get("output_tokens", 0)),
        cache_read_tokens=int(details.get("cache_read", 0)),
        cache_write_tokens=int(details.get("cache_creation", 0)),
        calls=1,
        by_model={model: 1},
    )
    usage.cost_usd = _price(model, usage)
    return usage


@lru_cache
def model_name(tier: Tier) -> str:
    s = get_settings()
    return s.orchestrator_model if tier is Tier.ORCHESTRATOR else s.worker_model


@lru_cache
def get_chat_model(tier: Tier, max_tokens: int = 4096):
    from langchain_anthropic import ChatAnthropic

    s = get_settings()
    kwargs: dict[str, Any] = {
        "model": model_name(tier),
        "max_tokens": max_tokens,
        "api_key": s.anthropic_api_key or None,
        "timeout": 120,
        "max_retries": 3,
    }
    if tier is Tier.ORCHESTRATOR:
        kwargs["model_kwargs"] = {"output_config": {"effort": s.orchestrator_effort}}
    return ChatAnthropic(**kwargs)


def cached_system(*blocks: str) -> SystemMessage:
    content: list[dict[str, Any]] = [{"type": "text", "text": b} for b in blocks if b]
    if content:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    return SystemMessage(content=content)


def _truncate_at(payload: Any, loc: tuple[Any, ...], limit: int) -> bool:
    if not loc:
        return False
    parent: Any = payload
    for step in loc[:-1]:
        try:
            parent = parent[step]
        except (KeyError, IndexError, TypeError):
            return False
    key = loc[-1]
    try:
        value = parent[key]
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(value, str) or len(value) <= limit:
        return False
    parent[key] = value[: max(0, limit - 1)].rstrip() + "…"
    return True


def _salvage(schema: type[T], raw: AIMessage | None) -> T | None:
    from pydantic import ValidationError

    calls = getattr(raw, "tool_calls", None) or []
    payload = copy.deepcopy(calls[0].get("args")) if calls else None
    if not isinstance(payload, dict):
        return None

    for _ in range(6):
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            repaired = False
            for error in exc.errors():
                if error.get("type") != "string_too_long":
                    return None
                limit = (error.get("ctx") or {}).get("max_length")
                if isinstance(limit, int) and _truncate_at(payload, error.get("loc", ()), limit):
                    repaired = True
            if not repaired:
                return None
    return None


async def structured_call(
    tier: Tier,
    schema: type[T],
    system: SystemMessage,
    human: str,
    *,
    max_tokens: int = 4096,
) -> tuple[T | None, Usage]:
    llm = get_chat_model(tier, max_tokens)
    runnable = llm.with_structured_output(schema, include_raw=True)
    result = await runnable.ainvoke([system, HumanMessage(content=human)])

    raw: AIMessage | None = result.get("raw") if isinstance(result, dict) else None
    parsed = result.get("parsed") if isinstance(result, dict) else result
    usage = usage_from_message(raw, model_name(tier)) if raw is not None else Usage()

    if parsed is None:
        error = result.get("parsing_error") if isinstance(result, dict) else None
        parsed = _salvage(schema, raw)
        if parsed is not None:
            logger.warning(
                "Structured call to %s for %s breached a length cap; trimmed and recovered.",
                model_name(tier),
                schema.__name__,
            )
        else:
            logger.warning(
                "Structured call to %s for %s produced no object: %s",
                model_name(tier),
                schema.__name__,
                str(error)[:600] or "no parsing_error reported",
            )
    return parsed, usage


async def tool_call_turn(
    tier: Tier,
    tools: list[Any],
    messages: list[BaseMessage],
    *,
    max_tokens: int = 2048,
    tool_choice: str | None = None,
) -> tuple[AIMessage, Usage]:
    bind_kwargs: dict[str, Any] = {}
    if tool_choice:
        bind_kwargs["tool_choice"] = tool_choice
    llm = get_chat_model(tier, max_tokens).bind_tools(tools, **bind_kwargs)
    response: AIMessage = await llm.ainvoke(messages)
    return response, usage_from_message(response, model_name(tier))


async def gather_usage(coros: list[Any]) -> list[Any]:
    return await asyncio.gather(*coros, return_exceptions=True)
