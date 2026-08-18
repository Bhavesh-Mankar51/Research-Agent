from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.graph import build_graph
from app.agent.service import ResearchService
from app.api.routes import router
from app.config import get_settings
from app.db.repository import ResearchRepository
from app.db.session import init_models

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stack = AsyncExitStack()
    app.state.db_ready = False
    app.state.checkpointer_ready = False
    repo: ResearchRepository | None = None
    checkpointer = None

    try:
        await init_models()
        repo = ResearchRepository()
        app.state.db_ready = True
    except Exception:
        logger.exception("Database unavailable; running without persistence")

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.dsn)
        )
        await checkpointer.setup()
        app.state.checkpointer_ready = True
    except Exception:
        logger.exception("Checkpointer unavailable; runs will not be resumable")
        checkpointer = None

    app.state.repo = repo
    app.state.service = ResearchService(repo=repo, graph=build_graph(checkpointer))
    logger.info(
        "Ready. orchestrator=%s worker=%s", settings.orchestrator_model, settings.worker_model
    )
    try:
        yield
    finally:
        await stack.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="App Integration Research Agent",
        description=(
            "Researches a single app's integration surface: category, auth methods, "
            "self-serve vs gated access, API surface, MCP availability, and whether an "
            "agent toolkit could be built for it today."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
