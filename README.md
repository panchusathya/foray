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

**3 · Retrieve** — `search/bm25.py`, `llm/embeddings.py`, `search/fusion.py`

BM25-Okapi over an in-process inverted index, plus cosine similarity over
embeddings. The two lists are combined with Reciprocal Rank Fusion, because
BM25 scores and cosine similarities live on incomparable scales and rank is the
only thing they share.

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
real delivery, `memory` for tests). Opens and clicks are tracked with
HMAC-signed URLs so the endpoints can't be walked. Replies are classified into
eight intents; anything except an out-of-office stops the sequence, bounces mark
the enrollment, and a referral outranks a decline because "not for me, but talk
to Sam" is a lead, not a rejection.

A daily send cap is enforced per org.

---

## Sourcing agents

A saved search bound to a project, run on a cadence. Each run re-executes the
search, skips anyone already in the pipeline, adds new matches above a score
threshold (carrying their match evidence onto the pipeline card), and can enroll
them into a sequence automatically.

```bash
juicebox run-agents      # every agent whose cadence has elapsed
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
| `DATABASE_URL` | SQLite in `data/` | Postgres: `postgresql+psycopg://…` |
| `ANTHROPIC_API_KEY` | *(empty)* | Enables LLM parsing, reranking, personalisation, reply classification |
| `EMBEDDING_PROVIDER` | `hashing` | `hashing` is offline and deterministic; `voyage` is hosted |
| `RERANK_TOP_K` | `20` | How many candidates get a model call per search; `0` disables |
| `EMAIL_PROVIDER` | `console` | `smtp` to actually send |
| `OUTREACH_DAILY_CAP` | `200` | Per-org send ceiling |

---

## Testing

```bash
make test        # 122 Python tests + 9 Node tests
make eval        # search quality metrics
make lint
```

Python tests run against an isolated SQLite file per test, with the model
disabled and a memory mailbox, so the suite is deterministic and needs no
network.

---

## Scale notes — read before production

Honest boundaries of what's here:

- **Retrieval is in-process.** The BM25 index and embedding matrix are built
  from the org's rows and cached until they change. That is fast and exact into
  the low hundreds of thousands of profiles on one node. Past that, move
  `search/index.py` behind pgvector or OpenSearch — the `CorpusIndex` interface
  is the seam, and nothing above it touches the internals.
- **The worker loop is a `sleep` loop.** Fine for a demo; use cron, Cloud
  Scheduler or Temporal for real send scheduling.
- **Email deliverability is not solved here.** SPF/DKIM/DMARC, warmup, domain
  rotation and suppression lists are operational work outside this codebase.
- **`hashing` embeddings are lexical-ish, not semantic.** They are deterministic
  and free, which is right for tests and demos. Switch to a real embedding model
  before judging semantic recall, then `juicebox reembed`.
- **Compliance is your call.** Sourcing and outreach are regulated differently
  across jurisdictions (GDPR, CAN-SPAM, CCPA, EU AI Act rules on automated
  candidate evaluation). The scoring layer is deliberately explainable so
  decisions can be audited, but the legal posture is yours.
