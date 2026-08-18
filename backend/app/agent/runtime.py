from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import EvidencePool, ToolProvider

logger = logging.getLogger(__name__)

Emitter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class RunContext:
    run_id: str
    provider: ToolProvider
    pool: EvidencePool
    repo: Any | None = None
    emit: Emitter | None = None
    _registry_owned: bool = field(default=False, repr=False)

    async def event(self, kind: str, **payload: Any) -> None:
        if self.emit is None:
            return
        try:
            await self.emit({"type": kind, "run_id": self.run_id, **payload})
        except Exception:
            logger.debug("Emitter for run %s failed", self.run_id, exc_info=True)


_REGISTRY: dict[str, RunContext] = {}


def register(ctx: RunContext) -> RunContext:
    _REGISTRY[ctx.run_id] = ctx
    return ctx


def get_context(run_id: str) -> RunContext:
    try:
        return _REGISTRY[run_id]
    except KeyError as exc:
        raise RuntimeError(
            f"No RunContext registered for run {run_id!r}. "
            "The graph must be invoked through app.agent.service.run_research()."
        ) from exc


def release(run_id: str) -> None:
    _REGISTRY.pop(run_id, None)
