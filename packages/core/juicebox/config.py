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
    # Auto-drafting a reply into the user's Drafts folder when a candidate
    # answers. Off: the drafts were not good enough to be worth the noise, and
    # the feature is loud in two places at once -- it writes into a mailbox we
    # do not own and emails the owner to say so. Turning it on again should
    # wait until the draft quality is worth both.
    reply_drafting_enabled: bool = field(
        default_factory=lambda: _env_bool("REPLY_DRAFTING_ENABLED", False)
    )
    # Send-floor copied from Foray. 0 disables it (the default until the
    # measured reach_score distribution is known). Unscored addresses are
    # never blocked; only a stored score below the floor is.
    min_reach_score: int = field(default_factory=lambda: _env_int("FORAY_MIN_REACH_SCORE", 0))

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
    #
    # What somebody asked for outweighs the job they already have. The
    # heuristic term scores a candidate's *record* against what an employer
    # wants -- their last title, their tracked skills, their years -- and at
    # 0.70 against preference's 0.40 it meant a career changer was ranked
    # mostly on the thing they are trying to leave. An investor moving into
    # strategic finance scored a flat miss on `title` for exactly the reason
    # they texted us.
    #
    # The preference term is `None` for anyone who never stated a preference,
    # and `score_pair` drops absent terms out of the blend entirely -- so this
    # re-weighting moves nobody except the candidates who told us what they
    # want. That is the intended blast radius. Recruiter-facing lists sort on
    # the same `JobMatch.score`, so for those candidates a recruiter now sees
    # "said they want this, and can plausibly do it" ranked above "could do it,
    # never asked". For a candidate-facing product that is the right order.
    #
    # Preference does not carry a match alone: a perfect 0.70/1.30 = 0.538 sits
    # just under the 0.55 floor, so a stated title matching a posting title
    # with nothing else behind it is not a proposal. Some corroboration from
    # the record or the embedding is still required.
    job_match_heuristic_weight: float = field(
        default_factory=lambda: _env_float("JOB_MATCH_HEURISTIC_WEIGHT", 0.35)
    )
    job_match_vector_weight: float = field(
        default_factory=lambda: _env_float("JOB_MATCH_VECTOR_WEIGHT", 0.25)
    )
    job_match_preference_weight: float = field(
        default_factory=lambda: _env_float("JOB_MATCH_PREFERENCE_WEIGHT", 0.70)
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

    # --- voice intake ------------------------------------------------------
    voice_provider: str = field(default_factory=lambda: _env("VOICE_PROVIDER", "retell"))
    # Doubles as the webhook signing secret -- Retell signs with the API key, so
    # this must be the key carrying the webhook badge in their dashboard. A key
    # that works for API calls can still fail every verification.
    retell_api_key: str = field(default_factory=lambda: _env("RETELL_API_KEY", ""))
    # Replay window. Retell's own guidance is five minutes; their webhook retries
    # (3, over ~seconds) land well inside it, so this is not a retry allowance.
    retell_webhook_tolerance_seconds: int = field(
        default_factory=lambda: _env_int("RETELL_WEBHOOK_TOLERANCE_SECONDS", 300)
    )
    # The mid-call coverage check is conversational latency: the candidate is
    # sitting in silence while it runs. `json_fast` exists for exactly this.
    intake_coverage_fast_model: bool = field(
        default_factory=lambda: _env_bool("INTAKE_COVERAGE_FAST_MODEL", True)
    )
    # Stated minutes at or below this close the gate on CORE_KEYS only.
    # A larger budget does not add questions; no budget is today's full set.
    intake_core_only_minutes: int = field(
        default_factory=lambda: _env_int("INTAKE_CORE_ONLY_MINUTES", 4)
    )
    # Who the agent says it is, and the number it reads out. The other half of
    # this pair is RECRUITER_NAME / SMS_NUMBER_SPOKEN in deploy/retell_agents.py,
    # which cannot import this module (deploy/ is stdlib-only, by design). The
    # prompt and the dynamic variable have to agree or the agent greets as one
    # name and refers to itself by another, so
    # test_the_persona_matches_the_deploy_script keeps the copy honest.
    intake_recruiter_name: str = field(
        default_factory=lambda: _env("INTAKE_RECRUITER_NAME", "Vivian")
    )
    # Digit-grouped: TTS reads "+16283865454" as noise, and the whole point is
    # that the candidate recognises the sender.
    intake_sms_number_spoken: str = field(
        default_factory=lambda: _env(
            "INTAKE_SMS_NUMBER_SPOKEN", "six two eight, three eight six, five four five four"
        )
    )
    # Flips one spoken close sentence. The SMS sender itself lives on
    # feat/messaging-intake; this flag must not promise a text that cannot send.
    sms_enabled: bool = field(default_factory=lambda: _env_bool("SMS_ENABLED", False))
    sms_from_number: str = field(
        default_factory=lambda: _env("SMS_FROM_NUMBER", "+16283865454")
    )

    # --- messaging (Linq iMessage) -----------------------------------------
    linq_api_key: str = field(default_factory=lambda: _env("LINQ_API_KEY", ""))
    linq_webhook_secret: str = field(default_factory=lambda: _env("LINQ_WEBHOOK_SECRET", ""))
    linq_from_number: str = field(default_factory=lambda: _env("LINQ_FROM_NUMBER", ""))
    linq_enabled: bool = field(default_factory=lambda: _env_bool("LINQ_ENABLED", False))

    # --- messaging governance (CANDIDATE_AGENT_PLAN §3-§4) ------------------
    # An outbound message answering an inbound one this recent sends regardless
    # of the clock: a reply is not a solicitation.
    intake_reply_window_minutes: int = field(
        default_factory=lambda: _env_int("INTAKE_REPLY_WINDOW_MINUTES", 60)
    )
    # The unknown-timezone send window: 8am-9pm intersected across Eastern and
    # Pacific, expressed in Eastern local time. Config rather than constants
    # because texting Alaska or Hawaii would narrow it to 14:00-21:00.
    quiet_hours_unknown_start_hour: int = field(
        default_factory=lambda: _env_int("QUIET_HOURS_UNKNOWN_START_HOUR", 11)
    )
    quiet_hours_unknown_end_hour: int = field(
        default_factory=lambda: _env_int("QUIET_HOURS_UNKNOWN_END_HOUR", 21)
    )
    quiet_hours_unknown_zone: str = field(
        default_factory=lambda: _env("QUIET_HOURS_UNKNOWN_ZONE", "America/New_York")
    )
    # The shared-number circuit breaker: opt-outs per org per rolling 24h
    # before that org's *initiated* outbound stops. Opt-out rate is the number
    # carriers actually score, so it is the number to watch.
    messaging_optout_breaker_per_day: int = field(
        default_factory=lambda: _env_int("MESSAGING_OPTOUT_BREAKER_PER_DAY", 5)
    )

    # --- candidate documents (resumes, cover letters) -----------------------
    # Local filesystem behind the API: a single-VM compose stack needs no S3,
    # and CandidateDocument.storage_path keeps a later move to object storage
    # a data migration rather than a schema change. In containers this is the
    # `filedata` volume, mounted on api and worker both.
    file_storage_dir: str = field(
        default_factory=lambda: _env(
            "JUICEBOX_FILE_STORAGE_DIR", str(REPO_ROOT / "data" / "files")
        )
    )
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("JUICEBOX_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    )
    # Candidate-facing message budgets (CANDIDATE_AGENT_PLAN §4.3). These were
    # constants until a live test wanted a different cadence than a real
    # candidate should get: a budget you cannot change without a deploy is one
    # that ends up commented out instead, and a commented-out budget protects
    # nobody. Defaults are the plan's numbers, so an unset env behaves exactly
    # as before.
    proposals_per_week: int = field(
        default_factory=lambda: _env_int("PROPOSALS_PER_WEEK", 10)
    )
    max_proposals_per_message: int = field(
        default_factory=lambda: _env_int("MAX_PROPOSALS_PER_MESSAGE", 3)
    )
    proposal_messages_per_day: int = field(
        default_factory=lambda: _env_int("PROPOSAL_MESSAGES_PER_DAY", 1)
    )
    # The proposal bar's score floor (CANDIDATE_AGENT_PLAN §5): necessary,
    # never sufficient -- vetoes and the explainability gate sit on top.
    proposal_score_floor: float = field(
        default_factory=lambda: float(_env("PROPOSAL_SCORE_FLOOR", "0.55"))
    )
    # Orgs exempt from the global floor, comma-separated ids. The floor doubles
    # as the kill switch for candidate-facing proposals (raise it above 1.0 and
    # nothing clears the bar), which makes end-to-end testing impossible while
    # the LL144 / authority-to-apply review is open. This lets our own org run
    # at the normal bar without opening the shared number to real candidates.
    # Deliberately not a per-org *value*: one exempt list is auditable, a table
    # of per-tenant thresholds is a thing nobody can reason about later.
    proposal_floor_exempt_orgs: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in _env("PROPOSAL_FLOOR_EXEMPT_ORGS", "").split(",") if o.strip()
        )
    )
    # The bar every exempt org runs at instead.
    proposal_score_floor_exempt: float = field(
        default_factory=lambda: float(_env("PROPOSAL_SCORE_FLOOR_EXEMPT", "0.55"))
    )
    # Named candidates exempt from the floor, comma-separated ids.
    #
    # This, not the org list, is what makes end-to-end testing possible here:
    # the shared Linq number belongs to the same org as every real candidate,
    # so exempting the org would open proposals to anyone who texts it. An
    # explicit list of candidate ids opens it for exactly the people on it.
    proposal_floor_exempt_candidates: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip()
            for c in _env("PROPOSAL_FLOOR_EXEMPT_CANDIDATES", "").split(",")
            if c.strip()
        )
    )

    # --- LinkedIn profile ingestion (paid, worker-only) ----------------------
    # Which paid provider `get_profile_provider` returns: apify | scrapin.
    # The free browser scrape is tried first by `enrich.profile_sources` and
    # only reaches here when it fails -- which, with no LINKEDIN_SESSION_COOKIE
    # set, is every time.
    profile_provider: str = field(
        default_factory=lambda: _env("PROFILE_PROVIDER", "apify").lower()
    )
    apify_api_token: str = field(default_factory=lambda: _env("APIFY_API_TOKEN", ""))
    # Either a bare actor id ("LpVuK3Zozwuipa5bp") or owner~name; the API path
    # accepts both, and a "/" in an owner/name form is translated.
    apify_linkedin_actor: str = field(
        default_factory=lambda: _env("APIFY_LINKEDIN_ACTOR", "LpVuK3Zozwuipa5bp")
    )
    # This actor bills by mode, and the mode is a human-readable string with
    # the price in it. Kept configurable because switching to a cheaper or
    # richer mode should not need a deploy -- but note the default is the one
    # that omits emails, which we do not need: we already have the address,
    # and paying for contact data we hold is money for nothing.
    apify_profile_mode: str = field(
        default_factory=lambda: _env(
            "APIFY_PROFILE_MODE", "Profile details no email ($4 per 1k)"
        )
    )
    apify_base_url: str = field(
        default_factory=lambda: _env("APIFY_BASE_URL", "https://api.apify.com")
    )
    scrapin_api_key: str = field(default_factory=lambda: _env("SCRAPIN_API_KEY", ""))
    scrapin_api_base: str = field(
        default_factory=lambda: _env("SCRAPIN_API_BASE", "https://api.scrapin.io")
    )
    # A profile fetched (or found missing) within this window is not re-paid.
    profile_ingest_recheck_days: int = field(
        default_factory=lambda: _env_int("PROFILE_INGEST_RECHECK_DAYS", 30)
    )

    # --- candidate portal (apply.goforay.io) ---------------------------------
    # Where magic links point. The api and worker mint links, so THEY carry
    # this; the portal container itself never needs it.
    portal_base_url: str = field(
        default_factory=lambda: _env("PORTAL_BASE_URL", "http://localhost:5174")
    )
    # A texted link is short-lived and single-use; the session it exchanges
    # into lasts a week. The OTP sits between: ten minutes, five attempts.
    portal_link_ttl_seconds: int = field(
        default_factory=lambda: _env_int("PORTAL_LINK_TTL_SECONDS", 900)
    )
    portal_session_ttl_seconds: int = field(
        default_factory=lambda: _env_int("PORTAL_SESSION_TTL_SECONDS", 7 * 24 * 3600)
    )
    portal_otp_ttl_seconds: int = field(
        default_factory=lambda: _env_int("PORTAL_OTP_TTL_SECONDS", 600)
    )
    portal_otp_max_attempts: int = field(
        default_factory=lambda: _env_int("PORTAL_OTP_MAX_ATTEMPTS", 5)
    )
    portal_otp_hourly_cap: int = field(
        default_factory=lambda: _env_int("PORTAL_OTP_HOURLY_CAP", 3)
    )

    # Who candidate-facing portal mail comes from. Separate from the outreach
    # sender on purpose: outreach is a named recruiter writing to a candidate,
    # while a sign-in code is the system talking, and a personal mailbox is
    # the wrong return address for it. Empty falls back to the outreach
    # sender, so an unconfigured deployment keeps working.
    # Google sign-in for the portal. The real fix for OTP being undeliverable:
    # outbound SMTP is blocked at the network level on the prod VM, and a
    # sign-in that sends no email cannot be blocked by that. Empty disables the
    # button, so a deployment without Google configured is unchanged.
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID", ""))
    portal_from_email: str = field(default_factory=lambda: _env("PORTAL_FROM_EMAIL", ""))
    portal_from_name: str = field(default_factory=lambda: _env("PORTAL_FROM_NAME", "Foray"))

    # --- auto-apply (CANDIDATE_AGENT_PLAN Phase F) --------------------------
    # console | memory | live. The gate on the whole feature, and it defaults to
    # the one that cannot reach an employer: console exercises the status machine
    # and the attempt row without sending, memory is the test double, and `live`
    # is the only value that lets a vendor adapter run. A deployment that has not
    # chosen explicitly does not apply in anybody's name.
    apply_submitter: str = field(default_factory=lambda: _env("APPLY_SUBMITTER", "console").lower())
    # The mailbox that owns every candidate alias, sub-addressed per candidate
    # (anna+<token>@goforay.io). Empty means no alias is issued and the
    # candidate's own address goes on the application -- which is honest, but
    # every ATS that gates on email verification then stalls.
    apply_alias_address: str = field(default_factory=lambda: _env("APPLY_ALIAS_ADDRESS", ""))
    # Forward employer mail that arrives at an alias on to the candidate's real
    # inbox. Off means we hold it and the portal is the only place they see it.
    apply_alias_relay: bool = field(default_factory=lambda: _env_bool("APPLY_ALIAS_RELAY", True))
    # Attempts per application before it stops and waits for a person. A form
    # that has rejected us three times is not going to accept the fourth.
    apply_max_attempts: int = field(default_factory=lambda: _env_int("APPLY_MAX_ATTEMPTS", 3))
    # A visible browser is for debugging one run by hand, never for the worker.
    apply_browser_headless: bool = field(
        default_factory=lambda: _env_bool("APPLY_BROWSER_HEADLESS", True)
    )
    # Vendor API credentials. Greenhouse and SmartRecruiters need a per-board
    # key; Lever and Ashby post to the public form endpoint and need none.
    greenhouse_api_key: str = field(default_factory=lambda: _env("GREENHOUSE_API_KEY", ""))
    smartrecruiters_api_key: str = field(
        default_factory=lambda: _env("SMARTRECRUITERS_API_KEY", "")
    )

    # Logged-in LinkedIn session for the intake scraper. JSON cookie array
    # (Playwright storage_state) or a raw ``li_at=...`` string. Never commit.
    linkedin_session_cookie: str = field(
        default_factory=lambda: _env("LINKEDIN_SESSION_COOKIE", "")
    )
    linkedin_session_path: str = field(
        default_factory=lambda: _env("LINKEDIN_SESSION_PATH", "")
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
