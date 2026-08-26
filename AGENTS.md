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

## Submitting applications (`packages/core/juicebox/applications`)

- **`request_submission` is the one door in.** Consent and the submission budget
  are checked there, before a job row exists, the same way `create_application`
  owns per-posting uniqueness. A surface that forgets a check must not be able
  to bypass one. `auto_submit` is the forgiving wrapper for side-effect surfaces
  (a portal approval, a thread "yes") — it reports a reason instead of raising,
  because "no adapter for this employer" is the normal answer.
- **Adding a vendor means implementing `Submitter` in `submit/base.py` and
  registering it in `submit/registry.py`.** Nothing else moves. An adapter gets a
  `SubmissionContext` with the bytes already read and returns a
  `SubmissionResult`; it never takes a `Session`, because an adapter one refactor
  away from moving the pursuit itself is a second status machine.
- **Only `applications/state.transition` marks anything submitted.** An adapter
  returns a verdict; the machine decides what it means. The consent gate guards
  `submitting` as well as `submitted`, so we never discover at the end of a
  browser run that we were never allowed to record it.
- **`needs_human` is a terminus, not a retry.** A required field we cannot fill
  from the resume, the preference row or the form answers stops the run. Nothing
  in this package invents an answer — `docgen/answers.py` leaves `why_company`
  blank on purpose, and a browser adapter leaves a blank field blank.
- **Every attempt gets a row, including the ones that failed.** A retried job
  must not look like a submission that never happened, and the partial unique
  index on a succeeded row is the double-submit guard that survives two workers
  racing.
- **`ats/detect.py` resolves from the URL alone on the ingest path** and is
  allowed one network hop only when somebody is actually applying. A URL-only
  miss leaves `ats_resolved_at` NULL on purpose — that is what lets the later
  follow happen. Recognising a vendor is not the same as having an adapter for
  it; `registry.get_submitter` returning `None` means Tier 0, not an error.
- **Alias mail never reaches the recruiter-reply path.** `integrations/sync.py`
  routes anything addressed to `anna+<token>@goforay.io` into
  `applications/inbox.py` and returns. Intent classification, reply drafting and
  enrollment stops are built for a candidate answering a recruiter, and would
  read an automated ATS acknowledgement as somebody changing their mind.
- **The alias token is derived, not stored** (`security.alias_token`). Inbound
  mail arrives before any lookup, and `candidate_for` re-verifies the token
  against the row's own candidate id rather than trusting an address it was
  handed.

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
imported into it (+1 628 386-5454). Plan and phasing live in
`VOICE_INTAKE_PLAN.md`. Non-obvious points:

- **The gate is data, not an instruction.** `schema.coverage()` decides when a
  call may end and `schema.next_questions()` decides what to ask; both read the
  record. This is what makes a cooperative call two or three minutes and an
  evasive one longer. Do **not** move that judgement into the system prompt — an
  agent asked to decide whether it has "covered enough" guesses, and the failure
  is invisible because the transcript always looks like a complete conversation.
- **A stated time budget may only narrow the gate.** `minutes_available` is
  persisted on first sight and `coverage(core_only=)` closes on `CORE_KEYS`
  when the budget is at or under `intake_core_only_minutes`. Twenty minutes
  does not add questions. No budget (`None`) is today's full required set.
  `spec.must_have_skills` is asked early but is **not** in `CORE_KEYS` — the
  heuristic cannot fill it, and putting it on the short-call gate would run
  every model-less pass to the 15-minute ceiling. Skipped required keys land
  on `deferred_fields` so they do not look forgotten.
- **Reactions land on the domain, never the person.** The agent may remark once
  on the field ("backend infrastructure — that's an area with real movement
  right now") and must never react to compensation, notice period, work
  authorisation, or anything protected; never say they are strong or in demand;
  never name a client or quote a number of roles. `progress_line` is composed
  in code at milestones `{5, 3, 1}` so the agent cannot invent a count.
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
- **A finished call rescores off `preference.updated_at`, not `Candidate.updated_at`.**
  Writing `CandidatePreference` touches neither the candidate row nor today's
  watermark by itself. `enqueue_match_for_candidate` keys idempotency on the
  preference clock plus `JOB_MATCH_ENGINE_VERSION`; the match tick also
  enqueues when `preference.updated_at > watermark.computed_at` or the stored
  `engine_version` is stale. Do not bump `Candidate.updated_at` to force a
  rescore — that ripples into `repository._apply` and the contacts grid sorts.
  A veto is `0.0` with a veto criterion, not `None`: `None` would keep the old
  score.
- **A key `ParsedQuery` cannot emit is a dead branch with a green test.** The
  comp veto originally read `parsed_spec["compensation_min"]` and five other
  guessed spellings. `parsed_spec` is `ParsedQuery.to_dict()`, which had no
  compensation field at all, so it was unreachable on every real posting — and
  its test passed because the test hand-wrote the key. `min_compensation` /
  `max_compensation` / `compensation_currency` are real fields now, filled by
  `search.parser.parse_compensation` on both JD paths. Two rules survive the
  fix: probe one key you can point at in the dataclass, never a list of
  candidate spellings; and drive a spec test through `parse_posting`, because a
  hand-planted `parsed_spec` proves nothing the parser would produce.
- **The veto compares the posting's ceiling, not its floor.** `max_compensation`
  is what a candidate's floor has to clear. "From 180k" states no ceiling and
  could pay 450k, so it is never a veto; neither is a silent posting, nor one
  quoted in a different currency (converting needs a rate, and a guessed rate
  drops real roles). `parse_compensation` is deliberately conservative for the
  same reason a false positive here silently removes a good role: it ignores
  clauses about the company's money (`raised`, `revenue`, `arr`) and strips
  `401k`, which is in half of all job descriptions and parses as $401,000.
- **The agent's persona exists twice and a test keeps it honest.**
  `RECRUITER_NAME` / `SMS_NUMBER_SPOKEN` in `deploy/retell_agents.py` and
  `intake_recruiter_name` / `intake_sms_number_spoken` in `config.py`. `deploy/`
  is stdlib-only and cannot import the app, so the copy is structural, not
  laziness. If they drift the agent greets as one name and refers to itself by
  another mid-call; `test_the_persona_matches_the_deploy_script` is what catches
  it. Do not name a server-side setting in the prompt either — the agent cannot
  see `sms_enabled`, so the close defers to `guidance`, which is composed
  server-side from the flag.
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

### Who called (`intake/contact.py`, `service.link_caller`)

- **The inbound agent asked for a name and an email long before anything
  stored them.** Step 3 of the inbound prompt has always been "ask for their
  name and the email they use with Foray, so a recruiter can find them"; the
  answer was transcribed and dropped. Every caller whose number was not already
  in `candidate_phones` therefore produced a completed intake with no person
  attached, waiting on a human to retype an address that was in the transcript.
  `IntakeCall.contact_name` / `contact_email` are that answer.
- **Identity is not a preference, on either channel.** These live beside the
  record, never inside it -- same rule `IntakeThread.contact_name` follows, same
  reason: `spec` is `ParsedQuery`'s vocabulary and a name has no counterpart on
  the job side. They are also outside `coverage()`: a caller who will not give a
  name has still completed an intake, and gating the wrap-up on identity would
  keep a finished call alive over a field the matcher never reads.
- **An address identifies somebody; a number does not.** An address the caller
  spoke may attribute the record to its owner. It may **not** write the calling
  number onto that person's row -- "somebody who knows Priya's address called
  from this phone" is not "this is Priya's phone", and a wrong row in
  `candidate_phones` reads that person's comp and pipeline to whoever dialled.
  The email-matched path leaves `UNVERIFIED_NUMBER_REASON` on the call, and
  `attribute_call` -- a human who has read the transcript -- is the only path
  that promotes the number.
- **A name is not an identity and creates nobody.** There are four Priya
  Ramans. Text sets the same bar for the same reason.
- **Creating anybody needs the call to have captured something**, not a closed
  gate. `coverage(...).covered` is the guard `attribute_thread` already uses.
  The gate was the first version and it was wrong: the heuristic path can hardly
  ever close one, so on an install with no `ANTHROPIC_API_KEY` -- the install
  this stack is built to run on -- it read as "never create anybody".
- **`_LEAD_INS` is why a spoken address is read correctly or not at all.** Three
  independent rules keep the decoder honest and each is a real failure: the last
  token must be a known suffix, or "reach me at priya dot raman" reads the
  *local* part as a domain and yields `me@priya.raman`; the word to the left must
  be a lead-in, or "priya raman at gmail dot com" (no spoken separator between
  the names) matches from "raman" and yields somebody else's mailbox; and the
  assembled string has to satisfy the ordinary address regex. Where the reading
  is ambiguous -- `priyaraman` or `priya.raman`? -- nothing is the only safe
  answer. `email_in_turn`'s cue gate is a *second* line of defence, not the
  first: "I work at Google dot com" survives the decoder and is stopped there.
- **A `review_reason` may never contain `"; "`.** `_join_reasons` uses it as the
  separator, so a reason holding one splits into two segments and stops matching
  itself. The first version of `_clear_identity_review` was an equality check
  against a single string, and the moment a call could carry two reasons it
  silently stopped clearing anything -- every attributed call stayed on the
  review queue forever. `test_no_reason_contains_the_separator` pins it.
- **`counterparty_number` exists because `from_number` is us on an outbound
  call.** Reading it as the candidate's is how our own intake line ends up in
  `candidate_phones`, pointing the dial-in caller lookup at whoever last had a
  row written.
- **`repository.merge_contact_details` is the one writer for "they told us their
  email/number/name".** Voice and text had drifted: the call path added a phone
  and an address and ignored the name, the thread path wrote none of the three
  onto the candidate at all -- so a texter's number never reached
  `candidate_phones` and the dial-in line did not recognise them when they rang.
  It owns three rules that are each easy to get wrong alone: a detail somebody
  else owns is reported as a conflict rather than taken (appending to
  `Candidate.emails` first leaves the JSON column claiming an address the
  projection does not have, which reads as ownership everywhere except the
  index); a placeholder name loses to a real one but a real name never loses to
  another real one (that is a merge question, not an import); and a newly
  learned address for a suppressed person is suppressed too, or do-not-contact
  is defeated by the person mentioning a second mailbox.

### One person, two channels (`intake/records.py`)

`AGENTS.md` already records that `messaging.records.persist_preference` and
`intake.service.upsert_preference` upsert the same row and never conflict
textually, so they diverge silently. Three divergences had accumulated, and all
three are now closed:

- **A call used to overwrite what a text had captured.** `upsert_preference`
  assigned every column from the call's record, so somebody who texted their
  comp expectation and then rang to mention an offer came out of the call with
  the comp blanked -- a three-minute conversation about one thing does not
  re-state the other thing. Both paths now call the monotonic merge, which moved
  to `intake/records.py` (`messaging.records` re-exports it, so the callers that
  grew up around that module are unchanged). It lives in `intake` because
  `messaging` imports `intake` and the other direction is a cycle.
- **The mid-call gate knew nothing about the stored record.** It is computed
  from this call's transcript, so a candidate who answered eleven questions by
  text last week was asked the eleven again and a two-minute update call ran to
  the fifteen-minute ceiling. `prior_preference_record` seeds it. The seed goes
  *under* the transcript, so "actually, make that 260" said out loud still wins,
  and it is deliberately **never written to `call.extracted`** -- that record is
  evidence about this conversation, and folding in things the caller never said
  makes a transcript and a record disagree.
- **Only the text path asked for job discovery.** A caller got scored against
  today's postings while a texter got a search for postings that did not exist
  yet.

Note the asymmetry between `known_for_call` and `prior_preference_record`, which
look like the same idea and are not: **identity** must not count as covered,
because a name we imported is not something the candidate said on this call and
banking it closes the gate on a question nobody asked -- while a **stated
preference** genuinely is known, and re-asking it wastes the call.

### The dial-in line knows who is calling (`service.inbound_call_context`)

Identity on the inbound line is resolved from caller ID **before the agent
speaks**, not asked for during the call. Retell fires `call_inbound` while the
phone is ringing and interpolates our response into the prompt, so
`/v1/intake/webhook` answers it with `caller_known`, the caller's first name,
when we last spoke, their last inbound text, their record and what we still owe
them. The prompt branches on `caller_known` and never asks.

This replaced an `identify_caller` tool that asked every caller for an email to
look them up and then, even for a caller matched on two factors, returned
topics rather than values. Both halves were wrong in the same direction, and
the rules that stand now are:

- **Never ask a caller to establish who they are.** The number answered that
  before the agent opened its mouth. Asking spends a question and an apology on
  something already known, and lands hardest on the returning candidate it is
  supposedly for. `deploy/tests/check_agent_prompts.py` fails the build if the
  asking creeps back into the prompt.
- **A recognised caller is entitled to their whole record, read back plainly.**
  Figures, companies, dates. `confidential_items` included: that flag means "do
  not put this in front of a *client*", and the person on the phone is the one
  who dictated it. Hedging with somebody about their own disclosure is the
  failure, not the safeguard -- and it is worst precisely on the subject they
  most likely rang about.
- **What is genuinely not disclosable is our side, not theirs.** Client
  feedback, whether anyone is interested, where a submission stands. That is a
  gap in what the record holds, not something being withheld, and the prompt
  says it that way.
- **An unrecognised number gets a first intake, with no implied history.** That
  covers a genuinely new caller, a number we do not hold, and a withheld caller
  ID -- `lookup_key` refuses the network's placeholders, so the withheld case
  needs no special path and cannot match the previous anonymous caller.
- **Ring time never fails the call.** An unclaimed number, an unknown caller
  and a broken lookup all return the unknown-caller variables. A 404 to a
  ringing phone is a dead line; the worst case here is a few questions we
  already had answers to. `default_dynamic_variables` covers the same ground on
  Retell's side, so a webhook timeout degrades to a first intake rather than an
  agent reading `{{caller_known}}` out loud.
- **The residual risk is a spoofed ANI, and it is accepted.** The record is
  what the candidate said about themselves, not an account anyone can spend.
  `resolve_candidate_id` says so in its docstring rather than carrying a rule
  the code above it no longer follows.

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
- **Consent flags are latched true, never overwritten.** `consent_recording` and
  `sms_opt_in` share one reader (`service._analysis_flag`) and one rule: a later
  event omitting the field must not silently withdraw a consent, and false is
  the safe default because unasked is unconsented. `sms_opt_in` is TCPA consent
  to *application updates* -- opt-out happens by a person replying STOP, never
  by a webhook. No verbatim-language column, deliberately: the transcript on the
  same row is the evidence record, and a copy could drift from it.
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

### The wrap-up pitch (`intake/teasers.py`, `/v1/intake/matches`)

A finished intake closes with two or three open roles that fit what the caller
just said they wanted, then the offer to text the full list. Non-obvious rules:

- **The agent reads sentences; it never composes them.** `lines`, `more_line`
  and the follow-up promise are built server-side, and the prompt's hard rules
  say the only company names, pay figures, and role counts the agent may ever
  speak are the ones inside those strings. `check_agent_prompts.py` pins this.
  Adding a "mention the role naturally" instruction to the prompt instead of a
  composed line is the regression to watch for.
- **Selection is the preference term alone, on purpose.** Postings are ranked
  with `jobmatch.preference.preference_term` against a *transient*
  `CandidatePreference` built from the live transcript -- never `score_pair`'s
  candidate-fact blend. Two reasons: the intake channel must emit no
  evaluation of the person (`INTAKE_COMPLIANCE.md` §5), and a first-time
  caller has no `JobMatch` rows mid-call -- the rescore is a queued worker
  job, so anything reading the book here would race a queue it cannot see.
- **What may be named is the posting's provenance, decided in code.**
  `NAMEABLE_SOURCES` is the policy: open-market sources say the company,
  `project` mandates become "one of our client companies", so the prompt's
  "never name a client" survives the feature that names companies. Pay is
  spoken only as the posting's `parsed_spec` states it, and never converted
  across currencies -- the same refusal the comp veto makes, for the same
  reason a wrong figure is worse than none.
- **A weak match is dropped, not ranked low.** `intake_teaser_min_score`
  errs high because a bad role pitched with enthusiasm burns the trust the
  pitch exists to build; vetoed postings, dismissed matches, and the caller's
  current employer are never pitched at all.
- **The consent ask never changes with the pitch.** The pitch primes ("I'd
  love to text you the full list"), then the canonical TCPA question is asked
  in the exact words the prompt has always had -- one wording is what keeps
  the transcript an evidence record, and `check_the_consent_ask_is_still_verbatim`
  fails the build if a prompt edit forks it.
- **The promise tracks what can actually send.** Guidance offers "keep an eye
  out for a text" only when a standing text thread exists (the rescore's
  `matches.ready` notification delivers there) or `sms_enabled` is true;
  otherwise it promises a recruiter. An opted-in caller who was pitched and
  cannot be texted lands on the review queue with
  `TEXT_PROMISE_REASON` (`intake.match_text.parked`) -- the promise was made
  on a recorded call, so it must land on a human rather than evaporate.
- **`matches_offered` is evidence, like the gate snapshot.** What was pitched,
  verbatim with posting ids and scores, on the call row -- the book moves and
  the record gets edited, so "what was this caller told" must not be a
  recomputation.

### Cheap models cannot be trusted with easy-looking work

The deployment runs cheap open-source models through OpenRouter (`z-ai/glm-5.2`
for reasoning, `openai/gpt-oss-120b` for `json_fast`). Two measurements from the
intake extractor, five runs each, that generalise past this subsystem:

- **Bucketing is not a model's job.** On "two weeks if the right thing came up",
  GLM 5.2 picked the right timeline bucket 0/5 and gpt-oss-120b 1/5, varying
  between `1_3_months`, `3_6_months` and `immediate` across identical calls. The
  fix was to have the model quote the phrase (`timeline_verbatim`) and map it in
  code, sharing `_TIMELINE_PATTERNS` with the heuristic path. Anything a
  deterministic function can decide -- fixed enums, arithmetic, thresholds --
  should be decided there, or a required field becomes non-deterministic.
- **A model will invent a permission.** Given a transcript where sharing never
  came up, GLM asserted `ok_to_share_with_clients: true` 3/5 and gpt-oss 2/5. So
  a model's claim never satisfies a consent or safety check on its own: it is
  recorded as evidence, it still closes the coverage gate (the *call* did its
  job), and `review_needed` stays set until a human or the candidate confirms.
- **Expect non-JSON.** Both models intermittently return prose despite a strict
  `json_schema` request and `provider.require_parameters`. The heuristic fallback
  is load-bearing, not decoration -- which is also why it must never fill
  `must_have_skills`, `highlights`, or consent.

### What the second live call taught us (inbound, the 628, 2026-08-23)

213 seconds, first call on the imported own-Twilio number. The consent capture
worked end to end -- `consent_recording` and `sms_opt_in` both arrived true via
post-call analysis. Three prompt rules came out of it:

- **The agent narrated its own tooling.** `check_coverage` failed (the number
  was registered in prod only after the call) and the agent told the caller "I
  had a technical issue checking coverage" -- which the caller heard as "COVID".
  Rule: on tool failure, continue smoothly and retry later; callers never hear
  about internal state.
- **A model invents a duration.** Asked "how long will this take", the agent
  said "10 to 15 minutes" -- three to five times the design target, and the
  caller nearly hung up. The expected duration is now stated in the prompt;
  anything time-shaped that matters must be, or the model will improvise it.
- **Echo acknowledgments are where the seconds go.** Every answer was read back
  in full ("Thank you for sharing that you want a level four backend role...").
  Rule: acknowledge in five words or fewer; the closing recap is the only
  repetition, and only if wanted.

### What the first live call taught us

223 seconds, real candidate, 2026-08-22. The mechanism worked -- the gate drove
the questions, the agent wrapped up when it closed, a mid-call comp retraction
("five hundred k... actually, I won't tell you") was correctly recorded as
declined rather than as $500k, and asked to read back the caller's own details
the agent refused. Four things were wrong, and three are the same thing:

- **A model fills required fields it was never given evidence for.** Nobody
  asked about work authorisation; the model wrote `"unknown"`, which `is_filled`
  counts as present, so the gate closed on an unasked and client-blocking
  question. Nobody asked for permission to share; the model returned `true`.
  Free-text fields now reject placeholders (`_PLACEHOLDERS`), and consent
  requires `ok_to_share_verbatim` -- **quote or it did not happen**. Apply that
  pattern to any field where fabrication is worse than absence.
- **The fallback is where wrong answers live.** `timeline_verbatim` is bucketed
  in code, but when the quote maps to no bucket the model's own choice is used --
  and on "if I find the right role I might start soon, but if not I'm okay to
  stay" it chose `under_1_month`, which puts a browsing candidate in front of a
  client hiring now. Conditional phrasing is now explicitly exploratory, and a
  bucket that came from the model rather than the mapping is flagged low
  confidence so it never looks as solid as a derived one.
- **A real title has to survive the taxonomy.** "Member of technical staff"
  noise-stripped to `member technical`, which matches no posting -- losing the
  entire point of sharing a vocabulary with the JD parser. Aliased to `software
  engineer`. When intake meets a title the taxonomy mangles, fix
  `TITLE_ALIASES`; do not let the intake side keep a private spelling.

## Messaging intake (`packages/core/juicebox/messaging`)

Candidate intake over text: a web chat widget now, Linq iMessage as the interim
text channel (tester-only, inbound-first), with Twilio SMS deferred until 10DLC
registration clears. It runs the *same* conversation as
the voice agent and writes the same `CandidatePreference`; everything that
decides **what** to ask is `juicebox/intake/` and is shared verbatim. This
package owns only what is different about text. Plan and phasing live in
`MESSAGING_INTAKE_PLAN.md`. Non-obvious points:

- **The gate decides cadence here, not hangup.** On a call
  `required_complete` says when the agent may stop talking. On a thread there
  is nothing to hang up, so it decides whether to send at all. The costs are
  not symmetric: a long call is disliked, one message too many gets the number
  blocked and every candidate after that silently receives nothing. Hence one
  question per message, and **never two outbound in a row** — an outbound
  requires an inbound after the last thing we sent. The only exceptions are the
  opening greeting and the single opt-out acknowledgement.

- **`IntakeField.asks` is not a question you can send.** It is a topic line for
  a voice agent's goal checklist ("Which roles they are actually going after."),
  and sending it verbatim asks somebody to describe themselves in the third
  person. `messaging/prompts.py:QUESTIONS` holds the candidate-facing wording,
  and is a *copy* of the field set in the same sense `llm.prompts.
  _INTAKE_FIELD_KEYS` is: `test_every_intake_field_has_a_question` is what stops
  the two drifting, and tells you where the second edit goes.

- **The heuristic extractor must only ever see the candidate's own turns.** It
  is regexes over the whole text with no notion of who is speaking. Given the
  full transcript it read a job title of *"take few details so recruiter can
  match you to right"* and a seniority of *"mid"* out of the agent's greeting —
  closing two required gates on words we wrote ourselves. `conversation._extract`
  gives the model the whole thread (an answer needs its question) and the
  heuristic `transcript_of(..., candidate_only=True)`. It also handles the
  degrade path explicitly rather than letting `extract_intake` fall back
  internally, because that fallback would still be holding the full transcript.

- **`records.merge_record` is monotonic, and that is what stops the gate
  oscillating.** Every turn re-extracts the whole transcript, and a later pass
  can legitimately come back quieter than an earlier one. Empty never
  overwrites filled; a filled value does overwrite, which is what makes
  "actually, make that 260" work. `declined` is a union — a refusal cannot be
  un-given by a pass that failed to notice it.

- **Two asks per field, then abandon — never mark it declined.** Nobody
  refused, and recording a refusal that did not happen puts an invented answer
  into a record that matching reads. The field stays in `fields_missing` and
  the thread is flagged for review. The cap applies to the contact questions
  too, and it matters most there: a name has no `declined` to fall back on, so
  without it somebody who never types anything name-shaped is asked forever.

- **Enrichment is budgeted (`MAX_ENRICHMENT_QUESTIONS`).** A call asks all
  fourteen because the line is already open. Each text is another notification.

- **Message idempotency is scoped to the thread, not the org** — deliberately
  unlike `Message.provider_native_id`. A mail provider's id is unique across a
  tenant; a web chat's comes from the browser, and two strangers both sending
  `"1"` would have the second message silently swallowed as a duplicate of the
  first.

- **An opt-out binds to the sender, not to the conversation.** A stranger who
  texts STOP has no candidate row and therefore no address to suppress, so the
  opt-out is remembered as a *stopped thread* and
  `threads.stopped_thread_exists` refuses a later one. Check for *any* stopped
  thread, never the most recent — the most recent is the conversation asking
  the question. `handle_stop` also writes the phone to `Suppression` when the
  number is known. Exactly one message goes out after a STOP, the confirmation
  a carrier expects.

- **Preferences are written when the required gate closes, not only at
  wrap-up.** A thread that answers everything and then goes quiet for a week
  would otherwise keep a complete record where matching cannot see it.
  `persist_preference` is an upsert, so later turns keep the row current.

- **The candidate row is created no earlier than that.** A public widget
  endpoint that made one on the first inbound message would let anyone fill the
  CRM by opening a chat and typing once; requiring a closed gate makes that
  cost a whole intake. An existing owner of the address always wins — splitting
  somebody's history across two rows is worse than not creating one.

- **`CandidatePreference` has one provenance link set at a time.** A thread
  write sets `source_thread_id` and clears `source_call_id`; the *content*
  still merges, so somebody who called and later texted keeps both halves.

- **`messaging/prompts.py` and `messaging/schemas.py` stay in this package.**
  The pre-merge plan was to promote them into `llm/prompts.py` and `schemas.py`
  after voice landed; that reason is gone and the self-contained boundary is
  better. Do not re-litigate the move.

- **Two paths write `candidate_preferences`, and a clean merge hid both gaps.**
  `messaging.records.persist_preference` and `intake.service.upsert_preference`
  upsert the same row and never conflicted textually, so the divergence was
  invisible until someone read both. Whatever either one grows, check the other:

  - **A thread write enqueues a rescore.** Without it a chat intake wrote a
    preference nothing ever scored, so the match term fired for callers and
    silently not for texters. Both paths key idempotency on
    `preference.updated_at`, never `Candidate.updated_at`.
  - **A thread may change a confirmed record; the confirmation does not
    survive it.** Voice *refuses* to overwrite once `confirmed_at` is set. A
    thread is the candidate's own words, which outrank a recruiter's
    transcription — so it writes, then clears `confirmed_at`/`confirmed_by` and
    goes back on the review queue. The state to never ship is the third one: a
    row still claiming a confirmation over content the confirmer never saw,
    because everything downstream trusts that flag.

- **A number's opt-out is written *and* read.** `handle_stop` now suppresses
  `from_address` and the candidate's known numbers through the phone column
  voice added, not just their emails -- and `is_blocked` checks
  `is_suppressed(phone=...)`, because writing a row nothing reads is theatre.
  `stopped_thread_exists` stays as the second read: `lookup_key` refuses to
  guess a country code, so a number it cannot parse has no `Suppression` row
  and the stopped thread is the only trace of the opt-out.

- **Attributing a thread refuses an address somebody else owns.** The guard
  `attribute_call` does not need: a thread usually knows an email, and
  attaching it to a different person is the disclosure failure, not a missed
  match. `UNATTRIBUTED_REASON` is one shared constant so the code that sets the
  flag and the code that clears it cannot drift, and attribution only writes a
  preference when the record actually captured something -- an empty thread
  must not blank a record another channel filled in.

- **The review queue reads threads as well as calls.** `IntakeThread.review_needed`
  defaults to `true`, so a queue that only knew about `IntakeCall` flagged every
  chat and displayed none of them. `GET /v1/intake/threads` is a deliberate
  sibling of `/calls` rather than a widened response: a thread has no duration or
  recording and a call has no message counts. Merging them is the `IntakeSession`
  refactor, which a review queue should not force.


## Phone numbers in the UI (`packages/web/src/lib/phone.js`)

- **One formatter, because E.164 reads badly and guessing reads wrong.** The API
  stores what `intake.phones.lookup_key` produced, which is correct and
  unambiguous and makes `+14155550123` a string a person has to decode before
  they can dial it. Only North American numbers get the grouped form: a country
  code is one to three digits and the national grouping differs by country, so a
  general "insert spaces every three" reformats a real London number into one
  that looks wrong to anybody who knows it. For everything else the stored
  E.164 *is* the presentation -- unfamiliar, but never misleading.
- **`telHref` always dials the raw digits.** The pretty form is for reading.
- **`CandidateSummary.phones` is empty on the search path** and that is honest,
  not a gap: search hits are built from the in-memory `CandidateDoc`, which
  carries addresses and not numbers, and no search surface shows a phone. It is
  why the field has a default.
- **`PhoneEditor`'s warning is about lookups, not deliverability.** A number
  stored without a country code is real data a recruiter can still ring; what it
  cannot do is match an inbound call, because `lookup_key` refuses to guess a
  country. Saying so beats rejecting it. Nothing else about the editor is new --
  `crm.patch_candidate` has always had `phones` in `EDITABLE_FIELDS`, in
  `UNION_FIELDS`, and syncing the projection; only the UI was missing.

## Consent copy (`messaging/prompts.py`, `messaging/consent.py`)

- **No greeting recites the opt-out any more, and that is a deliberate trade
  with a documented cost.** "you can stop any time", "Reply STOP any time to
  opt out, HELP for info", and the sentence after the voice consent question
  are all gone. `INTAKE_COMPLIANCE.md` §6a carries the argument (an opt-out
  recital in a *first* message is the strongest available cue that what follows
  is bulk, which produces the complaint it was meant to prevent) **and the
  exposure** (it cuts against `CANDIDATE_AGENT_PLAN` §2's three-disclosure
  rule, and 10DLC campaign review may simply reject a first message without
  opt-out language, which throttles a shared sender for every tenant on it).
  `SMS_ENABLEMENT.md` makes re-reviewing it a blocker on enablement rather
  than a copy tweak afterwards. If review asks for it back, it goes in the
  **first outbound message only** -- not every greeting, and not the voice
  wrap-up, which is not an SMS.
- **Removing a sentence about a mechanism is not removing the mechanism.** STOP
  still stops, HELP still answers, `handle_stop` still writes the suppression
  row and the stopped thread, the single acknowledgement a carrier expects
  still goes out, and `lowercase_outbound` still holds the keywords out of the
  voice transform whether or not any current message contains one.
  `test_intake_consent_copy.py::TestKeywordsStillWork` exists so a later edit
  cannot slide from the copy into the handling.
- **The consent *question* is not copy.** "Can I text you updates at this
  number?" is the TCPA opt-in itself and is unchanged, word for word. What was
  removed is the sentence that followed it -- which landed immediately before
  the candidate answered, so the last thing they heard was a reason to say no.
  The prompt now also tells the agent not to supply that sentence itself, and
  to answer plainly if a caller asks how to stop.
- **Disclosure versions are the reason this is safe to change at all.**
  `web-widget-v2`, `inbound-text-v2`, `voice-wrapup-v2`. Rows written under
  `-v1` keep describing what those people actually saw, which is why
  `ConsentRecord` stores the text rather than a pointer to it. Bump the version
  whenever the wording moves; never edit a disclosure string in place without
  one.
- **Three tests used to assert the recitals were present.** They now assert the
  two disclosures that survive -- the org is named, the AI is disclosed -- which
  is the part that was always load-bearing.

## CSV candidate import (`packages/web/src/lib/csvImport.js`)

Import a Juicebox CSV export and enroll the rows in an existing sequence. There
is deliberately **no new backend**: the page ends in the same
`POST /v1/candidates/bulk` then `POST /v1/sequences/{id}/enroll` that Sourcing
calls, so a CSV-sourced enrolment and a search-sourced one are the same row
afterwards. Non-obvious points:

- **Parsing is client-side on purpose.** The recruiter sees every row, with the
  address we would actually mail, before anything is written — which is what
  makes "take however many I want" possible. It also means no upload endpoint
  and no half-imported file to clean up.
- **Personal address before work, always.** `emails[0]` is what
  `sequencer.send_step` mails, so a work address in front of a personal one puts
  a recruiting pitch in somebody's work inbox. Same rule `forayImport.js`
  records, same reason.
- **Undeliverable addresses are dropped at import, not left to the reach
  floor.** `FORAY_MIN_REACH_SCORE` defaults to `0`, and `_sendable_address`
  short-circuits on `floor <= 0` and returns `emails[0]` unconditionally — so on
  a default install a known-bad address *would* be mailed. The floor is the
  second line of defence, not the first.
- **`email_reach` is no longer Foray-only.** Juicebox's
  Deliverable/Risky/Undeliverable verdict maps onto the same reach scores, and
  writing those under `raw.foray` to make them visible would be a lie about
  provenance. `sequencer._email_reach_scores` therefore spans every namespace in
  `raw`, merging `foray` last so its measured scores still win a conflict.
- **`source_id` is the LinkedIn slug** (`li:ada-lovelace`), falling back to the
  Juicebox profile URL, then an address, then a name slug. That is what makes
  re-uploading two overlapping exports update rather than duplicate — the same
  free idempotency the Foray importer gets from a GitHub login.
- **An `.xlsx` makes one round trip and comes back as CSV.** A workbook is a zip
  of XML the browser cannot read, so `POST /v1/imports/workbook` converts the
  first sheet with `openpyxl` and returns delimited text; the client-side mapper,
  the review table and their tests then carry on unchanged. That route persists
  nothing -- it is a format conversion, so no `CommittingRoute` and no audit row
  -- and keeping the conversion there rather than the whole import is what
  preserves "nothing is written until the recruiter presses the button". It
  reports the other sheet names, because data on tab two would otherwise look
  like an empty file.
- **An export can have no email columns at all.** Which columns a Juicebox
  export carries depends on what the user ticked, so every column is optional
  and looked up by name; the page says so rather than importing profiles nobody
  can be mailed. Real fill rates are low — 15 usable addresses in 79 rows is a
  normal file.
- `years_experience` is **omitted** when the column is blank rather than sent as
  `0`: `normalize_candidate` only computes it from experiences when it arrives
  as `None`.
