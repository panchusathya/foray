SHELL := /bin/bash
PY    ?= python
VENV  := .venv
BIN   := $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
endif

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup ----

.PHONY: install
install: ## Create the venv and install every package
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e "packages/core[dev]"
	cd packages/web && npm install

.PHONY: migrate
migrate: ## Bring the database schema up to the latest migration
	$(BIN)/python -m juicebox.cli migrate

.PHONY: migration
migration: ## Autogenerate a migration from model changes (NAME="what changed")
	cd packages/core && ../../$(BIN)/python -m alembic revision --autogenerate -m "$(NAME)"

.PHONY: seed
seed: ## Create the demo workspace (org, project, sequence, agent, 120 people)
	$(BIN)/python -m juicebox.cli seed

.PHONY: export-seed
export-seed: ## Write the generated corpus to data/seed/candidates.jsonl
	$(BIN)/python -m juicebox.cli export-seed data/seed/candidates.jsonl --count 400

# ------------------------------------------------------------------ run ----

.PHONY: api
api: ## Run the API with autoreload on :8000
	$(BIN)/python -m juicebox.cli serve --reload

.PHONY: web
web: ## Run the React dev server on :5173
	cd packages/web && npm run dev

.PHONY: ingest-server
ingest-server: ## Run the inbound ingestion webhook on :8787
	cd packages/ingest && npm run serve

.PHONY: worker
worker: ## Drain due jobs from the queue (outreach + sourcing agents)
	$(BIN)/python -m juicebox.cli work --once

# ----------------------------------------------------------------- test ----

.PHONY: test
test: test-core test-ingest ## Run every test suite

.PHONY: test-core
test-core: ## Python tests
	cd packages/core && ../../$(BIN)/python -m pytest -q

.PHONY: test-foray
test-foray: ## Foray tests (DB-backed tests skip if Postgres is down)
	cd packages/foray && uv run pytest -q

.PHONY: test-ingest
test-ingest: ## Node tests
	cd packages/ingest && npm test

# Hits real provider APIs and costs money, so it is never part of `make test`.
# Each test skips itself unless its key is present.
.PHONY: test-live
test-live: ## Contract tests against the real Anthropic / Voyage APIs
	cd packages/core && ../../$(BIN)/python -m pytest -m live -v

.PHONY: eval
eval: ## Score juicebox search quality against the labelled query set
	$(BIN)/python eval/run_eval.py --corpus-size 400

.PHONY: eval-foray
eval-foray: ## Score Foray hybrid retrieval (needs Postgres + a derived corpus)
	cd packages/foray && uv run python eval/run_eval.py

.PHONY: lint
lint: ## Lint the Python package
	$(BIN)/python -m ruff check packages/core packages/foray eval

.PHONY: format
format: ## Autoformat the Python package
	$(BIN)/python -m ruff format packages/core packages/foray eval

.PHONY: linq-verify linq-setup
linq-verify: ## Check Linq API credentials and list phone numbers
	$(PY) deploy/linq_setup.py verify

linq-setup: ## Show Linq intake wiring status (credentials, numbers, webhooks)
	$(PY) deploy/linq_setup.py status

# ----------------------------------------------------------------- misc ----

.PHONY: build-web
build-web: ## Production build of the front end
	cd packages/web && npm run build

.PHONY: clean
clean: ## Remove the local database and build artefacts
	rm -f data/juicebox.db data/juicebox.db-wal data/juicebox.db-shm
	rm -rf packages/web/dist packages/core/.pytest_cache
