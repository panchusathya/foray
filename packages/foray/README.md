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
FastAPI: SQL filter -> bounded pool -> pure rank -> hydrate [Phase 5]
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

## Search ranking and pagination

`foray/rank.py` is the dependency-free ranking core. The API counts candidates
that pass the structured filters, retrieves bounded **vector** and **lexical**
arms (plus an optional legacy `score` arm), fuses them with RRF, then blends
RRF with the precomputed archetype score and exact cosine. Unscored rows are
retrievable; a scored row below `FORAY_DISPLAY_FLOOR` is still excluded.
Local MiniLM rerank (`FORAY_RERANK_ENABLED`) reorders the head of the fused
pool. The Python default is off so CI (no torch) does not load the model;
`docker-compose.prod.yml` turns it on together with `FORAY_UNSCORED_SCORE=20`
so unscored rows sit at the display floor and the CE ranks them. `paginable`
is the fused pool capped by `FORAY_MAX_PAGINABLE` (1000 by default).

`FORAY_RETRIEVAL_ARMS=score` restores the pre-hybrid "top-N by archetype score"
pool and is the eval/rollback lever. Flipping only one of rerank / unscored
is wrong: unscored=20 without CE is a precision cliff; CE without unscored=20
leaves the VLM/CUDA people at the bottom of the page.

Lexical documents live in `candidate_signals.search_tsv` (migration 006). Backfill
with `scripts/backfill_search_tsv.py` — it never touches `derived_at`. Until that
runs, the lexical arm matches nothing and search degrades to vector-only.

`POST /search` and `GET /search/stream` accept `limit` and `offset`; `limit: 0`
means "the server's own page size", which the response echoes back as
`page_size` so a client never has to hardcode the stride it pages in.
`total_matched` is the full filter count; `paginable` is the fused retrieval
pool after floor / min-evidence vetoes, capped by `FORAY_MAX_PAGINABLE`.
When no archetype matches, score is None for every row and the blend reduces
to RRF + cosine — there is no separate vector-fallback path.

## Tests

```bash
uv run pytest -q                  # unit tests always; DB tests skip without Postgres
uv run pytest -q -m "not model"   # CI shape: no Hugging Face download
```

CI starts `pgvector/pgvector:pg16`, applies `scripts/migrate.sh` to both `foray`
and `foray_staging`, and runs the DB-backed tests. Rehearsing a migration
against a restored production dump is a VM step — see `deploy/STAGING.md`.
