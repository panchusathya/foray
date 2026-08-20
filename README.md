# Juicebox

An AI recruiting stack: describe who you want to hire in plain English, get a
ranked shortlist **with the evidence behind every score**, push people into a
pipeline, and run personalised outreach sequences that stop themselves when
someone replies.

```
                    ┌──────────────────────────────────────────────┐
  natural language  │  parse → filter → retrieve → score → rerank   │  ranked people
  ─────────────────▶│  (LLM)   (rules)  (BM25+vec) (rules)  (LLM)   │────────────────▶
                    └──────────────────────────────────────────────┘   + why they matched
```

Everything runs offline. Without an `ANTHROPIC_API_KEY` the deterministic
parser, scorer and templates still produce a working product — the model
improves quality, it is not a dependency for the thing to function.

The model client needs `anthropic>=0.78`: every request carries
`output_config.effort`, which older SDKs cannot express. An SDK below that floor
is reported as unavailable at startup rather than failing one call at a time.

---

## Quick start

```bash
make install          # venv + python package + web deps
make seed             # demo workspace: 120 people, a project, a sequence, an agent
make api              # http://localhost:8000  (docs at /docs)
make web              # http://localhost:5173  → demo@juicebox.dev / juicebox-demo
```

No Make? The equivalents:

```bash
python -m venv .venv && .venv/bin/pip install -e "packages/core[dev]"
.venv/bin/python -m juicebox.cli seed
.venv/bin/python -m juicebox.cli serve --reload
cd packages/web && npm install && npm run dev
```

Search straight from the terminal:

```bash
.venv/bin/python -m juicebox.cli search \
  "senior machine learning engineers in San Francisco with pytorch, 5+ years" --verbose
```

```
  1. Fatima Kaur   0.970  Senior Machine Learning Engineer @ Sardine
       + Title: machine learning engineer: Current title "Senior Machine Learning Engineer"
       + Seniority: senior: senior vs requested senior
       + Experience: 5+ years: 6 years of tracked experience
       + Location: san francisco: Based in San Francisco, CA, United States
       + Must have pytorch: Lists pytorch as a skill
```

With Docker:

```bash
cp .env.example .env
docker compose up --build         # api :8000, web :5173, ingest :8787, postgres :5432
```

---

## Repository layout

| Path | What it is |
|---|---|
| `packages/core` | Python. The engine and the HTTP API — enrichment, search, outreach, agents. |
| `packages/ingest` | Node (JS, zero dependencies). File/CSV imports, GitHub sourcing, inbound webhooks. |
| `packages/web` | React + Vite (plain JS/JSX, no TypeScript). Search, pipelines, sequences, agents, analytics. |
| `eval/` | Labelled query set + scoring harness for search quality. |
| `data/seed/` | Generated corpus, exportable to JSONL. |

---

## How search works

Five stages, each independently useful and each degradable.

**1 · Parse** — `search/parser.py`

The query becomes a `ParsedQuery`: titles, must/nice skills, seniority, years,
locations, industries, company stage and size, schools, keywords, exclusions,
plus freeform criteria a filter can't express. Claude does this with a strict
JSON schema; a regex + taxonomy parser is the fallback and is used whenever the
model is unavailable, returns nothing useful, or is switched off.

**2 · Filter** — `search/filters.py`

Only the constraints that should *remove* someone: excluded companies and
titles, negative keywords, and years-of-experience bounds (with tolerance,
because self-reported tenure is noisy). Everything else is scored, not filtered
— a silently dropped good profile costs a recruiter far more than a mediocre one
at rank 12.

**3 · Retrieve** — `search/index.py`, `search/postgres.py`, `search/fusion.py`

On Postgres, hybrid search is `tsvector` (GIN) plus pgvector cosine, fused with
Reciprocal Rank Fusion. SQLite (and `RETRIEVAL_BACKEND=memory`) keep BM25-Okapi
plus numpy cosine in process so `make test` needs no infrastructure. RRF is used
because lexical scores and cosine similarities live on incomparable scales;
rank is the only thing they share.

**4 · Score** — `search/scoring.py`

Every requirement becomes a `Criterion` with a status (`strong`/`partial`/
`miss`), a 0–1 score, and a **piece of evidence pulled from the profile**. The
weighted mean is the heuristic score; the criteria list is what the UI renders.
This runs with no model at all, which is what keeps the explanations honest —
the LLM adjusts the ordering, it does not invent the reasoning.

**5 · Rerank** — `search/rerank.py`

The top-K shortlist goes to Claude, one call per candidate, concurrently. It
returns per-requirement verdicts with quoted evidence, an overall score, and
concerns. It re-orders and enriches; it never changes who was eligible.

The final score blends heuristic, retrieval and rerank signals with weights from
`HEURISTIC_WEIGHT` / `RETRIEVAL_WEIGHT` / `RERANK_WEIGHT`.

### Search quality

`make eval` scores the engine against `eval/queries.yaml`. Ground truth is a
*rule* over candidate attributes rather than a frozen id list, so labels stay
correct as the corpus grows. Current numbers on a 400-profile corpus, heuristic
parser only (no model calls):

| parser accuracy | P@5 | P@10 | R@10 | MRR | nDCG@10 | median latency |
|---|---|---|---|---|---|---|
| 1.00 | 0.97 | 0.77 | 0.64 | 1.00 | 1.00 | 24 ms |

`--min-ndcg 0.8` makes it a CI gate.

---

## Enrichment

`enrich/` turns messy inbound profiles into one canonical shape, so search and
outreach never deal with source quirks:

- **Titles** → canonical form (`Sr. ML Engineer` → `machine learning engineer`),
  with families for "close enough" credit.
- **Seniority** → a 9-rung ladder inferred from the title when not stated, with
  signed distance so over-levelled and under-levelled score differently.
- **Skills** → alias collapsing (`k8s` → `kubernetes`), extraction from prose,
  and adjacency (PyTorch experience partially satisfies a TensorFlow ask).
- **Tenure** → union of employment spans, so concurrent roles aren't double
  counted.
- **Locations** → metro membership (Oakland matches a San Francisco search).
- **Emails** → pattern inference with a confidence per guess. A guess is never
  presented as verified.

The taxonomies are plain dictionaries in `enrich/taxonomy.py` on purpose:
recruiters need to argue with a match, and you can't argue with an embedding.

---

## Outreach

Sequences are ordered steps with per-step delays and templates. Before each
send, `outreach/personalize.py` builds a variable bag — deterministic fields
from the profile plus three written fragments (`opening_line`, `why_relevant`,
`subject_hint`) from Claude, grounded in specifics and falling back to
profile-derived templates when no model is configured.

```
{{ first_name }}  {{ current_company }}  {{ top_skill }}  {{ role_title }}
{{ opening_line }}  {{ why_relevant }}   {{ anything | with a fallback }}
```

The template language has no loops, conditionals or expressions — recruiters
paste into a form, so the renderer must not be able to execute anything.

Sending goes through a provider interface (`console` by default, `smtp` for
real delivery, `memory` for tests). Connect real mailboxes in Settings —
Outlook via Microsoft Graph (needs an Azure app registration) or Zoho Mail
(needs an api-console client) — and sequences send as those people rather than
from a shared address.

Mailboxes are a tenant-wide pool, and sequences rotate across it by default:
each mailbox carries its own daily cap over a rolling 24 hours, and the
least-utilised one with room sends next. That is how sourcing volume goes up
without leaning harder on a single sending reputation. A contact is pinned to
whichever mailbox emailed it first and never moves, because a follow-up from a
different address arrives as a second thread from a stranger. The "send from"
picker on the sequence can override the pool and pin one mailbox instead. When
every mailbox is at its cap the send **defers** — the job stays queued and
goes out when room appears — while a sequence pinned to a disconnected mailbox
fails loudly rather than silently falling back to another provider. Each
`(enrollment, step)` pair can produce at most one outbound `Message` row — a
second worker cannot insert a duplicate. Opens and clicks are tracked with
HMAC-signed URLs so the endpoints can't be walked. Outreach is 1:1 personal
email from the sender's own account, so messages carry no unsubscribe footer
or bulk-mail headers. Bounces and reply opt-outs ("please stop contacting me")
land on a per-org internal do-not-contact list and stop further mail.

Replies are detected automatically: the worker polls each connected inbox
(every `MAILBOX_SYNC_SECONDS`, one job per mailbox so a slow one cannot starve
the rest) and matches new email back to the outreach that provoked it — by
thread id first, then by the reply's `In-Reply-To` against the `Message-ID` we
stored on send, then by sender address — and skips everything else. Graph
walks a delta link; Zoho has no delta API or webhooks, so it is polled against
a per-folder timestamp high-water mark. The first pass over a folder stores a
baseline cursor so mailbox history is never read. Replies are classified into eight intents; anything
except an out-of-office stops the sequence, bounces mark the enrollment, and a
referral outranks a decline because "not for me, but talk to Sam" is a lead,
not a rejection.

When a human reply lands, a response draft is written onto the actual thread
and waits in that mailbox's own Drafts folder — the model writes it when
configured, a plain per-intent template otherwise — and the user gets a "your
draft is ready" email from their own mailbox. Opt-outs, bounces and
out-of-office replies never get a draft. Editing and sending stays in the
user's own mail client; the app never sends the reply itself.

Drafts improve with use: the same sync also watches the Sent folder, so when the
user sends (an edit of) a draft — or discards it and writes their own — the
final text lands on the `reply_drafts` row, and the most recent drafted→sent
pairs are fed back into the next draft's prompt as voice examples.

A daily send cap is enforced per org too, lifted automatically to whatever
the mailbox pool can carry so it stays a spend guard rather than a second,
lower deliverability limit.

Adding a third mail provider means implementing `MailProvider`
(`packages/core/juicebox/integrations/providers.py`) and registering it: OAuth,
sending, and reading raw mailbox traffic as `MailboxEvent`s. Reply matching,
intent classification, enrollment stops, the CRM privacy guard and reply-draft
generation are shared and must not be touched.

Background work lives in a `jobs` table. `juicebox work` claims due rows
(Postgres uses `SKIP LOCKED` so multiple workers don't double-send).
`juicebox run-outreach` / `run-agents` still exist as synchronous one-shots.

```bash
juicebox work            # loop
juicebox work --once     # drain and exit (`make worker`)
```

---

## Sourcing agents

A saved search bound to a project, run on a cadence. Each run re-executes the
search, skips anyone already in the pipeline, adds new matches above a score
threshold (carrying their match evidence onto the pipeline card), and can enroll
them into a sequence automatically.

```bash
juicebox work --once     # also runs due agents via the job queue
juicebox run-agents      # synchronous one-shot, same as before
```

---

## Ingestion

```bash
cd packages/ingest
export JUICEBOX_API_KEY=jbx_...           # POST /v1/auth/api-keys

node src/index.js file ../../data/seed/candidates.jsonl
node src/index.js file exports/ats.csv --source greenhouse-export
node src/index.js github 'language:python location:"San Francisco" followers:>200' --limit 25
node src/index.js serve                   # inbound webhook on :8787
```

Batched, retried with exponential backoff and jitter, bounded concurrency. 4xx
responses are never retried — a malformed batch stays malformed, and retrying
only delays the error an operator needs to see. The webhook verifies an HMAC
signature so third-party systems never hold a Juicebox API key.

---

## API

`http://localhost:8000/docs` for the full interactive reference.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/auth/signup` · `/login` | Create a workspace, get a token |
| `POST` | `/v1/auth/api-keys` | Mint a key for ingestion or webhooks |
| `POST` | `/v1/candidates/bulk` | Upsert people (dedupes on source + source_id) |
| `POST` | `/v1/search` | Natural-language search with evidence |
| `GET`  | `/v1/searches/{id}` | Replay a persisted search |
| `POST` | `/v1/projects/{id}/candidates` | Add to a pipeline, carrying match rationale |
| `POST` | `/v1/sequences` · `/{id}/enroll` · `/{id}/run` | Outreach |
| `GET`/`PATCH` | `/v1/org` | Workspace settings |
| `GET`/`POST`/`DELETE` | `/v1/suppressions` | Do-not-contact list |
| `GET`/`POST`/`DELETE` | `/v1/integrations/microsoft` | Connect an Outlook mailbox (OAuth) |
| `POST` | `/v1/integrations/microsoft/{id}/sync` | Check the inbox for replies now |
| `GET`  | `/v1/reply-drafts` | AI reply drafts waiting in Outlook |
| `POST` | `/v1/webhooks/inbound-email` | Reply intake and classification |
| `POST` | `/v1/agents/{id}/run` | Run a sourcing agent |
| `GET`  | `/v1/analytics/funnel` | Pipeline + outreach metrics |

Two auth modes: a bearer token for humans, `X-API-Key` for machines. Every
query is scoped to the caller's org — there is a test that asserts one workspace
cannot read another's people.

---

## Configuration

Every setting has a working default; see `.env.example`. The ones that matter
most:

| Variable | Default | Effect |
|---|---|---|
| `JUICEBOX_ENV` | `development` | `production` refuses to boot on a default/short `SECRET_KEY` |
| `DATABASE_URL` | SQLite in `data/` | Postgres: `postgresql+psycopg://…` |
| `ANTHROPIC_API_KEY` | *(empty)* | Enables LLM parsing, reranking, personalisation, reply classification |
| `EMBEDDING_PROVIDER` | `hashing` | `hashing` is offline and deterministic; `voyage` is hosted |
| `RETRIEVAL_BACKEND` | `auto` | `postgres` uses FTS+pgvector; `memory` keeps the RAM index |
| `RERANK_TOP_K` | `20` | How many candidates get a model call per search; `0` disables |
| `EMAIL_PROVIDER` | `console` | `smtp` to actually send |
| `OUTREACH_DAILY_CAP` | `200` | Per-org send ceiling |
| `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | *(empty)* | Azure app registration; enables Connect Outlook |
| `MS_TENANT_ID` | `organizations` | Entra tenant for the OAuth flow |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` | *(empty)* | Zoho api-console client; enables Connect Zoho Mail |
| `ZOHO_ACCOUNTS_DOMAIN` | `https://accounts.zoho.com` | Data centre the Zoho mailboxes live in |
| `MAILBOX_SYNC_SECONDS` | `120` | How often the worker checks inboxes for replies |

---

## Testing

```bash
make test        # Python (core) + Node tests
make test-foray  # Foray; DB tests skip if Postgres is down
make eval        # search quality metrics
make lint
make test-live   # contract tests against the real APIs; needs keys
```

Python tests run against an isolated SQLite file per test, with the model
disabled and a memory mailbox, so the suite is deterministic and needs no
network. Model-backed code is covered through `FakeTransport`, which scripts
responses, failures and retries without a key.

The same suite runs against PostgreSQL — worth doing before a deploy, since
SQLite will not catch a Postgres-only problem:

```bash
JUICEBOX_TEST_DATABASE_URL=postgresql+psycopg://juicebox:juicebox@localhost:5432/juicebox_test \
  make test-core
```

`make test-live` makes one real call per integration point to check the wire
contract — that the `output_config` request shape is accepted and decodes. Each
test skips itself unless its key is present, so it is never part of `make test`.

CI runs lint, the suite on both SQLite and Postgres (including `alembic check`
for schema drift), Foray's suite against pgvector (SQL, migrations, derive),
the eval gate, the Node tests, the web build, a lockfile freshness check,
compose-file isolation guards, and a smoke boot of the staging stack.

There is no automatic promotion onto the VM. After CI is green, an operator
rehearses risky changes against a restored dump via
[`deploy/STAGING.md`](deploy/STAGING.md) (`docker-compose.staging.yml`,
separate Postgres, `juicebox_staging` / `foray_staging`), then merges to
`main` and `git pull && compose up` on prod. Local Docker is optional; most
day-to-day work is still the host venv + SQLite.

## Migrations

The schema is managed by Alembic; `create_all` is only used for SQLite demos and
tests.

```bash
make migrate                      # alembic upgrade head
make migration NAME="add x to y"  # autogenerate after changing models
juicebox migrate <rev> --down     # downgrade
```

Booting the API against Postgres does **not** migrate implicitly — it warns if
the database is behind. `docker compose up` runs `juicebox migrate` first.
`tests/test_migrations.py` fails if the models drift from the migrations.

---

## Scale notes — read before production

Honest boundaries of what's here:

- **Retrieval is in Postgres when you use Postgres.** Hybrid search is `tsvector`
  (GIN) plus pgvector cosine, fused with RRF. The embedding column is unbounded
  so hashing (512) and Voyage (typically 1024) can share a schema; cosine is an
  exact scan until a later migration pins a dimension and adds HNSW. SQLite and
  `make test` keep the in-process BM25+numpy snapshot. Force either with
  `RETRIEVAL_BACKEND=memory|postgres`. Recreate the compose `pgdata` volume if
  you previously ran vanilla `postgres:16` — that image has no pgvector.
- **The worker is a durable job queue.** `juicebox work` claims `jobs` rows
  (outreach ticks/sends and agent ticks). Postgres uses `FOR UPDATE SKIP LOCKED`.
  SQLite is single-process. Failed jobs retry with backoff, then park.
- **Email deliverability is not solved here.** Sends are meant to come from the
  recruiter's own mailbox (Outlook via Microsoft Graph), which inherits that
  account's reputation. The internal do-not-contact list *is* in the product.
- **`hashing` embeddings are lexical-ish, not semantic.** They are deterministic
  and free, which is right for tests and demos. Switch to a real embedding model
  before judging semantic recall, then `juicebox reembed`.
- **Compliance is your call.** The productised floor is an internal
  do-not-contact list that is honoured on every send. Sourcing and outreach are
  still regulated differently across jurisdictions (GDPR, CCPA, EU AI Act).
  The scoring layer is deliberately explainable so decisions can be audited, but
  the legal posture is yours.
