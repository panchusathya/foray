# Working in `packages/foray/`

A separate, self-contained service: its own `uv` project, its own database, raw
`asyncpg` and numbered SQL migrations — **no SQLAlchemy, and not Juicebox's
`DATABASE_URL`**. Read the root `AGENTS.md` first for repo-wide conventions;
almost everything else in this repo is Juicebox's and does not apply here.

Full command reference: `packages/foray/README.md`.

## Foray sourcing engine (`packages/foray`)

A GitHub-sourced candidate search engine with precomputed archetype scores; the
query path calls no external API. Non-obvious points:

- **Own database, shared container.** Foray uses the `foray` database + role
  created by `packages/foray/docker/initdb/00-foray.sql` inside the shared
  `pgvector/pgvector:pg16` container. Start it with `docker compose up -d db`. It
  reads **`FORAY_DATABASE_URL`** (not Juicebox's `DATABASE_URL`), default
  `postgresql://foray:foray@localhost:5432/foray`. The init script only runs on a
  fresh `pgdata` volume; if the `foray` db is missing, `docker compose down -v`
  first.
- **Setup:** `cd packages/foray && uv sync --extra dev`, then
  `FORAY_DATABASE_URL=... ./scripts/migrate.sh` (idempotent numbered-SQL applier).
- **Pipeline is offline-first.** `harvest`/`enrich`/`freshness` need `GITHUB_TOKEN`;
  `score` (Batch API) and query `parse` use `ANTHROPIC_API_KEY`. All fail fast with
  a clear message when a required key is missing (never silent). Without keys, use
  `scripts/seed_synthetic.py` (fake GitHub-shaped profiles) + `score.run --offline`
  (explicit spend-free heuristic) to exercise derive -> embed -> score -> search
  end to end.
- **Search API:** `uv run uvicorn foray.api.app:app --port 8100` (standalone UI at
  `/`, `POST /search`, SSE `GET /search/stream`). Both endpoints accept `limit`
  and `offset`; `total_matched` means filters + display floor, while `paginable`
  is capped by `FORAY_MAX_PAGINABLE` (default 1000). Ranking is deterministic
  within the bounded archetype-score retrieval pool. The archetype **score is in
  the JSON payload but must never be rendered**; results are shown by rank +
  evidence only, and lists are never padded below the display floor.
- **Web integration:** the Juicebox React app (`packages/web`) has a **Sourcing**
  page (`src/pages/Sourcing.jsx`) that calls the Foray API via a Vite dev proxy
  (`/foray/*` -> `http://localhost:8100`, `FORAY_API_URL` to override). To use it,
  run the Juicebox API (:8000, for login), the web dev server (:5173), and the
  Foray API (:8100) together. The page must never render the score.
- **Tests:** `cd packages/foray && uv run pytest -q`. DB-backed tests auto-skip if
  Postgres is unreachable. CI always starts pgvector and runs those tests
  (plus `migrate.sh` against both `foray` and `foray_staging`). The embedding
  similarity test is marked `model` and is skipped in CI (no Hugging Face
  fetch); the foray Docker image still bakes the model on every push.
## Email enrichment providers (`packages/foray/foray/enrich`)

- **A paid email provider implements `EmailProvider` in `contacts.py`.** It returns
  a `ProviderResult` and never touches the database; `store_result` owns
  persistence, the confidence ladder and the employer-domain guard.
- **Never bolt one onto `resolve_linkedin`'s waterfall.** That waterfall's
  currency is `linkedin_url` and it stops at the first hit, so a provider
  returning addresses but no URL is recorded as a *miss* -- stamping
  `linkedin_checked_at` and reporting `found: false` after the credit was spent.
- **Every source string needs a place on the ladder** (the table in
  `emails.py`'s docstring) *and* in `candidate_emails`' check constraint.
  `store_result` raises if the two disagree, because the alternative is an
  opaque IntegrityError at write time.
- **Confidence is not send-preference.** It answers "is this really them". Which
  address outreach uses is decided by the ordering in `api/search.py`, which
  prefers personal over work regardless of confidence -- a recruiting pitch must
  not land in somebody's work inbox.
- **Anything on Foray that spends money goes under `/internal/` behind
  `FORAY_INTERNAL_TOKEN`, and is triggered through Juicebox.** `/foray/*` is
  proxied to the browser with no authentication anywhere in the chain, and
  Foray has no `org_id` to scope a request to. Only Juicebox has principals,
  so only Juicebox can decide whether the caller may run up a bill. Adding a
  paid route next to `/search` would make it payable by strangers.
- **Stamp the negative cache on a miss, not just a hit.** `shortlist_events` has
  no uniqueness and is never read as an already-attempted check, so a
  `<provider>_checked_at` column is the only thing stopping a second click from
  paying twice.
