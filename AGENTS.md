# AGENTS.md

Juicebox is an offline-first AI recruiting stack. It runs end-to-end with **no**
`ANTHROPIC_API_KEY` (heuristic parser, deterministic scoring, hashing
embeddings). No secrets are required for development or testing.

This file is deliberately short. It carries the rules that apply everywhere and
tells you which longer file to open for the subsystem you are about to touch.
**The detail lives next to the code** — see "Where the rules live" below.

---

## Orienting in an unfamiliar package

Four files answer most "how does this fit together" questions faster than
reading around:

- **`packages/core/juicebox/main.py`** — the router include list is the API's
  table of contents. Start here.
- **`packages/core/juicebox/deps.py`** — the whole auth and transaction model,
  including `CommittingRoute`, the custom `APIRoute` nearly every writing router
  uses to commit *before* the response goes out.
- **The `HANDLERS` dict at the bottom of `packages/core/juicebox/worker.py`** —
  every asynchronous thing the system does, in one place. Prod runs **two**
  worker processes: `worker` for everything, and `worker-intake` for only what a
  texter is waiting on. Priority inside one process was not enough — a live
  thread's "Hey" was answered 284 seconds later, at the exact second a batch of
  job parses drained. `worker-intake` deliberately does not run `ensure_ticks`,
  or every recurring sweep would fire twice.
- **The module docstring.** Nearly every module opens with one explaining why it
  exists separately, usually by naming the failure it prevents. `head -20` on a
  file beats reading it.

## The five packages

| Path | What it is |
|---|---|
| `packages/core` | Python. API + worker + CLI + all the domain logic. By far the largest thing here, and the only one with a shared database and an ORM. |
| `packages/foray` | A genuinely separate service: GitHub-derived sourcing search, its own database, raw `asyncpg`, no ORM. |
| `packages/web` | The recruiter SPA. React + Vite, plain JS/JSX, no TypeScript. |
| `packages/portal` | The candidate SPA. A separate container by design, with a proxy narrowed to `/v1/portal`. |
| `packages/ingest` | Node, **zero npm dependencies**. File/CSV imports, GitHub sourcing, inbound webhooks. Small and easy to forget exists. |

Plus `eval/` (labelled query set + scoring harness) and `data/seed/`.

## Where the rules live

Each of these is an `AGENTS.md` beside the code it governs. **Read the one for
the package you are changing** — they hold the non-obvious failure modes, each
usually stated as the mistake it prevents.

| Before you touch | Read |
|---|---|
| `packages/core/juicebox/intake/` — the field set, the coverage gate, extraction, caller identity | [`intake/AGENTS.md`](packages/core/juicebox/intake/AGENTS.md) |
| `packages/core/juicebox/messaging/` — text intake, cadence, consent copy, job cards in a thread | [`messaging/AGENTS.md`](packages/core/juicebox/messaging/AGENTS.md) |
| `packages/core/juicebox/applications/` — proposals, consent, submission adapters, the alias | [`applications/AGENTS.md`](packages/core/juicebox/applications/AGENTS.md) |
| `packages/core/juicebox/integrations/` — mail providers, reply sync | [`integrations/AGENTS.md`](packages/core/juicebox/integrations/AGENTS.md) |
| `packages/foray/` — the sourcing engine and its paid enrichment providers | [`foray/AGENTS.md`](packages/foray/AGENTS.md) |
| `packages/web/` — phone formatting, CSV import | [`web/AGENTS.md`](packages/web/AGENTS.md) |
| Anything a candidate hears on a call or reads in a thread | [`docs/INTAKE_COMPLIANCE.md`](docs/INTAKE_COMPLIANCE.md) — **binding**, and cited by section from code and tests |

Everything else — the design record, the runbooks, and what each document's
status is — is indexed in [`docs/README.md`](docs/README.md). The product-level
tour of how search, enrichment and outreach work is [`README.md`](README.md).

**When you learn something non-obvious, add it to the package file, not here.**
This file grew to 900 lines once by being the default destination.

---

## Running it

Start services from the repo root. The `Makefile` targets assume a `python` on
PATH; pass `make PY=python3 <target>` if yours is `python3` only.

| Service | Command | Port | Notes |
|---|---|---|---|
| API (FastAPI) | `.venv/bin/python -m juicebox.cli serve --reload` | 8000 | Docs at `/docs`, health at `/health`. `make api` |
| Recruiter web | `cd packages/web && npm run dev` | 5173 | Proxies `/v1` + `/health` to the API and `/foray/*` to :8100, so the API must be running. `make web` |
| Candidate portal | `cd packages/portal && npm run dev` | 5174 | Proxies only `/v1/portal`. |
| Worker | `.venv/bin/python -m juicebox.cli work --once` | — | Drains the job queue. Loop: `juicebox work`. `make worker` |
| Foray API | `cd packages/foray && uv run uvicorn foray.api.app:app --port 8100` | 8100 | Separate database; see its own `AGENTS.md`. |
| Ingest webhook (Node) | `cd packages/ingest && npm run serve` | 8787 | Optional. Webhook-only; health at `/healthz` (`/` returns 404 by design). No install step — zero dependencies. |

**Seeding.** The demo workspace lives in a gitignored SQLite DB at
`data/juicebox.db`. On a fresh checkout, seed before using the app:
`.venv/bin/python -m juicebox.cli seed` (`make seed`). Login:
`demo@juicebox.dev` / `juicebox-demo`. Seeding is data setup, not dependency
setup, so it is deliberately kept out of any startup update script.

**Postgres is optional.** Default storage is SQLite and nothing extra is needed.
For Postgres, install the extra (`.venv/bin/python -m pip install -e
"packages/core[postgres]"`) and set `DATABASE_URL`, or use
`docker compose up --build`.

### Lint / test / eval

- Lint: `.venv/bin/python -m ruff check packages/core eval` (`make lint`)
- Python tests: `cd packages/core && ../../.venv/bin/python -m pytest -q`
  (`make test-core`) — isolated SQLite per test, model disabled, deterministic,
  no network.
- Node (ingest): `cd packages/ingest && npm test` (`make test-ingest`)
- Foray: `cd packages/foray && uv run pytest -q` (`make test-foray`) — DB-backed
  tests skip if Postgres is unreachable.
- Search-quality eval: `.venv/bin/python eval/run_eval.py --corpus-size 400`
  (`make eval`)

`make test` is core + ingest only. The `web` and `portal` node tests (`npm test`
in each) are **not** in it, nor is `packages/web`'s Playwright suite (`npm run
test:e2e`) — run them yourself when you have changed either SPA. `packages/portal`
declares a `test:e2e` script but has no `e2e/` directory yet.

Known pre-existing lint finding: `ruff` reports `EXE001` on `eval/run_eval.py`
(shebang present, file committed as mode `100644`). That is a repo issue, not an
environment issue — do not "fix" it via environment setup.

### On the Cursor Cloud VM

The update script has already created `.venv` and installed `packages/web` node
modules, so skip the install step. That VM has `python3` but no `python`, so
either invoke the venv binaries directly as above or use `make PY=python3`.

---

## Repo-wide non-negotiables

These hold in every package. The package files assume them and do not repeat
them.

- **Offline-first.** Everything must work with no `ANTHROPIC_API_KEY`: a
  heuristic fallback for every model path, degrading with a warning, never
  failing. A heuristic path is allowed to be *deliberately incomplete* — see
  intake's — but it is load-bearing, not decoration.
- **`CommittingRoute` on any router that writes.** See `deps.py`.
- **An Alembic migration per model change.** `tests/test_migrations.py` fails on
  drift. One revision per change, and never stacked onto a mid-chain parent.
- **`new_id("prefix")` for ids, `record_event()` for the audit trail.**
- **Every query is scoped to the caller's org**, and there is a test asserting
  one workspace cannot read another's people.
- **Never resolve an org from a webhook payload.** The trusted sources are a row
  we already wrote for this call or thread, and the number that was dialled or
  texted. `routers/track.py` is the pattern: unauthenticated router, HMAC-signed
  token, org read off the server-side row. Metered spend behind an unsigned
  public endpoint is a billing DoS, not just a data leak.
- **No provider's wire format may escape its adapter.** Mail, voice, messaging
  and ATS vendors all normalise to one internal shape at the boundary, and
  nothing downstream knows which vendor it came from.
- **A vendor's own extraction never owns our record.** Every one of these
  platforms offers slot-filling; persisting their vocabulary puts data into a
  private shape `jobmatch` cannot score. Worth a second opinion, never the
  record.
- **Anything that spends money needs a principal.** Only Juicebox has org-scoped
  callers, so a paid route reachable without one is a bill payable by strangers.
  Foray's `/internal/` routes behind `FORAY_INTERNAL_TOKEN` are the shape to
  copy.
- **`ruff` stays clean**, and the Python suite runs against isolated SQLite with
  the model disabled.
