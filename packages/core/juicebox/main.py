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
    imports,
    intake,
    integrations,
    job_discovery,
    job_matches,
    job_postings,
    messaging,
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


def _voice_webhooks_ready() -> bool:
    """Can the intake webhook verify a signature at all?

    Asks the provider rather than reading the setting, so a provider whose
    verification does not need a key (the fake one in tests) reports ready and
    a future provider with different credentials needs no change here.
    """
    from .intake.providers import SignatureError, get_voice_provider

    try:
        provider = get_voice_provider()
        # An empty body with an obviously-bogus signature: a provider missing
        # its key raises before it ever looks at the signature, which is the
        # state worth reporting. Anything else means the key is present and the
        # request was simply not signed by it.
        provider.verify(raw_body=b"", signature="v=0,d=0")
    except SignatureError as exc:
        return "no Retell API key configured" not in str(exc)
    except Exception:  # noqa: BLE001 - a health check must never 500
        # Any other failure reads as not ready, which is the safe direction:
        # claiming the line works when we cannot tell is the one answer that
        # loses calls.
        return False
    return True


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
    app.include_router(imports.router)
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
    app.include_router(messaging.router)
    # Unauthenticated by design, like track: a candidate on a careers page
    # has no principal. Every request carries an HMAC we minted.
    app.include_router(messaging.chat_router)
    app.include_router(messaging.linq_router)

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
            # Reported for the same reason `llm` is: a subsystem that is
            # configured-off looks identical to one that is broken, and this
            # one fails silently. With no RETELL_API_KEY, `verify` refuses
            # every webhook -- correctly, since accepting unsigned bodies would
            # be worse -- so calls happen, cost money, and are never stored,
            # leaving one log line per delivery as the only trace. It cost a
            # day of lost calls once. `false` here means the phone line cannot
            # record anything.
            "voice_webhooks": _voice_webhooks_ready(),
        }

    return app


app = create_app()
