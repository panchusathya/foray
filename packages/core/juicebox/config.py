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

# OWASP's 2023 PBKDF2-HMAC-SHA256 baseline. Tests drop this via
# JUICEBOX_PBKDF2_ROUNDS; verify_password reads the cost out of the stored
# hash, so mixed-cost hashes keep verifying. Production and staging refuse to
# boot below this so a leftover test env cannot weaken live passwords.
DEFAULT_PBKDF2_ROUNDS = 240_000

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
    # Local SQLite boots with no migrate step. Tests copy a prebuilt schema
    # into each tmp db and turn this off so TestClient lifespan is not a
    # second create_all().
    auto_create_schema: bool = field(
        default_factory=lambda: _env_bool("JUICEBOX_AUTO_CREATE_SCHEMA", True)
    )
    pbkdf2_rounds: int = field(
        default_factory=lambda: _env_int("JUICEBOX_PBKDF2_ROUNDS", DEFAULT_PBKDF2_ROUNDS)
    )

    # --- foray (the sourcing engine, a separate service) -----------------
    # The browser reaches Foray's free routes through the nginx /foray/ proxy.
    # This is the server-to-server path, used only for the routes that spend
    # money and therefore need a principal in front of them.
    foray_base_url: str = field(default_factory=lambda: _env("FORAY_BASE_URL", "http://localhost:8100"))
    foray_internal_token: str = field(default_factory=lambda: _env("FORAY_INTERNAL_TOKEN", ""))
    # A 100-candidate batch at 8-way concurrency is a few seconds; the ceiling
    # is here so a hung vendor cannot pin a request for minutes.
    foray_timeout_seconds: float = field(
        default_factory=lambda: _env_float("FORAY_TIMEOUT_SECONDS", 120.0)
    )

    # --- llm -------------------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY", ""))
    openrouter_base_url: str = field(
        default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    # auto: whichever key is present, Anthropic first so a developer's existing
    # key keeps working. Pin it explicitly in production so a stray key cannot
    # silently redirect spend to another provider.
    llm_provider: str = field(
        default_factory=lambda: _env("JUICEBOX_LLM_PROVIDER", "auto").lower()
    )
    openrouter_model: str = field(
        default_factory=lambda: _env("OPENROUTER_MODEL", "z-ai/glm-5.2")
    )
    # Deliberately empty by default: the per-provider default is chosen at
    # runtime, because hardcoding an Anthropic slug here would follow the
    # OpenRouter path and 404.
    llm_model_override: str = field(default_factory=lambda: _env("JUICEBOX_LLM_MODEL", ""))
    llm_fast_model_override: str = field(
        default_factory=lambda: _env("JUICEBOX_LLM_FAST_MODEL", "")
    )
    llm_enabled: bool = field(default_factory=lambda: _env_bool("JUICEBOX_LLM_ENABLED", True))
    llm_max_concurrency: int = field(default_factory=lambda: _env_int("LLM_MAX_CONCURRENCY", 8))
    llm_timeout_seconds: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT_SECONDS", 120.0))
    # Retries cover 429s and 5xx only; a refusal or bad JSON is not retried.
    llm_max_retries: int = field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2))
    # Fast-model calls answer a user who is waiting -- a preview, not a send. The
    # main 120s budget is right for drafting a whole sequence in a worker and
    # absurd for a request someone is watching, so those get their own ceiling
    # and no retries: degrading to the heuristic text beats a spinner.
    llm_fast_timeout_seconds: float = field(
        default_factory=lambda: _env_float("LLM_FAST_TIMEOUT_SECONDS", 20.0)
    )
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

    # --- integrations: Microsoft Graph (Outlook) ---------------------------
    # Azure app registration (single-tenant). Empty client id = not configured;
    # the app runs fine without it and the UI says so.
    ms_client_id: str = field(default_factory=lambda: _env("MS_CLIENT_ID", ""))
    ms_client_secret: str = field(default_factory=lambda: _env("MS_CLIENT_SECRET", ""))
    ms_tenant_id: str = field(default_factory=lambda: _env("MS_TENANT_ID", "organizations"))
    # Defaults to {BASE_URL}/v1/integrations/microsoft/callback; must match the
    # redirect URI registered on the Azure app.
    ms_redirect_uri: str = field(default_factory=lambda: _env("MS_REDIRECT_URI", ""))

    # --- integrations: Zoho Mail -------------------------------------------
    # A "server-based" OAuth client at api-console.zoho.com. Multi DC must be
    # enabled on it if any mailbox lives outside the accounts domain below; the
    # callback then tells us which data centre a given mailbox belongs to.
    zoho_client_id: str = field(default_factory=lambda: _env("ZOHO_CLIENT_ID", ""))
    zoho_client_secret: str = field(default_factory=lambda: _env("ZOHO_CLIENT_SECRET", ""))
    zoho_redirect_uri: str = field(default_factory=lambda: _env("ZOHO_REDIRECT_URI", ""))
    # Where the consent screen lives. Set this to the data centre the mailboxes
    # are actually in (accounts.zoho.eu, .in, .com.au, ...) so the first hop is
    # right; the callback confirms it.
    zoho_accounts_domain: str = field(
        default_factory=lambda: _env("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
    )

    # --- mailbox sync (any provider) ---------------------------------------
    # How often the worker polls each connected inbox for replies.
    mailbox_sync_seconds: int = field(
        default_factory=lambda: _env_int("MAILBOX_SYNC_SECONDS", 120)
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
    # How far back the one-off Outlook history import reaches (candidates only).
    mailbox_backfill_days: int = field(
        default_factory=lambda: _env_int("MAILBOX_BACKFILL_DAYS", 365)
    )
    # --- contact discovery -------------------------------------------------
    # Provider pages per job before it re-enqueues itself, so one very large
    # mailbox cannot monopolise a worker.
    discovery_pages_per_job: int = field(
        default_factory=lambda: _env_int("DISCOVERY_PAGES_PER_JOB", 5)
    )
    # Hard stop. Zoho has no server-side date filter, so a sweep there is
    # bounded only by how far back the caller is willing to page.
    discovery_max_pages: int = field(
        default_factory=lambda: _env_int("DISCOVERY_MAX_PAGES", 500)
    )

    # --- job matching ----------------------------------------------------
    job_match_heuristic_weight: float = field(
        default_factory=lambda: _env_float("JOB_MATCH_HEURISTIC_WEIGHT", 0.70)
    )
    job_match_vector_weight: float = field(
        default_factory=lambda: _env_float("JOB_MATCH_VECTOR_WEIGHT", 0.30)
    )
    job_match_min_score: float = field(
        default_factory=lambda: _env_float("JOB_MATCH_MIN_SCORE", 0.55)
    )
    job_posting_stale_days: int = field(
        default_factory=lambda: _env_int("JOB_POSTING_STALE_DAYS", 21)
    )
    job_rematch_not_now_days: int = field(
        default_factory=lambda: _env_int("JOB_REMATCH_NOT_NOW_DAYS", 90)
    )
    job_match_refresh_hours: int = field(
        default_factory=lambda: _env_int("JOB_MATCH_REFRESH_HOURS", 24)
    )
    exa_api_key: str = field(default_factory=lambda: _env("EXA_API_KEY", ""))
    job_scrape_max_auto_queries: int = field(
        default_factory=lambda: _env_int("JOB_SCRAPE_MAX_AUTO_QUERIES", 5)
    )

    @property
    def resolved_llm_provider(self) -> str:
        """``anthropic`` | ``openrouter`` | ``none``.

        Under ``auto`` a present Anthropic key wins, so an existing dev setup
        behaves exactly as before; production has no Anthropic key and lands on
        OpenRouter without needing a flag.
        """
        if self.llm_provider in {"anthropic", "openrouter", "none"}:
            return self.llm_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.openrouter_api_key:
            return "openrouter"
        return "none"

    @property
    def resolved_llm_model(self) -> str:
        if self.llm_model_override:
            return self.llm_model_override
        return (
            self.openrouter_model
            if self.resolved_llm_provider == "openrouter"
            else "claude-opus-5"
        )

    @property
    def resolved_llm_fast_model(self) -> str:
        if self.llm_fast_model_override:
            return self.llm_fast_model_override
        return self.resolved_llm_model

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

    @property
    def requires_secure_secret(self) -> bool:
        # Staging hosts a restored prod dump on the VM, so a known SECRET_KEY
        # is as bad as it is in production. Local `development` still boots
        # with the shipped default so `make api` needs no .env.
        return self.env.strip().lower() in {"production", "prod", "staging"}

    def validate(self) -> None:
        """Fail fast on settings that are only safe in development.

        SECRET_KEY signs auth tokens and the outreach tracking HMACs, so a known
        value means anyone can mint a token for any workspace. That is worth
        refusing to boot over in production and staging — every other
        environment keeps working with no .env at all.

        PBKDF2 rounds below 1 are never valid. Staging and production also
        refuse a cost below ``DEFAULT_PBKDF2_ROUNDS`` so ``JUICEBOX_PBKDF2_ROUNDS``
        cannot be left at the test value of 1000.
        """
        problems: list[str] = []
        if self.pbkdf2_rounds < 1:
            problems.append("JUICEBOX_PBKDF2_ROUNDS must be at least 1")

        if self.requires_secure_secret:
            if not self.secret_key or self.secret_key in INSECURE_SECRET_KEYS:
                problems.append(
                    "SECRET_KEY is unset or still the development default; generate one with "
                    '`python -c "import secrets; print(secrets.token_urlsafe(48))"`'
                )
            elif len(self.secret_key) < 32:
                problems.append(
                    f"SECRET_KEY is only {len(self.secret_key)} characters; use at least 32"
                )
            if self.pbkdf2_rounds < DEFAULT_PBKDF2_ROUNDS:
                problems.append(
                    f"JUICEBOX_PBKDF2_ROUNDS is {self.pbkdf2_rounds}; "
                    f"production and staging must use at least {DEFAULT_PBKDF2_ROUNDS}"
                )

        if problems:
            raise ConfigError(
                f"refusing to start with JUICEBOX_ENV={self.env}:\n  - "
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
