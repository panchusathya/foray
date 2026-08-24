# Voice Intake Agent — Build Plan (rev. 2, after repo recon)

Supersedes the pre-recon draft. Read this before writing code; read `AGENTS.md`
first for repo-wide conventions.

## Status

**Phase 0 (recon) complete.** Findings below changed five things in the original plan.

**Phase 1 complete.** Provider is **Retell**. Schema, models, migration and
extraction are in, with 69 tests passing and the full core suite green (808
passed). Non-obvious rules are recorded in `AGENTS.md` under "Voice intake".
Shipped:

- `juicebox/intake/schema.py` — the field set, tiered into a required gate and an
  enrichment tier, plus `coverage()` and `next_questions()`.
- `juicebox/intake/extract.py` — transcript to preference record, model path with
  a strict JSON schema and a deliberately incomplete heuristic fallback.
- `juicebox/intake/phones.py` — E.164 lookup keys that refuse to guess a country.
- `IntakeCall`, `CandidatePreference`, `CandidatePhone` + migration
  `c1a7f4e05b93` (backfills the phone projection).
- `repository.sync_candidate_phones`, wired wherever `Candidate.phones` changes.
- `tests/test_intake.py`, `tests/intake_fixtures.py`.

**Adaptive call length** (added after recon, at Tushar's request): the field set
is tiered, and `coverage()` computes a `required_complete` flag from the record.
A cooperative caller who has answered the gate can be released in two to three
minutes; a caller who has not keeps going. The decision is data, never the
model's sense of elapsed time. A *declined* field counts as covered, which is
what lets a caller who refuses to discuss comp still finish the call.

**Platform: Retell**, with a Twilio number imported into it. Synthflow was
evaluated and dropped — capable, but it would have owned the agent runtime too.
No Phase 1 code changed through either switch; see "Provider adapter".

**Phase 1.5 — number provisioned. ✅** `+1 (888) 260-0142` is live in the `Foray`
Retell workspace, showing `Provider: Twilio`. It is toll-free rather than the
local 415 originally chosen; see "It is toll-free, which is a trade" for the
consequence and the two add-ons that mitigate it.

**Superseded (2026-08-23): the live number is now the local `+1 (628) 386-5454`**,
bought on our own Twilio account and brought into Retell over the SIP trunk
(`twilio_provision.py` trunk/attach + `retell_agents.py import`). The toll-free
trade is unwound; the 888 stays detached until the 628 has taken a few real
calls, then gets released. Console steps live in `deploy/NUMBER_CHECKLIST.md`.

Outstanding on the number, none of which block Phase 2 code — now tracked, with
the Twilio trust add-ons and the whole SMS path, in `deploy/NUMBER_CHECKLIST.md`:
- Restrict allowed inbound/outbound countries to US (+CA) — abuse and spend control.
- Set the fallback number to a real recruiter, so concurrency overflow reaches a human.
- Buy Verified Phone Number and Branded Call; both have lead time.
- Leave both webhook fields blank until the Phase 2 routes exist.

**Compliance guardrails consolidated** in `INTAKE_COMPLIANCE.md`, and enforced
in the agent prompts and schema rather than only documented: salary history is
never asked (expectations only), work authorization is capped at the two
permitted questions, protected categories have no field to land in, and the
intake path carries no score, rank, or recommendation. TCPA SMS opt-in is now
asked at wrap-up and captured per call (`intake_calls.sms_opt_in`, migration
`e5f0a92c7d18`, latched like `consent_recording`) so lawful opt-ins accumulate
while texting itself waits on the entity and toll-free verification.

**Phase 2 complete.** 45 new tests, full suite 853 passed, ruff clean. Shipped:

- `intake/providers.py` — `VoiceProvider` protocol, `RetellProvider` (signature
  verification + event normalisation), `FakeVoiceProvider`, and `CallEvent` as
  the shape nothing downstream sees past.
- `routers/intake.py` — `POST /v1/intake/webhook` (verify → record → queue →
  return) and `POST /v1/intake/coverage` (the mid-call gate). Unauthenticated,
  signature-verified, following `routers/track.py`.
- `intake/service.py` — org and caller resolution, event recording, extraction,
  preference upsert, coverage snapshots. Shared by the router and the worker so
  neither owns domain logic.
- `IntakeNumber` + migration `d2b8c31f9e47` — the number-to-org map that makes
  org resolution possible without trusting the payload.
- `KIND_INTAKE_EXTRACT` + worker handler, idempotent on the call row.
- `juicebox intake-number` CLI to register a number.
- Retell settings in `config.py`; `RETELL_API_KEY` doubles as the webhook secret.

Exit criteria met: a replayed webhook produces an `IntakeCall` and a
`CandidatePreference` via the worker; forged, unsigned, stale and re-serialised
requests are all refused; duplicate deliveries are a no-op.

Next: **Phase 3** — author the two agents (inbound status-line, outbound intake),
register `check_coverage` as a custom function against them, and build the
website widget. Blocked on one thing: the two endpoints must be publicly
reachable before an agent can call them, so `app.goforay.io` needs to be serving
this branch (or a tunnel for development).

---

## What recon changed

1. **There is no external ATS to adapt to — Juicebox *is* the system of record.**
   `candidates`, `projects` / `project_candidates` (pipelines), `candidate_notes`,
   `events`, `crm_status`. The `ATSAdapter` interface in the original §6 is
   indirection with nothing on the other side of it. Write to the domain models
   through `repository.py`'s existing upsert path. The review queue survives; the
   adapter does not.

2. **The "back-solve from what the company wants to know" contract already exists
   in code, and the proposed schema conflicts with it.** `ParsedQuery`
   (`juicebox/search/parser.py:28`) is the canonical requirements vocabulary —
   20 taxonomy-normalised fields. `jobmatch/spec.py:job_spec()` compiles a
   `JobPosting` into one; `jobmatch/engine.py:score_pair()` scores a candidate
   against it and writes `JobMatch` rows with per-criterion evidence. The
   original §4 schema reinvents ~8 of those fields under different names
   (`role_types` vs `job_titles`, `company_stage` vs `company_stages`,
   `industries_preferred` vs `industries`). Intake data in a private vocabulary
   can never be scored against `job_postings` — it degrades to notes a human has
   to read. **Intake must emit `ParsedQuery`-shaped preferences** pushed through
   `enrich/taxonomy.py`, so "a Series B fintech, nothing in crypto" lands on the
   same tokens the JD parser emits.

3. **Intake fills a real, load-bearing hole in the data model.** `Candidate` is
   100% supply-side fact — title, skills, tenure, location, seniority. The only
   preference field on it is `open_to_remote`. There is no timeline, no target
   comp, no stage preference, no dealbreakers, no process state. So matching
   today runs one direction only: candidate-facts vs company-requirements. Intake
   unlocks the reverse — company-facts vs candidate-requirements — which is where
   most bad matches come from. This is the payoff phase, and the original plan
   did not have it.

4. **Auth is the hardest problem here, and `routers/track.py` is the precedent.**
   Every route is org-scoped through `get_principal` (bearer or `X-API-Key`). A
   candidate on the website or dialling a number has no principal. `track.py`
   solves exactly this shape: unauthenticated router, HMAC-signed opaque token
   (`security.py:signed_tracking_token` / `verify_tracking_token`), org read off
   the *server-side row* and never off the request. Voice minutes are metered
   spend, so an unsigned public endpoint is a billing-DoS, not just a data leak.
   `AGENTS.md` already warns about this class of mistake in the Foray notes.

5. **The recruiter web app is fully auth-gated and is not the marketing site.**
   `App.jsx:124` returns `<LoginPage/>` before any route renders. A
   candidate-facing review page needs public routes hoisted above that gate. The
   one-click widget belongs on the public marketing property as an embeddable
   snippet pointing at signed endpoints — not as a page in `packages/web`.

---

## Where things live

| Concern | Location |
|---|---|
| Requirements vocabulary | `juicebox/search/parser.py` (`ParsedQuery`) |
| Normalisers (titles, skills, seniority, locations, industries) | `juicebox/enrich/taxonomy.py` |
| Candidate ↔ posting scoring | `juicebox/jobmatch/engine.py:score_pair` |
| Per-criterion evidence | `juicebox/search/scoring.py:assess` |
| Unauthenticated + signed endpoints | `juicebox/routers/track.py`, `juicebox/security.py` |
| Org scoping / principals | `juicebox/deps.py` |
| Background work | `juicebox/jobs.py` (`KIND_*`, `enqueue`), `juicebox/worker.py` |
| LLM with strict JSON schema | `llm.json(system=, prompt=, schema=, effort=, max_tokens=)` |
| Prompt + schema conventions | `juicebox/llm/prompts.py` |
| Provider-adapter precedent | `juicebox/integrations/providers.py` (`MailProvider`) |
| Normalised contact table precedent | `CandidateEmail` (`models.py:398`) |

Non-negotiable conventions: offline-first (must work with no `ANTHROPIC_API_KEY`
— heuristic fallback for every LLM path, degrade with a warning, never fail);
`CommittingRoute` on any writing router; `new_id("prefix")` ids; `record_event()`
for audit; an Alembic migration per model change (`tests/test_migrations.py`
fails on drift); `ruff`; pytest against isolated SQLite with the model disabled.

---

## Schema decision: mirror `ParsedQuery`, extend only where it has no field

**Reuse — these map onto existing normalised fields. Do not invent new names.**

| Intake concept | `ParsedQuery` field | Normaliser |
|---|---|---|
| Roles they want | `job_titles` | `tx.normalize_title` |
| Level | `seniority` | `tx.normalize_seniority` (9-rung ladder) |
| Skills they want to use | `must_have_skills` / `nice_to_have_skills` | `tx.normalize_skill` |
| Industries wanted / ruled out | `industries` / `negative_keywords` | `tx.normalize_industry` |
| Company stage | `company_stages` | — |
| Company size | `company_sizes` | — |
| Dream companies | `target_companies` | — |
| Companies to avoid | `excluded_companies` | — |
| Remote / locations | `remote_ok`, `locations` | `tx.normalize_location` (metro membership) |
| Dealbreakers not expressible as a filter | `freeform_criteria` (verbatim) | — |

**Extend — genuinely new, no home in `ParsedQuery`.** These go on a new
`CandidatePreference` row: `employment_status`, `reason_for_looking`, `timeline`,
`urgency_driver`, `target_base`, `target_total`, `equity_importance`,
`comp_flexibility_notes`, `work_authorization`, `willing_to_relocate`,
`active_processes[]`, `offers_in_hand[]`, `confidential_items[]`,
`ok_to_share_with_clients`.

Rules carried forward from the original plan, still right: every field gets a
confidence; low confidence goes to a review queue rather than silently into the
record; `fields_missing` is first-class so a call that drops early still yields a
valid partial; verbatim quotes kept for subjective fields to prevent paraphrase
drift.

### Two tiers, and what they are for

Every field carries the **client question it answers** — that is the admission
price, not documentation. Intake exists to answer what a hiring company will
ask, so a field no client would ask about is a field that makes every call
longer for nothing.

*Required* (11 fields, the gate): roles, level, locations, remote-or-office,
core skills, employment status, timeline, comp expectation (base **or** total),
work authorisation, other live processes, permission to represent. These are
what a client asks about every candidate, so a call that has not covered them
has not done its job.

*Enrichment* (14 fields): the highlight/project story, company stage and size,
industries, secondary skills, reason for looking, companies ruled in or out,
dealbreakers, offers, relocation, equity weighting, urgency, confidentiality.
Asked only once the gate closes — which is what makes a cooperative call short.

A **declined** field counts as covered. A candidate who will not discuss comp has
resolved it; if refusal left it unanswered the gate could never close and the
agent would ask forever. So an empty field is not evidence nobody asked — read
`declined` first.

**Projects and standout work** land in `highlights` with the detail quoted
verbatim, plus the skills the story evidences. Kept as the candidate's own words
because that is the thing a resume does not have and a client actually responds
to. Note that `Candidate.summary` and `Candidate.skills` feed the retrieval
haystack (`search/index.py:142`), so wiring highlights into `doc_text` would make
these stories searchable — deliberately deferred to Phase 5 rather than
overwriting an imported `summary` here.

### New tables

- **`CandidatePreference`** — 1:1 with `Candidate`. Holds the `ParsedQuery`-shaped
  `spec` (JSON, same shape as `JobPosting.parsed_spec`) plus the extension fields
  above, `spec_version`, and per-field confidence. Its own table, not columns on
  `Candidate`: `repository._apply` re-applies `manual_fields` on top of imported
  data specifically so a nightly Foray refresh cannot clobber human edits, and
  intake-derived preferences need the same immunity without widening that
  mechanism.
- **`IntakeCall`** — one row per call: `entry_point` (web | dial_in), provider,
  provider call id (idempotency key — `Message.provider_native_id` is the
  precedent), duration, consent flag, transcript, recording URL, raw + extracted
  payloads, `status`, `review_needed`.
- **`CandidatePhone`** — normalised, indexed. `Candidate.phones` is a JSON list,
  which cannot serve an inbound caller-ID lookup on either backend. `CandidateEmail`
  is the shape to copy.

---

## Platform and telephony

**Retell** is the agent runtime. The number is a **Twilio** number imported into
Retell, so Retell shows it as `Provider: Twilio`. Synthflow was evaluated and
dropped; its capabilities were fine, but it is not a CPaaS and would have owned
the agent runtime as well as the number.

**Provisioned:** `+1 (888) 260-0142`, in the `Foray` Retell workspace.

**One shared number**, which Retell's model supports directly: one inbound agent
and one outbound agent per number. That maps onto the two conversations exactly —

| Direction | Agent | Conversation |
|---|---|---|
| Inbound | status-line agent | Diff mode against an existing record; unknown callers fall through to full intake |
| Outbound | intake agent | Full first intake, triggered by the website "call me" widget |

A candidate who misses the outbound call, or wants to update us later, redials
the number in their call history and lands on the inbound agent.

### It is toll-free, which is a trade

`888` is toll-free, not the local 415 originally chosen. That is *better* for a
published inbound status line and *worse* for the outbound "call me" flow, where
toll-free reads as a call centre and answer rates suffer. Two mitigations, both
Retell add-ons and both worth buying:

| Add-on | Cost | What it fixes |
|---|---|---|
| Verified Phone Number | $10/mo | Stops the number being labelled "Spam Likely" — the risk flagged in open decision 3 |
| Branded Call | $0.10/outbound call | Displays the business name as caller ID |

At 500 intake calls that is $50 of Branded Call — trivial against recruiter time,
but it is per-call rather than per-month, so it scales with volume.

If outbound answer rates still disappoint, add a **local** number for outbound
only and keep the toll-free number as the published inbound line. Retell allows a
different agent per direction, so this needs no code change.

### SMS is a different track on toll-free

Toll-free numbers do **not** use A2P 10DLC. They use **Toll-Free Verification**,
a separate process with its own lead time. Voice is unaffected; only the
review-link-by-text depends on it, so it must not gate Phase 2.

### The two webhooks are not the same thing

Retell has two, and they do different jobs:

1. **Inbound webhook** (set per number, on the Phone Numbers page). Fires when a
   call arrives, *before* the agent speaks, and its response can inject dynamic
   variables. This is the caller-identification mechanism for Phase 4: look up
   `from_number` against `CandidatePhone`, and return what the agent may know.
   Subject to the disclosure rule — identifying a caller authorises accepting an
   update, never reading their record back without confirmation.
2. **Post-call webhook** (agent/workspace level). Carries the transcript and
   recording. This is what feeds the Phase 1 extraction.

Both must be publicly reachable. `deploy/Caddyfile` already proxies `/v1/` to the
API on `APP_DOMAIN` (`app.goforay.io`), so the routes land at
`https://app.goforay.io/v1/webhooks/intake/...`. Note Caddy blocks
`/v1/auth/signup` at the edge but passes the rest of `/v1/`. Until those routes
exist, leaving the webhook fields blank is correct — a configured URL that 404s
fails every call.

`deploy/twilio_provision.py` is retained: it is how a second (local, outbound)
number gets provisioned and trunked if we add one.

## Provider adapter

Define `VoiceProvider` alongside the `MailProvider` precedent: `place_call`,
`parse_webhook` → a normalised `CallEvent`, `fetch_transcript`. Retell is the
first adapter; a `fake` provider backs the tests the way `memory` does for mail.
No provider wire format may escape its adapter.

This has already earned its keep. The platform choice went Retell → Synthflow →
Retell before a line of provider code was written, and nothing in Phase 1 had to
change: the schema, coverage gate, extraction and phone projection are all
provider-agnostic, and `IntakeCall.provider` is a plain string rather than an
enum precisely so a new value does not need a migration.

Keep it that way. Every one of these platforms offers its own post-call
extraction — Retell's is its analysis/`collected_variables` surface — and
persisting those slot names directly is the specific temptation to resist.
**Our extraction owns the record.** Storing the provider's vocabulary would put
intake back into a private shape that `jobmatch` cannot score, undoing the whole
point of §"Schema decision". A provider's own extraction is worth at most a
second opinion for the `diff_records` cross-check.

---

## Phases

**Phase 1 — Schema, models, extraction. No vendor. ✅ DONE**
`CandidatePreference` / `IntakeCall` / `CandidatePhone` + Alembic migration.
Extraction: transcript → `ParsedQuery` + extension fields via `llm.json` with a
strict schema in `prompts.py`, plus a heuristic fallback, following
`jobmatch/spec.py`'s conservative posture (invented constraints are the expensive
failure). Run both a live-extraction and a post-hoc pass and diff them.
*Exit: a committed transcript fixture extracts to a schema-valid, taxonomy-normalised
preference record, tested with `FakeTransport` and no API key.*

**Phase 2 — Webhook, org resolution, queue.** Unauthenticated signed router
following `track.py`. Org resolved from the dialled number or a signed widget
token — never from the payload. Webhook returns 200 immediately and enqueues
`intake.extract` (new `KIND_`), idempotent on provider call id. Wire the chosen
provider adapter.
*Exit: a replayed provider webhook produces an `IntakeCall` + preference record via
the worker; a forged or unsigned request is rejected; a duplicate webhook is a no-op.*

**Phase 3 — Agent + website entry point.** Author the intake agent. Its system
prompt carries the gate as a goal checklist and it calls a `check_coverage` tool
mid-call, so `next_questions()` — not the model's sense of elapsed time — decides
what to ask and when to wrap up. Target two to three minutes when the gate
closes, longer when it does not; adaptive follow-ups, two per topic max.
Embeddable widget: in-browser call plus "call me" number entry.
Pre-call context passed as provider metadata so the agent never re-asks a known
fact. Public review page (routes hoisted above the auth gate) where the candidate
corrects what was captured and sets confidentiality.
*Exit: stranger → completed call → corrected record, on desktop and mobile.*

**Phase 4 — Dial-in status line.** Inbound number; caller lookup via
`CandidatePhone`; **verification before disclosure** (see amended criterion 3);
diff-mode update conversation; unknown caller falls through to full intake;
recruiter alert on material change (offer, deadline, accepted elsewhere). Respect
the existing `Suppression` / do-not-contact list.

**Phase 5 — Match-engine integration (the payoff).** Feed `CandidatePreference`
into `score_pair` as a second direction, so a `JobMatch` reflects both what the
company asked for and what the candidate said they want. Surface intake state on
`CandidateDetail.jsx`; review queue for flagged extractions.

**Phase 6 — Hardening.** Persona eval suite (evasive, rambling, hurried, hostile,
non-native speaker, exploding offer) graded per field, in the style of
`eval/queries.yaml` — rules over attributes, not frozen expected values. Rate
limiting and abuse handling on the public number. Recording retention policy.
Add a Voice Intake section to `AGENTS.md` recording the non-obvious failure modes.

Each phase ends demoable. Do not start Phase 3 before Phase 2's exit criteria pass.

---

## Acceptance criteria (amended)

1. Website → completed intake call in under 60s, desktop (browser) and mobile ("call me").
2. A completed call yields a schema-valid record, ≥90% of fields filled or explicitly
   marked skipped/missing — nothing silently absent.
3. The status line recognises a returning candidate and references their existing
   state within 20 seconds **of verification** — ANI match alone authorises accepting
   updates, never disclosing them.
4. Confidential items never appear in any client-facing view.
5. Recording consent captured before substantive questions, every call, stored on the record.
6. A recruiter reads the one-screen summary in under 60s and knows what a client would ask.
7. Every intake preference field is expressed in the taxonomy `job_spec` already uses,
   so it is scoreable against `job_postings` on day one.
8. A cooperative caller who answers the required set is off the phone in 2–3 minutes;
   a caller who has not answered it is still being asked. Call length tracks coverage,
   never a timer, and a refusal advances coverage rather than stalling it.

## Guardrails

No voiceprint or voice-biometric identification (biometric-privacy statutes) —
phone-number match plus verbal confirmation only. The agent emits no fit or
quality judgment: intake is data collection, and a scoring output would pull this
into automated-employment-decision-tool territory with bias-audit obligations.
No EEO-protected attributes collected, and if volunteered, not stored in
extractable fields. The agent never promises a role, names clients unprompted, or
gives legal or visa advice.

## Open decisions

1. ~~Vapi or Retell~~ — **decided: Retell.** The `VoiceProvider` adapter still
   goes in, so the choice stays reversible and tests get a `fake` provider.
2. **Firm name and branding** the agent uses on calls; human-handoff destination.
3. **Outbound caller identity** — the "call me" flow needs a verified/branded number
   with STIR-SHAKEN attestation and CNAM registration or it gets spam-labelled.
   Days of lead time; start it during Phase 2, not Phase 4.
4. **Recording retention**, and whether calls may be used to improve prompts.
5. **Which functions we recruit for** — sharpens the title taxonomy beyond the
   current tech-role bias in `enrich/taxonomy.py`.
