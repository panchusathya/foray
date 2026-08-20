# AGENTS.md

## Cursor Cloud specific instructions

Juicebox is an offline-first AI recruiting stack. It runs end-to-end with **no**
`ANTHROPIC_API_KEY` (heuristic parser + deterministic scoring + hashing
embeddings). No secrets are required for development or testing.

### Services & how to run them

The update script has already created `.venv` (Python) and installed `packages/web`
node modules. Start services from the repo root:

| Service | Command | Port | Notes |
|---|---|---|---|
| API (FastAPI) | `.venv/bin/python -m juicebox.cli serve --reload` | 8000 | Docs at `/docs`, health at `/health`. |
| Web (React/Vite dev) | `cd packages/web && npm run dev` | 5173 | Proxies `/v1` + `/health` to the API (see `vite.config.js`), so the API must be running. |
| Worker | `.venv/bin/python -m juicebox.cli work --once` | — | Drains the job queue (outreach + sourcing agents). Loop: `juicebox work`. |
| Ingest webhook (Node) | `cd packages/ingest && npm run serve` | 8787 | Optional. Webhook-only; health at `/healthz` (`/` returns 404 by design). |

`packages/ingest` has **zero npm dependencies**, so it needs no install step.

The `Makefile` targets (`make api`, `make web`, `make test`, etc.) assume a
`python` binary on PATH. This VM only has `python3`, so either invoke the venv
binaries directly (as above) or run make with `make PY=python3 <target>`.

### Data / seeding

The demo workspace lives in a gitignored SQLite DB at `data/juicebox.db`. If it is
missing (fresh checkout), seed it before using the app:
`.venv/bin/python -m juicebox.cli seed`. Login: `demo@juicebox.dev` /
`juicebox-demo`. Seeding is data setup, not dependency setup, so it is kept out of
the startup update script.

### Lint / test / eval

- Lint: `.venv/bin/python -m ruff check packages/core eval`
- Python tests: `cd packages/core && ../../.venv/bin/python -m pytest -q` (isolated SQLite per test, model disabled — deterministic, no network).
- Node tests: `cd packages/ingest && npm test`
- Search-quality eval: `.venv/bin/python eval/run_eval.py --corpus-size 400`

Known pre-existing lint finding: `ruff` reports `EXE001` on `eval/run_eval.py`
(shebang present but the file is committed as mode `100644`). This is a repo issue,
not an environment issue — do not "fix" it via environment setup.

### Postgres (optional)

Default storage is SQLite; nothing extra is needed. To use Postgres, install the
extra (`.venv/bin/python -m pip install -e "packages/core[postgres]"`) and set
`DATABASE_URL`, or use `docker compose up --build`.

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
- **Stamp the negative cache on a miss, not just a hit.** `shortlist_events` has
  no uniqueness and is never read as an already-attempted check, so a
  `<provider>_checked_at` column is the only thing stopping a second click from
  paying twice.

## Mail providers (`packages/core/juicebox/integrations`)

- **Adding a provider** means implementing `MailProvider` in `providers.py` and
  registering it there: OAuth, sending, and reading raw mailbox traffic as
  `MailboxEvent`s. Reply matching, intent classification, enrollment stops, the
  thread-sync privacy guard and reply-draft generation are shared — a provider
  must never need to touch `sequencer.py`.
- **Never let a provider's wire format past its adapter.** Graph returns ISO
  timestamps, a nested `from.emailAddress.address` and a `conversationId`; Zoho
  returns millisecond epochs, a plain `fromAddress` and a `threadId`. Both
  become `MailboxEvent` in `to_event`, and nothing downstream knows which one
  it came from.
- **`Message.provider_native_id` is the ingest idempotency key.** Dedupe, the
  reply-draft lookup, the sent-items guard and a partial unique index all read
  it. A provider that cannot supply its own message id cannot be idempotent, so
  a send that comes back without one fails rather than reporting success.
- **Running out of send capacity is not an error.** `process_enrollment` returns
  `"deferred"`, which requeues the same permanent idempotency key. Raising
  parks the job; advancing `next_send_at` makes the requeue return `"skipped"`,
  which marks the job succeeded and loses that step for good.

## Foray sourcing engine (`packages/foray`)

Foray is a separate, self-contained package (its own `uv` project, `asyncpg`, raw
SQL migrations — no SQLAlchemy). It is a GitHub-sourced candidate search engine
with precomputed archetype scores; the query path calls no external API. Full
command reference is in `packages/foray/README.md`. Non-obvious points:

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
