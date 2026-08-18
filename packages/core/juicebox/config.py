"""Runtime configuration, read once from the environment.

Everything has a working default so the stack boots with no .env at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

DEV_SECRET_KEY = "dev-secret-change-me-in-production"

# Values that must never sign tokens in production: the shipped default, the
# ones in .env.example and docker-compose.yml, and the obvious placeholders.
INSECURE_SECRET_KEYS = frozenset(
    {
        DEV_SECRET_KEY,
        "changeme",
        "change-me",
        "secret",
        "test-secret",
    }
)


class ConfigError(RuntimeError):
    """Configuration that is unsafe to run with."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- app -------------------------------------------------------------
    app_name: str = "Juicebox"
    env: str = field(default_factory=lambda: _env("JUICEBOX_ENV", "development"))
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", DEV_SECRET_KEY))
    access_token_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ACCESS_TOKEN_TTL_SECONDS", 60 * 60 * 12)
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in _env("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        )
    )

    # --- storage ---------------------------------------------------------
    # sqlite by default so `make dev` works with zero infrastructure.
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", f"sqlite:///{(REPO_ROOT / 'data' / 'juicebox.db').as_posix()}"
        )
    )

    # --- llm -------------------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("JUICEBOX_LLM_MODEL", "claude-opus-5"))
    llm_fast_model: str = field(
        default_factory=lambda: _env("JUICEBOX_LLM_FAST_MODEL", "claude-opus-5")
    )
    llm_enabled: bool = field(default_factory=lambda: _env_bool("JUICEBOX_LLM_ENABLED", True))
    llm_max_concurrency: int = field(default_factory=lambda: _env_int("LLM_MAX_CONCURRENCY", 8))
    llm_timeout_seconds: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT_SECONDS", 120.0))
    # Retries cover 429s and 5xx only; a refusal or bad JSON is not retried.
    llm_max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2))
    llm_retry_initial_delay: float = field(
        default_factory=lambda: _env_float("LLM_RETRY_INITIAL_DELAY", 0.5)
    )
    llm_retry_max_delay: float = field(
        default_factory=lambda: _env_float("LLM_RETRY_MAX_DELAY", 8.0)
    )

    # --- embeddings ------------------------------------------------------
    # "hashing" needs no network; "voyage" uses VOYAGE_API_KEY.
    embedding_provider: str = field(
        default_factory=lambda: _env("EMBEDDING_PROVIDER", "hashing").lower()
    )
    embedding_dim: int = field(default_factory=lambda: _env_int("EMBEDDING_DIM", 512))
    voyage_api_key: str = field(default_factory=lambda: _env("VOYAGE_API_KEY", ""))
    voyage_model: str = field(default_factory=lambda: _env("VOYAGE_MODEL", "voyage-3"))

    # --- retrieval tuning ------------------------------------------------
    retrieval_candidate_pool: int = field(
        default_factory=lambda: _env_int("RETRIEVAL_CANDIDATE_POOL", 200)
    )
    # auto = postgres when DATABASE_URL is postgres, else the in-process index.
    retrieval_backend: str = field(
        default_factory=lambda: _env("RETRIEVAL_BACKEND", "auto").lower()
    )
    rerank_top_k: int = field(default_factory=lambda: _env_int("RERANK_TOP_K", 20))
    rrf_k: int = field(default_factory=lambda: _env_int("RRF_K", 60))
    heuristic_weight: float = field(default_factory=lambda: _env_float("HEURISTIC_WEIGHT", 0.55))
    retrieval_weight: float = field(default_factory=lambda: _env_float("RETRIEVAL_WEIGHT", 0.20))
    rerank_weight: float = field(default_factory=lambda: _env_float("RERANK_WEIGHT", 0.25))

    # --- outreach --------------------------------------------------------
    # console | smtp | memory
    email_provider: str = field(default_factory=lambda: _env("EMAIL_PROVIDER", "console").lower())
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD", ""))
    smtp_starttls: bool = field(default_factory=lambda: _env_bool("SMTP_STARTTLS", True))
    outreach_from_email: str = field(
        default_factory=lambda: _env("OUTREACH_FROM_EMAIL", "recruiting@example.com")
    )
    outreach_from_name: str = field(
        default_factory=lambda: _env("OUTREACH_FROM_NAME", "Talent Team")
    )
    outreach_daily_cap: int = field(default_factory=lambda: _env_int("OUTREACH_DAILY_CAP", 200))
    outreach_postal_address: str = field(
        default_factory=lambda: _env("OUTREACH_POSTAL_ADDRESS", "")
    )
    # How often the worker enqueues the next per-org tick after one completes.
    worker_tick_seconds: int = field(default_factory=lambda: _env_int("WORKER_TICK_SECONDS", 60))
    worker_poll_seconds: float = field(
        default_factory=lambda: _env_float("WORKER_POLL_SECONDS", 2.0)
    )
    worker_batch_size: int = field(default_factory=lambda: _env_int("WORKER_BATCH_SIZE", 20))
    worker_lock_timeout_seconds: int = field(
        default_factory=lambda: _env_int("WORKER_LOCK_TIMEOUT_SECONDS", 15 * 60)
    )
    worker_max_attempts: int = field(default_factory=lambda: _env_int("WORKER_MAX_ATTEMPTS", 5))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def uses_postgres_retrieval(self) -> bool:
        if self.retrieval_backend == "memory":
            return False
        if self.retrieval_backend == "postgres":
            return True
        return not self.is_sqlite

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in {"production", "prod"}

    def validate(self) -> None:
        """Fail fast on settings that are only safe in development.

        SECRET_KEY signs auth tokens and the outreach tracking HMACs, so a known
        value means anyone can mint a token for any workspace. That is worth
        refusing to boot over, but only in production — every other environment
        keeps working with no .env at all.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if not self.secret_key or self.secret_key in INSECURE_SECRET_KEYS:
            problems.append(
                "SECRET_KEY is unset or still the development default; generate one with "
                '`python -c "import secrets; print(secrets.token_urlsafe(48))"`'
            )
        elif len(self.secret_key) < 32:
            problems.append(
                f"SECRET_KEY is only {len(self.secret_key)} characters; use at least 32"
            )

        if problems:
            raise ConfigError(
                "refusing to start with JUICEBOX_ENV=production:\n  - "
                + "\n  - ".join(problems)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings


def reset_settings_cache() -> None:
    """Tests mutate os.environ then call this to pick up new values."""
    get_settings.cache_clear()
