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
from .routers import agents, analytics, auth, candidates, outreach, projects, search, track

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
logger = logging.getLogger("juicebox")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    create_all()
    logger.info(
        "juicebox %s up | db=%s | llm=%s | embeddings=%s",
        __version__,
        "postgres" if not settings.is_sqlite else "sqlite",
        "enabled" if get_llm().available else "disabled (heuristics only)",
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
    app.include_router(candidates.router)
    app.include_router(search.router)
    app.include_router(projects.router)
    app.include_router(outreach.router)
    app.include_router(outreach.messages_router)
    app.include_router(outreach.webhooks_router)
    app.include_router(agents.router)
    app.include_router(analytics.router)
    app.include_router(track.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "llm": get_llm().available,
            "embeddings": get_embedder().name,
        }

    return app


app = create_app()
