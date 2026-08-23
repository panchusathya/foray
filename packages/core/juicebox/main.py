"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import get_settings
from .db import create_all
from .llm.client import get_llm
from .llm.embeddings import get_embedder
from .migrate import is_up_to_date
from .routers import (
    agents,
    analytics,
    auth,
    candidates,
    crm,
    intake,
    integrations,
    job_discovery,
    job_matches,
    job_postings,
    org,
    outreach,
    projects,
    search,
    sourcing,
    track,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
logger = logging.getLogger("juicebox")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if settings.is_sqlite and settings.auto_create_schema:
        # Zero-setup dev/demo path: the local file may not exist yet.
        # Tests copy a session-built schema into each tmp db and set
        # JUICEBOX_AUTO_CREATE_SCHEMA=false so TestClient does not run
        # create_all a second time on every client fixture.
        create_all()
    elif not is_up_to_date():
        # Never mutate a real database implicitly — several workers boot at once
        # and `alembic upgrade head` is a deploy step, not a side effect.
        logger.warning(
            "database schema is not at the latest migration; run `juicebox migrate`"
        )
    llm = get_llm()
    logger.info(
        "juicebox %s up | db=%s | llm=%s | embeddings=%s",
        __version__,
        "postgres" if not settings.is_sqlite else "sqlite",
        "enabled" if llm.available else f"disabled, heuristics only ({llm.unavailable_reason})",
        get_embedder().name,
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Juicebox API",
        version=__version__,
        description=(
            "AI recruiting stack: natural-language people search with explainable "
            "matching, pipelines, and personalised outreach sequences."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins) or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(org.router)
    app.include_router(org.suppressions_router)
    app.include_router(candidates.router)
    app.include_router(candidates.notes_router)
    app.include_router(search.router)
    app.include_router(sourcing.router)
    app.include_router(projects.router)
    app.include_router(job_postings.router)
    app.include_router(job_matches.router)
    app.include_router(job_discovery.router)
    app.include_router(outreach.router)
    app.include_router(outreach.messages_router)
    app.include_router(outreach.reply_drafts_router)
    app.include_router(outreach.webhooks_router)
    app.include_router(agents.router)
    app.include_router(analytics.router)
    app.include_router(crm.router)
    app.include_router(integrations.router)
    app.include_router(intake.router)
    app.include_router(track.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        llm = get_llm()
        return {
            "status": "ok",
            "version": __version__,
            "llm": llm.available,
            "llm_reason": llm.unavailable_reason,
            "llm_usage": llm.usage(),
            "embeddings": get_embedder().name,
        }

    return app


app = create_app()
