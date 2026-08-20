"""Configuration, loaded from the repo-root ``.env`` with pydantic-settings.

Foray shares the Juicebox monorepo's ``.env`` but namespaces its own database
URL as ``FORAY_DATABASE_URL`` so it never clobbers Juicebox's ``DATABASE_URL``.
Secrets (GitHub / OpenRouter / Apollo) are optional at load time so that
infrastructure commands (migrate, derive) import cleanly; each command that
needs a key calls ``settings.require(...)`` and fails fast with a clear error.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # packages/foray
REPO_ROOT = PACKAGE_ROOT.parent.parent  # repo root
DATA_DIR = PACKAGE_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", PACKAGE_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- storage (namespaced to avoid colliding with Juicebox's DATABASE_URL) ---
    database_url: str = Field(
        default="postgresql://foray:foray@localhost:5432/foray",
        validation_alias="FORAY_DATABASE_URL",
    )

    # --- secrets (optional at load; required per-command via require()) ---
    github_token: str = Field(default="", validation_alias="GITHUB_TOKEN")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    apollo_api_key: str = Field(default="", validation_alias="APOLLO_API_KEY")

    # --- models (any OpenRouter slug; defaults are cheap open-weight models) ---
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", validation_alias="OPENROUTER_BASE_URL"
    )
    scoring_model: str = Field(
        default="openai/gpt-oss-120b", validation_alias="FORAY_SCORING_MODEL"
    )
    parse_model: str = Field(
        default="openai/gpt-oss-120b", validation_alias="FORAY_PARSE_MODEL"
    )
    scoring_concurrency: int = Field(default=10, validation_alias="FORAY_SCORING_CONCURRENCY")
    scoring_usd_per_call: float = Field(
        default=0.0003, validation_alias="FORAY_SCORING_USD_PER_CALL"
    )

    # --- enrich payload (config-driven so it tunes without a rebuild) ---
    enrich_repos: int = Field(default=18, validation_alias="FORAY_ENRICH_REPOS")
    enrich_topics: int = Field(default=8, validation_alias="FORAY_ENRICH_TOPICS")
    enrich_languages: int = Field(default=4, validation_alias="FORAY_ENRICH_LANGUAGES")
    enrich_batch_size: int = Field(default=16, validation_alias="FORAY_ENRICH_BATCH_SIZE")

    # --- nightly pipeline budgets ---
    nightly_shards: int = Field(default=350, validation_alias="FORAY_NIGHTLY_SHARDS")
    nightly_enrich: int = Field(default=12000, validation_alias="FORAY_NIGHTLY_ENRICH")
    nightly_score_calls: int = Field(default=10000, validation_alias="FORAY_NIGHTLY_SCORE_CALLS")
    nightly_budget_min: int = Field(default=450, validation_alias="FORAY_NIGHTLY_BUDGET_MIN")
    # Hard ceiling on LLM calls spent scoring LinkedIn-evidence rows in one
    # run. Ungated, 200k rows x 15 archetypes would be ~$900; this plus the
    # gates is the difference between that and ~$10.
    linkedin_score_cap: int = Field(
        default=30_000, validation_alias="FORAY_LINKEDIN_SCORE_CAP"
    )
    monthly_spend_cap_usd: float = Field(
        default=40.0, validation_alias="FORAY_MONTHLY_SPEND_CAP_USD"
    )
    embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5", validation_alias="FORAY_EMBEDDING_MODEL"
    )
    embedding_dim: int = Field(default=384, validation_alias="FORAY_EMBEDDING_DIM")

    # --- retrieval blend (Phase 5) ---
    score_weight: float = Field(default=0.7, validation_alias="FORAY_SCORE_WEIGHT")
    vector_weight: float = Field(default=0.3, validation_alias="FORAY_VECTOR_WEIGHT")
    rerank_pool: int = Field(default=300, validation_alias="FORAY_RERANK_POOL")
    result_limit: int = Field(default=40, validation_alias="FORAY_RESULT_LIMIT")
    # Maximum number of ranked candidates exposed through offset pagination.
    # The retrieval pool grows to at least this size before the vector blend.
    max_paginable: int = Field(
        default=1000, ge=1, le=5000, validation_alias="FORAY_MAX_PAGINABLE"
    )
    display_floor: int = Field(default=20, validation_alias="FORAY_DISPLAY_FLOOR")
    activity_floor_months: int = Field(default=24, validation_alias="FORAY_ACTIVITY_FLOOR_MONTHS")
    min_results_before_widen: int = Field(default=12, validation_alias="FORAY_MIN_RESULTS")

    # --- harvest tuning ---
    github_rpm: int = Field(default=28, validation_alias="FORAY_GITHUB_RPM")
    # Core REST quota (events/public etc.) is 5,000/hr — a separate bucket from
    # the Search API's 30/min that github_rpm governs. 70 rpm = 4,200/hr,
    # leaving headroom for freshness checks.
    github_rest_rpm: int = Field(default=70, validation_alias="FORAY_GITHUB_REST_RPM")

    # --- contact enrichment ---
    # A LinkedIn miss is retried after this many days (negative cache);
    # allow_apollo and force bypass it.
    linkedin_recheck_days: int = Field(default=90, validation_alias="FORAY_LINKEDIN_RECHECK_DAYS")
    # Outbound politeness cap for the bulk waterfall (raw.githubusercontent +
    # personal websites), independent of concurrency.
    linkedin_rpm: int = Field(default=120, validation_alias="FORAY_LINKEDIN_RPM")

    # --- scoring spend guard ---
    spend_confirm_threshold_usd: float = Field(
        default=25.0, validation_alias="FORAY_SPEND_CONFIRM_THRESHOLD_USD"
    )

    @property
    def data_dir(self) -> Path:
        return DATA_DIR

    def require(self, *names: str) -> None:
        """Fail fast if any named secret/config is missing. Never degrade silently."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            pretty = ", ".join(sorted({_ENV_NAMES.get(n, n.upper()) for n in missing}))
            raise SystemExit(
                f"Foray: missing required configuration: {pretty}. "
                f"Set it in {REPO_ROOT / '.env'} (see .env.example)."
            )


_ENV_NAMES = {
    "github_token": "GITHUB_TOKEN",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "apollo_api_key": "APOLLO_API_KEY",
    "database_url": "FORAY_DATABASE_URL",
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
