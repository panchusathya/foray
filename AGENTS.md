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

## Voice intake (`packages/core/juicebox/intake`)

Candidate-side intake by phone: a web widget places a call, and a dial-in number
takes process updates. Retell is the agent platform, on a Twilio number
imported into it (+1 888 260-0142). Plan and phasing live in
`VOICE_INTAKE_PLAN.md`. Non-obvious points:

- **The gate is data, not an instruction.** `schema.coverage()` decides when a
  call may end and `schema.next_questions()` decides what to ask; both read the
  record. This is what makes a cooperative call two or three minutes and an
  evasive one longer. Do **not** move that judgement into the system prompt — an
  agent asked to decide whether it has "covered enough" guesses, and the failure
  is invisible because the transcript always looks like a complete conversation.
- **A declined field counts as covered.** A candidate who will not discuss comp
  has resolved it. If refusal left the field unanswered the gate could never
  close and the agent would ask forever, so `declined` is load-bearing, not
  bookkeeping. It is also why an empty field is not evidence nobody asked --
  read `declined` before concluding a call missed something.
- **A preference has to stay in `ParsedQuery`'s vocabulary.** `spec` is
  normalised by routing it through `ParsedQuery.normalized()`, the same call
  `jobmatch.spec` makes for a job description, so the two sides cannot drift.
  Adding a bespoke key to `spec` produces a field `jobmatch.engine` cannot score
  and quietly turns intake back into notes. `SPEC_FIELDS` is deliberately a
  subset: a candidate has no stated opinion about which school they attended.
- **`prompts._INTAKE_FIELD_KEYS` is a copy.** `intake` imports `llm.prompts`, so
  importing back would be a cycle. `test_the_prompt_enum_matches_the_field_set`
  is what keeps the copy honest -- if you add a field, that test tells you where
  the second edit goes.
- **The heuristic path is deliberately incomplete.** It never fills
  `must_have_skills` (a regex cannot tell "I want to keep writing Rust" from "I
  did some Rust once", and must-haves are what `search.scoring.assess` penalises
  hardest), never fills `highlights` (the value is the verbatim phrasing), and
  never sets `ok_to_share_with_clients` -- the one field where a wrong guess is a
  permission we did not have. Every heuristic extraction is flagged for review.
  Filling these in "since we already have the transcript" is the regression to
  watch for.
- **Negation is checked, not assumed.** "fully remote isn't what I want"
  contains "fully remote". `_asserted()` suppresses a phrase that appears only
  inside a negation, and conflicting signals record nothing rather than
  guessing. A pattern added without going through `_asserted` will read
  preferences backwards.
- **`candidate_phones` refuses to guess a country code.** The key comes from
  `intake.phones.lookup_key`, which is stricter than
  `enrich.normalize.clean_phones`: E.164 or North-American-shaped only, no row
  otherwise. A wrong email lookup finds nobody; a wrong *phone* lookup finds the
  wrong person and the dial-in agent reads their comp and pipeline to whoever
  dialled. Do not add a region default to widen coverage -- unknown caller costs
  one question, a wrong match is a disclosure. `repository.sync_candidate_phones`
  is the only writer; call it wherever `Candidate.phones` changes.
- **Caller ID authorises accepting an update, never disclosing one.** ANI is
  spoofable, so the dial-in agent must confirm something only the candidate knows
  before reading existing state back. Phase 4 owns this; the constraint is
  recorded here because it is easy to lose while making the call feel personal.
- **`IntakeCall.org_id` comes from the dialled number or a signed widget token,
  never from the webhook body.** The caller is unauthenticated -- there is no
  principal on a phone call -- and voice minutes are metered, so a
  payload-supplied org is both a cross-tenant write and somebody else's bill.
  `routers/track.py` is the pattern to copy: HMAC-signed token, org read off the
  server-side row.
- **`(provider, provider_call_id)` is the idempotency key.** Voice webhooks are
  always retried. Without it a retry extracts the same conversation twice.
- **Nullable booleans are deliberate.** `willing_to_relocate`, `remote_ok` and
  `ok_to_share_with_clients` have three states, because
  `Candidate.open_to_remote` cannot tell "wants an office" from "never asked" and
  that is precisely why filtering on it is untrustworthy.
- **Preferences live off `Candidate` on purpose.** `repository._apply` re-applies
  `manual_fields` over imported data so a nightly Foray refresh cannot overwrite
  a human correction. Preferences need that immunity, and widening
  `manual_fields` across twenty more columns makes an already subtle mechanism
  carry weight it was not built for.

### The webhook and the mid-call gate (phase 2)

- **A number must be registered before it can take a call.** `intake_numbers`
  maps a number to an org, and the webhook refuses anything it cannot resolve.
  `juicebox intake-number +1... --org <id>` is the only way in; there is no
  provider-side step that creates it. A 404 from `/v1/intake/webhook` almost
  always means this row is missing.
- **Org resolution has exactly two trusted sources**, in order: a row we already
  wrote for this call id (which covers retries and covers web calls, because we
  placed those), then the number that was dialled. Never the payload. There is a
  test that plants `org_id` in `metadata` and asserts it is inert -- keep it.
- **The signature is over the raw bytes plus the timestamp**, in that order,
  keyed on the Retell API key, hex. Verify before parsing, and never against a
  re-serialised body: `json.dumps` of the parsed payload changes whitespace and
  key order, and every signature stops matching. `test_re_serialising_the_body
  _breaks_verification` exists to catch exactly that regression.
- **No key configured means refuse everything.** Accepting unsigned bodies
  because nothing is set up turns a misconfiguration into an open write endpoint
  that also spends money on calls.
- **The handler verifies, records, queues, returns.** Retell times out at 10
  seconds and retries 3 times, so an extraction on the request path becomes four
  copies of the same conversation. `KIND_INTAKE_EXTRACT` is keyed on the call
  row, so three deliveries produce one job.
- **`(provider, provider_call_id)` is the idempotency key** and the unique
  constraint. `call_started`, `call_ended` and `call_analyzed` all land on one
  row, and each event fills only the fields it actually carries -- a later event
  must not blank what an earlier one supplied.
- **A transcript is never shortened.** A late `call_analyzed` sometimes carries a
  truncated one, and losing the conversation is unrecoverable.
- **An unmapped event type is acknowledged, not an error.** Retell emits
  transcript updates and a whole transfer lifecycle; a provider adding an event
  must never start failing this endpoint.
- **A confirmed preference is flagged, never overwritten.** Once `confirmed_at`
  is set a human has vouched for that record, and a re-run undoing their
  correction is how people stop trusting the system.
- **An unidentified caller still produces a record** -- on the `IntakeCall`, with
  `review_needed` -- but no `CandidatePreference`, and **no auto-created
  Candidate.** Creating people from cold inbound calls would fill the CRM with
  strangers. A human decides who that was.
- **`/v1/intake/coverage` is the gate, and it returns topics, not people.** It is
  reachable by anyone holding a valid signature, so it must never disclose a
  candidate's record. `still_needed` is required-tier only; once the gate closes
  it empties and enrichment moves to `optional_topics`. Reporting enrichment as
  "needed" would keep a finished call alive, which is the exact thing the tiering
  exists to prevent.
- **`wrap_up` is permission, not an instruction.** The agent may spend one more
  question on the highest-value enrichment topic (the projects story) if the
  candidate still sounds willing. It runs on `json_fast` because a candidate is
  sitting in silence while it answers, and it degrades to a usable answer rather
  than erroring -- an error here is heard as the agent going quiet.
