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
