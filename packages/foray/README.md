# Foray sourcing engine

A candidate search engine over US software engineers, built from public GitHub
data, with **precomputed archetype fit scores** so query latency is sub-second
instead of ~30s. Lives inside the Juicebox monorepo but is self-contained: its
own stack (uv, asyncpg, raw SQL migrations, Postgres 16 + pgvector, local
sentence-transformers embeddings, Haiku Batch scoring, FastAPI) and its own
`foray` database in the shared Postgres container.

> The critical property: **nothing in the query path calls an external API.**
> All expensive work happens offline.

```
GitHub Search API (sharded)   -> candidates            [Phase 1]
GitHub GraphQL (batched)      -> profile + repo facts  [Phase 2]
derive + local embeddings     -> signals + vectors     [Phase 3]
Haiku 4.5 Batch API           -> archetype_scores      [Phase 4]
FastAPI: SQL filter -> vector rerank -> ORDER BY score [Phase 5]
on-click: lazy LinkedIn + freshness refresh            [Phase 6]
tiered cron by field volatility                        [Phase 7]
```

## Setup

```bash
# from the repo root: start the shared pgvector Postgres (creates the foray db)
docker compose up -d db

# install deps + apply migrations
cd packages/foray
uv sync --extra dev
FORAY_DATABASE_URL=postgresql://foray:foray@localhost:5432/foray ./scripts/migrate.sh
```

Secrets live in the repo-root `.env` (see `.env.example`): `GITHUB_TOKEN`
(harvest/enrich), `OPENROUTER_API_KEY` (scoring + query parse via OpenRouter;
model slugs in `FORAY_SCORING_MODEL`/`FORAY_PARSE_MODEL`), `APOLLO_API_KEY`
(Phase 6 only), and `FORAY_DATABASE_URL`. Every command that needs a key fails
fast with a clear message if it is missing.

## Commands

```bash
uv run python -m foray.harvest.shards            # generate the shard grid
uv run python -m foray.harvest.search --limit N  # harvest logins
uv run python -m foray.enrich.profiles           # GraphQL profile+repo fetch
uv run python -m foray.enrich.derive             # recompute signals (no API calls)
uv run python -m foray.embed.run                 # local bge-small embeddings
uv run python -m foray.score.gate --archetype X --count
uv run python -m foray.score.run --archetype X --dry-run
uv run uvicorn foray.api.app:app --port 8100     # search API
```

## Schema

See `migrations/001_init.sql`. The one decision that is expensive to reverse:
archetype scores live in the normalized `archetype_scores` child table, not wide
columns on `candidates` (adding an archetype is an INSERT; `rubric_hash` enables
partial rescoring of stale rows).

## Tests

```bash
uv run pytest -q                  # unit tests always; DB tests skip without Postgres
uv run pytest -q -m "not model"   # CI shape: no Hugging Face download
```

CI starts `pgvector/pgvector:pg16`, applies `scripts/migrate.sh` to both `foray`
and `foray_staging`, and runs the DB-backed tests. Rehearsing a migration
against a restored production dump is a VM step — see `deploy/STAGING.md`.
