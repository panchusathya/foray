# Candidate Agent — Build Plan

Third plan in the intake series. `VOICE_INTAKE_PLAN.md` covers the phone,
`MESSAGING_INTAKE_PLAN.md` covers text; both collect *what a candidate wants*.
This one spends it: find the roles that fit, propose them, and — with a
per-application yes — apply. Read `AGENTS.md` first, and `INTAKE_COMPLIANCE.md`
before touching the fit bar.

## Decisions taken (Tushar, 2026-08-23)

1. **Mixed sourced-outbound and inbound texting, mostly inbound.** Written
   consent capture is therefore in scope — see §2.
2. **One shared number for everything** — every call, every text, every org.
   `+1 (628) 386-5454`.
3. **Quiet hours: yes.** §3.
4. **Application limits: yes.** §4 — which turns out to be three different
   budgets that are easy to confuse.
5. **The interface is messaging.** Anything hiring-related starts as a message.

---

## §1 What already exists, and what genuinely does not

The proactive loop sounds like a new product. Most of its machinery is built.

| Need | Already there |
|---|---|
| A market of postings | `JobPosting` — `source` = project / exa / manual / csv, `parsed_spec` (`ParsedQuery`-shaped), embedding, `pending_parse -> open -> stale -> closed` |
| Finding postings online | `JobScrapeQuery` (saved Exa queries on a cadence) + `KIND_JOBS_SCRAPE` / `KIND_JOBS_SCRAPE_ONE` |
| Turning a JD into a spec | `KIND_JOBS_PARSE`, `jobmatch/spec.py` |
| Scoring a candidate against a posting | `jobmatch/engine.py:score_pair` — heuristic + vector + **preference** terms |
| Per-criterion, human-readable evidence | `search/scoring.py:assess` and `jobmatch/preference.py:_criterion(name, label, score, evidence, veto=)` |
| Absolute vetoes | `engine.py:74` — a veto criterion with a zero preference score kills the match outright |
| Rescoring the book when the engine changes | `JobMatchWatermark.engine_version` |
| A conversation that can hold state for days | `messaging/` — `threads`, `conversation.run_turn`, cadence rules in code |
| Deferred, idempotent, retried work | `jobs.py` / `worker.py`, and `Job.run_at` |
| Suppression on email *and* phone | `outreach/suppressions.py` + `messaging/suppression.py` |

**What does not exist at all.** Verified, not assumed:

- **Resume ingest.** No parser, no upload route, and *no blob storage anywhere
  in the repo* — no S3, no `boto3`, no `UploadFile`. Where a file lives is an
  open decision, not an implementation detail.
- **Any notion of an application.** No model, no submission path, no status.
- **Any candidate-facing web surface.** `packages/web/src/App.jsx:128` returns
  `<LoginPage/>` before a single route renders. The public review page that
  `VOICE_INTAKE_PLAN.md` Phase 3 specified was never built. Messaging is
  currently the *only* candidate-facing surface that exists.
- **On-demand posting discovery.** `JobScrapeQuery` runs on a cadence. "Find
  me the hardware roles at Tesla, now" has no path.

So the build is: three new primitives (resume, proposal, application), one new
presentation contract, and a governance layer — on top of a matching engine
that already works in both directions.

---

## §2 What the two channel decisions cost, and how to pay it

Decisions 1 and 2 are the two hardest options in `MESSAGING_INTAKE_PLAN.md`'s
open list. Taking both is defensible, but neither is free and the mitigations
are not optional.

### One shared number couples every tenant's reputation

Juicebox is org-scoped; a shared sender is not. One tenant's complaint rate
degrades delivery for every other tenant, and carrier filtering is invisible —
messages stop arriving and nothing errors. Three mitigations, all cheap:

1. **A per-org outbound budget**, enforced in the send path, not the UI.
   `MAX_NEW_THREADS_PER_ORG_PER_HOUR` is the precedent already in
   `routers/messaging.py`.
2. **A complaint-rate circuit breaker.** Track opt-outs per org over a rolling
   window; past a threshold, that org's outbound stops and a human is told.
   Opt-out rate is the number carriers actually score, so it is the number to
   watch.
3. **Identify the org in the first message of a thread**, every time. A shared
   number means the candidate cannot recognise the sender from the number, so
   the message has to do it: who we are, which company's process this is about,
   and how to stop.

### Sourced outbound is the case none of the comparables solve

Poke, Instinct and Tomo all arrange for the candidate to text first
(`MESSAGING_INTAKE_PLAN.md` records the mechanisms). We are choosing not to.
That means consent provenance is load-bearing: in a dispute, the artifact
*is* the defence. A new `ConsentRecord` row, written wherever consent is
obtained, carrying channel, source, timestamp, the exact disclosure text shown
**and its version**, and the acting principal.

### The rule that makes "mostly inbound" true rather than aspirational

Do not cold-text a sourced candidate. First contact happens on a channel we
already have a lawful basis for — the outreach email stack, or the voice call
where `intake_calls.sms_opt_in` is now captured. **The text thread is opened by
their reply.** That is a one-line rule with three consequences: consent is
always prior, "mostly inbound" falls out of the architecture instead of being
a policy someone has to remember, and the shared number's first impression is
always a message the person was expecting.

Sourced outbound then means exactly one thing: texting a number whose owner
gave us consent on another channel and has not revoked it. That is a much
smaller, much safer surface than "sometimes we initiate".

---

## §3 Quiet hours

TCPA restricts solicitation to **8am–9pm in the recipient's local time**. We do
not reliably know the recipient's timezone, so the design question is what to do
when we don't.

### Getting a timezone, best source first

1. **Their stated location.** `CandidatePreference.spec.locations` is already
   collected in the required gate and already normalised through
   `enrich/taxonomy.py` (which knows metro membership). This is the best source
   we have and it costs nothing new.
2. **Candidate record location**, same normaliser.
3. **Area code.** A fallback only. Mobile numbers are portable and people move;
   a 415 number in Austin is common. Good enough to break a tie, not good
   enough to authorise a 7am send.
4. **Ask.** Cheap on a text channel, and worth doing when the thread is already
   open and nothing else resolved.

### When it is unknown, intersect the windows instead of guessing

If the recipient could be anywhere in the contiguous US, the only safe window
is the intersection of 8am–9pm across Eastern and Pacific. That intersection is
stable in both DST regimes when expressed in Eastern local time:

> **Unknown timezone → send only between 11:00 and 21:00 America/New_York.**

That guarantees no earlier than 08:00 for a Pacific recipient and no later than
21:00 for an Eastern one — a 10-hour window, which is plenty for a channel whose
nudge cadence is measured in days. If we ever text Alaska or Hawaii the
intersection narrows to 14:00–21:00 Eastern; make that a config value rather
than a constant, and keep the contiguous-48 default.

A known timezone gets the real 8am–9pm window. Store the resolved zone and
*how* it was resolved on the thread — a window derived from an area code and one
derived from a stated location deserve different confidence, and when a
complaint arrives you need to know which you used.

### A reply is not a solicitation

Quiet hours govern messages **we initiate**. Someone who texts us at 11pm
expects an answer; holding their reply until 11am makes the product feel broken
and does not serve the rule the restriction exists for. The engineering rule:

- An outbound message that answers an inbound one received within the last
  `INTAKE_REPLY_WINDOW_MINUTES` (propose 60) sends regardless of the clock.
- Everything else — greetings on a thread we opened, nudges, proposals — obeys
  the window.

This composes with the existing cadence rule that an outbound requires an
inbound after the last one we sent, so it needs no new state.

### Where it goes in code

A `messaging/cadence.py` gate, called from `conversation.run_turn` **before
composing**, for the same reason `suppression.is_blocked` is checked before
composing: rendering a message we then drop still costs a model call.

Deferral reuses the queue. `Job.run_at` already exists and the worker already
claims on `(status, run_at)` — a held message is `enqueue(KIND_INTAKE_TURN,
run_at=window_opens_at)`. Do not write a scheduler.

---

## §4 Limits — three budgets, and only one of them is new

"Limits on how many times we apply" collapses three different things. They need
separate counters, because they fail in different ways.

### 1. Message cadence, per thread — already built

One question per message; never two outbound in a row; two asks per field then
abandon; enrichment budget of six; `MAX_INBOUND_PER_THREAD = 200`. Enforced in
`conversation.run_turn` and `prompts.valid_question`. Nothing to do.

### 2. Nudge frequency, per person — decided, not built

First nudge at 48h of silence, at most 2 per thread, at least 72h apart, never
after a wrap-up or an opt-out. Add: never a nudge inside quiet hours, and a
nudge counts against the same budget whichever channel carries it, so moving a
nudge to email does not reset it.

### 3. Application submissions — new, and the one that matters

The binding constraint here is **not our cost**. It is the candidate's
reputation with an employer, which we can spend and they cannot get back.

| Limit | Proposal | Why |
|---|---|---|
| Per posting | **Exactly one application, ever.** Unique on `(candidate_id, job_posting_id)` | A duplicate submission reads as spam in an ATS and is the single most embarrassing failure available to us |
| Per company | 2 per rolling 30 days | Eight applications to Tesla in a week makes the candidate look indiscriminate, and ATS dedupe often flags it as one |
| Per candidate | 5 submitted / week, 10 proposed / week | Above this we are not representing somebody, we are spraying |
| Proposals per message | 3 | A text with eight jobs in it is not a conversation |
| Proposal messages per day | 1 | The channel has to stay something people answer |
| Ignored-proposal breaker | After 3 consecutive proposals with no reply, stop proposing and wait | Silence is information |

**And the limit that is not a number: consent is per application, never
blanket.** A candidate saying "yes, things like that" is not consent to a
specific submission. Every `Application` needs an explicit yes bound to a
specific `job_posting_id`, recorded with the message it arrived in. This is what
keeps us out of "you applied to forty jobs as me" — which is both the worst
product outcome and the worst legal one.

All of these belong in one `applications/limits.py` returning a decision
object, not scattered conditionals at call sites — the same reason
`schema.coverage()` owns the gate rather than the prompt.

---

## §5 What "meets our bar" means

Do not invent a second scoring system. `score_pair` already emits a score plus
per-criterion evidence, and already runs in both directions. The bar is a
filter over its output, with four parts:

1. **A score floor**, per-org configurable. Necessary and not sufficient.
2. **Vetoes are absolute.** `engine.py:74` already makes a veto criterion kill a
   match, and `jobmatch/preference.py` already emits them for excluded
   companies and comp below floor. Extend to work authorisation and impossible
   location. A veto is not a low score; it is a no, and it must not be
   outweighed by a strong vector term.
3. **Explainability is a gate, not a garnish.** Only propose a match we can
   explain in one sentence built from `criteria` evidence strings. If the score
   is carried by vector similarity with no structured criterion behind it, we do
   not propose it — the candidate will ask "why this one?", and "the embedding
   liked it" is not an answer we can give. This rule also happens to be the
   cheapest guard against proposing nonsense.
4. **Freshness.** `last_seen_at` and `status` are on `JobPosting` already.
   Proposing a closed role costs more trust than proposing nothing.

**Never show the candidate a score.** A number invites negotiation of the
number and implies precision we do not have. Show the reasons; keep the score
recruiter-side. This is also the line `INTAKE_COMPLIANCE.md` §5 draws — see
§12, because pointing this engine at the candidate is the one decision in this
plan that needs counsel rather than engineering judgment.

---

## §6 The two entry points

**A. A resume arrives.** Parse it, upsert supply-side facts, run the match
sweep for that one candidate (`KIND_JOBS_MATCH_ONE` exists), apply the bar,
propose the survivors.

**B. A named intent** — "I want to apply to Tesla's hardware engineer role."
Four steps, and the first is the one people skip:

1. **Resolve the company**, and confirm it back. "Tesla" is unambiguous;
   "Apex" is not, and applying somewhere the candidate did not mean is
   unrecoverable.
2. **Discover the postings on demand.** New `KIND_JOBS_DISCOVER_ONE` taking
   `(company, title_hint)`, building a company-scoped Exa query, writing
   `JobPosting` rows with `source="exa"` and enqueuing the existing parse. This
   is a new *trigger* on an existing pipeline, not a new pipeline.
3. **Score them against the record we already hold** — resume facts plus intake
   preferences — and apply the §5 bar.
4. **Propose, at most three, with reasons.**

Then: propose -> consent -> prepare -> submit -> track -> report. Every step is
a message, and every step is resumable, because a thread can be dormant for a
week (`threads.py` already assumes this).

---

## §7 Resume ingest — the missing primitive

New table `CandidateDocument`: kind (resume | other), mime, byte size, storage
key, extracted text, parse status, provenance, and the thread or call it arrived
on.

Three things to decide before writing it:

- **Where the bytes live.** There is no blob store in this repo today. Local
  filesystem behind the API is enough for the first version and is one config
  value away from S3; picking neither is what stalls this.
- **How a resume actually arrives.** Not carrier MMS. MMS size limits make a
  PDF-over-text unreliable in a way that presents as the product losing
  documents. Text a **signed upload link** instead — `security.signed_intake_token`
  and the `routers/track.py` pattern already do exactly this shape of
  unauthenticated, scoped, expiring access. *(Since revised for Linq: iMessage
  attachments have no MMS limits and arrive as CDN URLs on the webhook, so the
  message channel now ingests them directly — `messaging/attachments.py` — and
  the upload link remains the web-chat route and the failure fallback.)*
- **Whether a resume may overwrite `Candidate.summary` and `.skills`.**
  It must not do so silently: those two fields feed the retrieval haystack
  (`search/index.py:142`), so a resume parse would quietly change what
  recruiters find when they search. Land resume-derived facts with their own
  provenance and let `repository._apply`'s `manual_fields` immunity protect
  them from the nightly Foray refresh, exactly as intake preferences are
  protected.

Extraction is supply-side fact, not preference — so it is a sibling of
`intake/extract.py`, not a caller of it. Same posture though: strict schema on
the model path, heuristic fallback, and it must work with no
`ANTHROPIC_API_KEY`.

---

## §8 Submission — tiered, and honest about where to stop

Automated submission into arbitrary career portals is the least reliable and
most embarrassing thing in this plan. Tier it, and ship from the bottom.

| Tier | What it is | Verdict |
|---|---|---|
| **0 — Assisted** | We prepare everything: tailored summary, the answer set, the link. The candidate presses submit | **Build first.** Most of the value, almost none of the liability |
| **1 — Apply by email** | Postings with an application address. Reuses `outreach` and `MailProvider` wholesale | Build second. Small |
| **2 — Known ATS APIs** | Greenhouse, Lever, Ashby have documented job-board and application endpoints | Real automation, bounded and testable |
| **3 — Governed browser agent** | Public forms: typed-action planner + policy, Playwright on Browser Use Cloud. Workday/YC stay handwritten two-leg | **Built.** See the reversal note below |

Worth saying plainly, because it is the example in the brief: **Tesla's careers
portal is custom, not one of the Tier-2 ATSs.** Without the browser agent
configured, the honest product is still Tier 0 — we find the roles, score them,
explain the fit, prepare the application, and hand it over. With the agent on,
the same governed planner that fills Greenhouse public forms runs here too, and
a login wall, CAPTCHA, or required self-ID still hands off rather than
improvising. The messaging copy must not imply a send that did not happen.

### The Tier-3 reversal, and what it is not

This table originally ruled Tier 3 out entirely: *brittle, frequently against
site terms, and a malformed submission under someone's name cannot be
withdrawn.* Two of those three still stand. What changed is the reading of the
first one, and the change is narrow enough to be worth stating precisely.

**What is out of scope is a model that types free-form answers on arbitrary
sites** — deciding for itself what a field means and what to type in it,
whether that model runs in our worker or as a hosted Browser Use `POST /runs`
agent. That is what "brittle" was about, and it is still refused.

**What is in scope is a typed-action planner with a policy layer.** The model
proposes one next action (`fill` / `select` / `upload` / `check` / `click_next`
/ `dismiss` / `final_submit` / `stop`) naming a Juicebox-issued control id and
an opaque value token. A non-model policy resolves tokens from
`SubmissionContext`, refuses invented text, credentials, CAPTCHA, payment,
required self-ID, off-origin navigation, and a submit click that has not
cleared consent / blanks / page-class `application`. Playwright executes the
approved action on a Browser Use Cloud Chrome over CDP. Workday and YC stay
handwritten two-leg adapters until a later replacement.

Keyed ATS APIs (when we hold a board key) and email (`mailto:`) stay HTTP.
`APPLY_SUBMITTER=console` remains the default. Public Greenhouse / Lever /
Ashby / SmartRecruiters / Workable FormSpec maps and `HostedFormSubmitter`
are gone.

Three things make the difference load-bearing rather than rhetorical:

- **A required field we cannot fill from an approved token ends the run.**
  `needs_human` is a terminus, not a retry. There is no "click the most likely
  button" path, and an unmapped question is never answered by a model
  improvising from the JD.
- **Every run leaves evidence.** The confirmation screenshot, the redacted
  step trace, and the provider session id, on the attempt row. A submission
  nobody can reconstruct is one we cannot answer for — which is the honest
  form of "cannot be withdrawn". After one guarded submit, a receipt is
  success; anything else is a hand-off, never a retry-click.
- **Growing what the agent may type is a code change with a reviewer.** New
  tokens and page-class rules land in `applications/submit/browser/agent/`,
  not a prompt, and not a config row somebody edits at 2am.

The identity question came with it. Every one of these portals makes you
register, and registration gates on an email you can read — so Tier 3 and the
owned-alias decision are the same decision, and neither works alone. See
`applications/alias.py` for what that costs us, written down rather than
discovered later.

### What we assert on somebody's behalf

Every generated answer carries provenance pointing at the resume line, intake
field, or message that supports it. No answer is generated from nothing. A
review step before a candidate's *first* submission, and an
`Application.submitted_by` that distinguishes candidate, recruiter, and agent.
An invented year of experience is not a bug we can apologise for.

---

## §9 One job card, three renderers

The consistency ask, and the cheapest thing in this plan to get right early —
it is expensive to retrofit once three surfaces have each grown their own.

**One server-side projection.** `presentation/jobcard.py`: takes
`(JobPosting, JobMatch | None, CandidatePreference | None)` and returns identity
(company, title, location, remote), the three strongest reasons drawn from
`criteria` evidence, any veto, a source badge, freshness, and the single action
available. No renderer computes any of that.

**Three renderers over that one contract:**

1. **Recruiter web.** Extend `packages/web/src/lib/jobs.js` — `sourceLabel` /
   `sourceTone` / `SourceBadge` already exist and are already shared by
   `Jobs.jsx`, `JobDetail.jsx` and `JobPitch.jsx`. Extend it; do not fork it.
2. **Text.** A hard character budget, `prompts.MAX_QUESTION_CHARS` as precedent.
3. **Candidate web** — the public page. Needs public routes hoisted above
   `App.jsx:128`, which does not exist yet and is a prerequisite the voice plan
   identified and never built. Do it once, here, for job cards, application
   status and the intake review page together.

**Design the card for text first.** The 320-character renderer is what keeps
the contract honest: a field that cannot survive being read in a single text is
not a headline field, it is a detail, and the web surfaces can add details. Build
it the other way round and the text channel becomes a lossy afterthought.

The same discipline for the second half of the ask — **what we are putting in
people's names.** An `ApplicationCard` projection with the same three
renderers: recruiter sees every answer with its provenance, the candidate sees
what was sent and when, and the text channel gets one line of status. One
contract, three depths.

---

## §10 New tables

- **`CandidateDocument`** — §7.
- **`JobProposal`** — one posting proposed to one candidate on one channel.
  `proposed | accepted | declined | expired`, plus the message it went out in.
  **Do not overload `JobMatch.status`.** That field is recruiter triage
  (`new | shortlisted | dismissed`); candidate consent is a different axis, and
  collapsing them means a recruiter shortlisting something reads as a candidate
  agreeing to it.
- **`Application`** — unique on `(candidate_id, job_posting_id)`.
  `prepared | awaiting_consent | submitted | acknowledged | rejected |
  withdrawn`, the answer set with per-answer provenance, `submitted_by`, and
  the consenting message id.
- **`ConsentRecord`** — §2.

One Alembic revision per model change; `tests/test_migrations.py` fails on
drift. And do not stack them onto a mid-chain parent — the `sms_opt_in`
migration just cost us that, twice.

---

## §11 Phasing

Each phase ends demoable, and the early ones ship value before any of the
irreversible parts exist.

- **A — Governance.** Quiet hours, the resolved-timezone column, nudge budget,
  the limits module, the per-org circuit breaker. No new product surface, and it
  is what unblocks SMS the moment 10DLC clears.
- **B — The job card contract**, plus the recruiter renderer and the public
  route hoist. Nothing candidate-facing works without the hoist.
- **C — Resume ingest**, signed upload link, extraction.
- **D — Propose and consent** over messaging: the bar, the proposal table, the
  per-application yes, `KIND_JOBS_DISCOVER_ONE`.
- **E — Tier 0 and Tier 1 submission**, the answer set with provenance, the
  application card.
- **F — Submission, for real.** Target resolution (`applications/ats`), the
  `Submitter` seam and its adapters (`applications/submit`), the owned alias and
  its inbox (`applications/alias.py`, `applications/inbox.py`), the `submitting`
  status and the `application_submissions` attempt log. One adapter at a time
  behind a protocol, the same way `MailProvider` and `VoiceProvider` are shaped.
  Ships inert: `APPLY_SUBMITTER` defaults to `console`. Setup in
  `AUTO_APPLY_SETUP.md`.

Phase A is independent of everything else and should start now. Phases B and C
are independent of each other.

---

## §12 Open decisions

**Blocking, and not an engineering call.** Items 1–3 were scoped as "before
Phase D ships". Phase F sharpens all three, and item 3 in particular: with an
owned alias the address on every application is `@goforay.io`, so
non-disclosure is not a position available to us any more. Counsel before
`APPLY_SUBMITTER=live`, not before the code lands:

1. **Pointing the scoring engine at candidates.** `INTAKE_COMPLIANCE.md` §5
   deliberately kept intake evaluation-free and flagged `jobmatch` as
   recruiter-facing decision support, noting NYC LL144 (bias audit + notice)
   and the EU AI Act's high-risk classification for employment screening. This
   plan tells a candidate which roles fit them, which is a genuinely different
   posture — we are acting as *their* agent, not screening them for an employer
   — and that distinction is real. But Juicebox is a recruiting agency, and
   LL144 reaches employment agencies. **Counsel, before Phase D ships**, not
   before Phase A or B.
2. **Authority to apply on somebody's behalf.** Per-application consent over
   text is the right mechanism; whether it is a *sufficient* one, or whether a
   signed authorisation is needed first, is a legal question.
3. **Disclosure to the employer.** Does a submission say an agent prepared it?
   Not disclosing is the kind of thing that is fine until it is not.

**Engineering decisions, ours to make:**

4. Blob storage: local filesystem now, or S3 from the start.
5. Whether resume-derived facts may enter the search haystack (§7).
6. Alaska/Hawaii in the unknown-timezone window (§3).
7. The score floor, and whether it is per-org or global.
8. ~~Which of Greenhouse / Lever / Ashby is the first Tier-2 adapter~~ — all
   three shipped in Phase F. The ordering question is now which Tier-3 vendor
   follows Workday, and the answer is the same method: run
   `applications/ats/detect.py` over the live posting book and count. Oracle is
   the current guess only because it is what Goldman runs.
9. Retention for resumes, alongside the transcript and recording policies that
   `INTAKE_COMPLIANCE.md` §7 already lists as open.
