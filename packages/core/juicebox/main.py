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
    applications,
    auth,
    candidates,
    connect,
    crm,
    documents,
    imports,
    intake,
    integrations,
    job_discovery,
    job_matches,
    job_postings,
    messaging,
    org,
    outreach,
    portal,
    portal_auth,
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

    origins = [o for o in settings.cors_origins if o]
    # `allow_origins=["*"]` together with `allow_credentials=True` is the one
    # combination that must never ship: it tells every origin on the internet
    # that it may send this API cookies and Authorization headers. An empty
    # CORS_ORIGINS used to fall straight into it, so the deployment that simply
    # forgot the variable got the most permissive setting of all. Fall back
    # closed instead -- credentials come off, and with no origins configured
    # nothing cross-origin is allowed at all. A visible CORS failure is a
    # five-minute fix; a silent cross-origin credential hole is not.
    allow_credentials = bool(origins) and "*" not in origins
    if not allow_credentials:
        logger.warning(
            "CORS_ORIGINS is %s: cross-origin credentials are disabled. "
            "Set CORS_ORIGINS to the exact front-end origins.",
            "unset" if not origins else "a wildcard",
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(org.router)
    app.include_router(org.suppressions_router)
    app.include_router(candidates.router)
    app.include_router(imports.router)
    app.include_router(candidates.notes_router)
    app.include_router(search.router)
    app.include_router(sourcing.router)
    app.include_router(projects.router)
    app.include_router(job_postings.router)
    app.include_router(job_matches.router)
    app.include_router(applications.router)
    app.include_router(documents.router)
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
    app.include_router(messaging.router)
    # Unauthenticated by design, like track: a candidate on a careers page
    # has no principal. Every request carries an HMAC we minted.
    app.include_router(messaging.chat_router)
    app.include_router(messaging.linq_router)
    app.include_router(messaging.upload_router)
    # Same trust model as the upload page: a texted, thread-bound HMAC link.
    # The OAuth callback inside is unauthenticated by necessity and carries
    # its own signed state.
    app.include_router(connect.router)
    app.include_router(messaging.jobcard_router)
    # The candidate portal: auth is public (link exchange, OTP), the rest is
    # candidate-principal only. The portal's own nginx exposes just this
    # prefix, so from apply.goforay.io nothing else here exists.
    app.include_router(portal_auth.router)
    app.include_router(portal.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        llm = get_llm()
        embedder = get_embedder()
        # A configured provider that is quietly answering from the hashing
        # fallback looks identical to a healthy one from the outside, and the
        # only symptom downstream is candidates being told nothing clears the
        # bar. Say it here instead.
        degraded = getattr(embedder, "degraded_reason", "")
        return {
            "status": "ok",
            "version": __version__,
            "llm": llm.available,
            "llm_reason": llm.unavailable_reason,
            "llm_usage": llm.usage(),
            "embeddings": embedder.name,
            "embeddings_degraded": bool(degraded),
            "embeddings_reason": degraded,
        }

    return app


app = create_app()
