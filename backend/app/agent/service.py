from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.agent import runtime
from app.agent.graph import build_graph
from app.agent.llm import Usage
from app.agent.runtime import RunContext
from app.agent.state import ResearchState
from app.agent.tools import ComposioToolProvider, EvidencePool, ToolProvider
from app.db.repository import ResearchRepository

logger = logging.getLogger(__name__)

_SENTINEL = object()


class RunBroker:
    def __init__(self, maxsize: int = 256) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._maxsize = maxsize

    def open(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues[run_id] = queue
        return queue

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        queue = self._queues.get(run_id)
        if queue is None:
            return
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        await queue.put(event)

    async def close(self, run_id: str) -> None:
        queue = self._queues.get(run_id)
        if queue is not None:
            await queue.put(_SENTINEL)

    def discard(self, run_id: str) -> None:
        self._queues.pop(run_id, None)

    async def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        queue = self._queues.get(run_id) or self.open(run_id)
        try:
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    return
                yield event
        finally:
            self.discard(run_id)


class ResearchService:
    def __init__(
        self,
        *,
        repo: ResearchRepository | None = None,
        graph: Any | None = None,
        provider: ToolProvider | None = None,
        broker: RunBroker | None = None,
    ) -> None:
        self.repo = repo
        self.graph = graph or build_graph()
        self.broker = broker or RunBroker()
        self._provider = provider
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def provider(self) -> ToolProvider:
        if self._provider is None:
            self._provider = ComposioToolProvider.from_settings()
        return self._provider

    async def execute(
        self, app_name: str, *, force_refresh: bool = False, run_id: str | None = None
    ) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex
        pool = EvidencePool()
        ctx = RunContext(
            run_id=run_id,
            provider=self.provider,
            pool=pool,
            repo=self.repo,
            emit=lambda event: self.broker.publish(run_id, event),
        )
        runtime.register(ctx)

        if self.repo is not None:
            await self.repo.start_run(run_id, app_name)

        initial: ResearchState = {
            "run_id": run_id,
            "app_name": app_name,
            "force_refresh": force_refresh,
            "retries": 0,
            "usage": Usage(),
        }
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 40}

        try:
            final = await self.graph.ainvoke(initial, config=config)
            await ctx.event("done")
            return _serialise(final, pool, self.provider)
        except Exception as exc:
            logger.exception("Run %s failed", run_id)
            await ctx.event("error", message=str(exc)[:400])
            if self.repo is not None:
                await self.repo.fail_run(run_id, str(exc))
            raise
        finally:
            await self.broker.close(run_id)
            runtime.release(run_id)

    def start_background(self, app_name: str, *, force_refresh: bool = False) -> str:
        run_id = uuid.uuid4().hex
        self.broker.open(run_id)

        async def _run() -> None:
            try:
                await self.execute(app_name, force_refresh=force_refresh, run_id=run_id)
            except Exception:
                pass
            finally:
                self._tasks.pop(run_id, None)
                asyncio.get_running_loop().call_later(300, self.broker.discard, run_id)

        self._tasks[run_id] = asyncio.create_task(_run())
        return run_id

    def stream(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        return self.broker.stream(run_id)


def _serialise(state: dict[str, Any], pool: EvidencePool, provider: ToolProvider) -> dict[str, Any]:
    report = state.get("report")
    verification = state.get("verification")
    usage = state.get("usage") or Usage()
    return {
        "run_id": state.get("run_id"),
        "app_name": state.get("app_name"),
        "served_from_cache": bool(state.get("served_from_cache")),
        "report": report.model_dump(mode="json") if report is not None else None,
        "verification": verification.model_dump(mode="json") if verification is not None else None,
        "fabricated_urls": state.get("fabricated_urls") or [],
        "retries": state.get("retries", 0),
        "usage": usage.as_dict(),
        "tool_stats": pool.stats(),
        "provider": provider.describe(),
        "trace": state.get("trace") or [],
        "error": state.get("error"),
    }
