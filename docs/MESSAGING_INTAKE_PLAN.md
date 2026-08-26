# Messaging Intake — Build Plan

The design record for running the intake conversation over text. Companion to
`VOICE_INTAKE_PLAN.md`, not a replacement for it. Read the root `AGENTS.md` for
repo-wide conventions and `packages/core/juicebox/messaging/AGENTS.md` for the
rules that hold today; read this for why they hold.

## Status

**Web chat and Linq iMessage are both live.** The goal §0 settled on — a text
channel that runs the *same* intake conversation as the voice agent, writing the
same `CandidatePreference` — is what shipped. Everything that decides *what* to
ask is `juicebox/intake/` and is reused verbatim; `juicebox/messaging/` owns only
what is different about text.

Since the first pass, three things this document lists as unbuilt have been
built: quiet hours (`messaging/cadence.py`, deferring to the window through
`Job.run_at` rather than a scheduler), HELP alongside STOP
(`suppression.looks_like_help`), and the nginx rate limit on the public chat
routes. Proposing roles in a thread arrived later still, along with the
proactive sends in `messaging/notify.py`; both are documented in the package
file, not here.

**Nudging is still not built.** The budget that would govern one is in
`cadence.py` — `INITIATIVE_NUDGE`, `ORIGIN_NUDGE`, `REASON_NUDGE_BUDGET` — and
nothing outside that module references them, because nothing ever sends a
nudge.

**Twilio SMS is the outstanding channel**, and it is blocked on the legal entity
rather than on code — see "Phase 2" below and `deploy/NUMBER_CHECKLIST.md`.

---

## What is different about text, and where each answer lives

Everything that decides *what* to ask is `juicebox/intake/`, reused verbatim.
Four things do not carry over, and they are the whole design.

| Difference | Where it is handled |
|---|---|
| The gate decides **cadence**, not hangup | `conversation.run_turn` — one question per message, never two outbound in a row |
| A thread is not a call — it can be dormant for a week | `IntakeThread.record`, rewritten each turn so the gate can be read before every send |
| No recording consent, but TCPA and STOP | `messaging/suppression.py` wired into the existing `Suppression` list |
| Inbound webhooks are retried | `IntakeMessage.provider_native_id`, partial unique **per thread** |

### The four cadence rules, all enforced in code

1. **One question per message.** `next_questions` in effect at `limit=1`, and
   `prompts.valid_question` rejects a composed message carrying a second.
2. **Never two outbound in a row.** An outbound requires an inbound after the
   last one we sent. Exceptions: the greeting, and the STOP acknowledgement.
3. **Two asks per field, then abandon.** Never marked declined — nobody
   refused it, and an invented refusal is an invented answer.
4. **Enrichment is budgeted.** Six, against voice's fourteen.

None of these are prompt instructions. An instruction not to ask two things at
once is a hope; a check that rejects the message is a rule.

---

## Decisions taken

**Build, not buy, for web chat.** Evaluated as §4 asked. A chat vendor buys
much less than a voice vendor did, because telephony was the hard part of voice
and there is no equivalent here — we already own the gate, the extraction and
the record. Worse, every such platform ships its own slot-filling, and
persisting *their* vocabulary is the specific temptation `VOICE_INTAKE_PLAN.md`
warns against under "Provider adapter". The channel adapter still goes in, so
the decision stays reversible.

**Re-extract the whole transcript each turn, then merge forward.** More
expensive than extracting the new message alone, and it is what makes
"actually, make that 260" work — the correction is read in the context that
gives it meaning. `merge_record` then stops a quieter later pass from undoing an
earlier answer. `extract_intake` already takes `known=`, so nothing in
`intake/` had to change.

**The web route runs the turn synchronously.** Somebody is watching a typing
indicator, and `llm.json_fast` exists for exactly that. The work itself is
`run_turn`, which is also what `KIND_INTAKE_TURN` calls, so the inline path and
the queued path cannot drift. SMS, whose inbound arrives by webhook, will use
the job.

**Identity is asked in the thread and kept off the spec.** A caller identifies
themselves by their number; a stranger in a web chat identifies themselves by
nothing. `contact_name` and `contact_email` are columns on `IntakeThread`, not
keys in `spec` — `ParsedQuery` has no field for a name, and putting one there
would be a field `jobmatch.engine` cannot score.

**Two things sit outside their usual homes on purpose, and stay there.**
`messaging/prompts.py` and `messaging/schemas.py` would normally be
`llm/prompts.py` and `schemas.py`. The original reason was that the voice branch
was appending to both, with a plan to promote after the merge; that reason is
gone and the self-contained boundary turned out to be the better one. Settled —
do not re-litigate the move.

---

## What the build found

Three defects, all caught by running the thing rather than by the unit tests.

1. **`IntakeField.asks` is not a sendable question.** It is a topic line for a
   voice agent's checklist. The first live run asked "Where they are and which
   places they would work in." Fixed with `prompts.QUESTIONS`, kept honest by a
   test against `FIELDS`.

2. **The heuristic extractor read our own greeting.** It is regexes over the
   whole transcript with no notion of speaker, so it extracted a job title of
   *"take few details so recruiter can match you to right"* and a seniority of
   *"mid"* from a conversation in which the candidate had said only their name
   — closing two required gates on words we wrote. Fixed in
   `conversation._extract`: the model gets the whole thread, the heuristic gets
   `candidate_only=True`, and the degrade path is explicit so a failed model
   call cannot fall back to the heuristic still holding the full transcript.

3. **Message idempotency was org-scoped**, copied from `Message`. That is right
   for a mail provider's id and wrong for one the browser chooses: two
   strangers both sending `"1"` would have the second swallowed. Now scoped to
   the thread.

### Pre-existing, in shared code — since fixed, and worth keeping for the shape

`intake/extract.py:_heuristic_titles` leaked the verb phrase into the title when
the sentence started "I'm after a …":

```
"I'm after a senior machine learning engineer role."  -> ['m after machine learning engineer']
"I am after a senior machine learning engineer role." -> ['am after machine learning engineer']
"I want a staff backend engineer role."               -> ['backend engineer']       # correct
```

Untouched at the time because `intake/` was the voice branch's working area.
That reason expired when the branches merged and the defect did not — it
survived on `main` for months. It mattered more on this channel than on voice,
because text with no API key leans on the heuristic path by default, and a
title no posting carries is the field `search.scoring.assess` penalises
hardest.

**The reason it survived is the part worth remembering.** The last pattern in
`_TITLE_CAPTURES` has no left anchor — it takes whatever precedes "role" — so
*every* one of these sentences hands `normalize_title` the entire clause. The
third line came out right only because `TITLE_ALIASES` happens to hold "backend
engineer", so the progressive-suffix lookup rescued it. Nothing about the
capture was working; the alias table was covering for it one title at a time,
and the correct-looking neighbour is what made the broken one read as an
oddity rather than a bug. When a heuristic is right for some inputs and wrong
for others, check whether a lookup table is doing the work before concluding
the pattern is nearly right.

Fixed by trimming the captured phrase at the last word that cannot belong to a
title (`_TITLE_BREAK_WORDS`, `_title_phrase`), before the role-noun check
rather than after — so a phrase that loses its noun to the trim records
nothing.

---

## Definition of done (§8)

| # | Criterion | Where it is demonstrated |
|---|---|---|
| 1 | Stranger completes a text intake, producing a `CandidatePreference` | `test_a_finished_conversation_writes_a_candidate_preference`, plus a live run against the seeded DB |
| 2 | The gate paces it: one question per message, required first, stops when closed | `TestCadence` (5 tests) |
| 3 | A returning candidate resumes without being re-asked | `test_a_dormant_thread_resumes_where_it_stopped` |
| 4 | Suppression honoured — a do-not-contact candidate receives nothing | `TestSuppression` (5 tests) |
| 5 | A duplicated inbound is a no-op | `test_a_duplicate_client_message_id_is_a_no_op` |
| 6 | Suite green, ruff clean, migration drift passing | 858 passed, 5 skipped |

---

## Phase 2 — Linq iMessage (interim; Twilio SMS when 10DLC clears)

*(The Twilio half was "when Toll-Free Verification clears". The line moved to the
local `+1 (628) 386-5454` on 2026-08-23, so that path is A2P 10DLC — brand plus
campaign, under the legal entity. iMessage via Linq needs neither, which is why
it is the interim channel. See `deploy/NUMBER_CHECKLIST.md`.)*

**Linq is live in code:** `LinqChannel`, `messaging/linq.py`, `POST /v1/intake/linq/webhook`.
Tester-only — number not advertised. iMessage-only; Android is a known gap.

Shipped on the Linq path:

1. **`LinqChannel` + Standard Webhooks verify** — `message.received` only; org from
   `IntakeNumber` where `provider == "linq"`.
2. **Thread lookup by `(provider, provider_thread_id)` first**, then `from_address`.
3. **`KIND_INTAKE_TURN` enqueued**; never inline (Linq retries ~25 minutes).
4. **HELP / STOP** — keyword-only opt-out as today.
5. **Rate caps** — `MAX_INBOUND_PER_THREAD` and org thread cap include `linq_inbound`.
6. **Voice outbound fix** — `IntakeNumber.provider == "retell"` on dial-out query.

Twilio SMS remains deferred. The blocker moved with the number: the line is
the local `+1 (628) 386-5454`, so the path is **A2P 10DLC** — brand plus
campaign, under the legal entity — not Toll-Free Verification, and nothing can
be filed until the entity exists and the EIN has propagated
(`deploy/NUMBER_CHECKLIST.md`). A draft adapter is on
`origin/cursor/messaging-intake-sms-5fd9`. The seam is in; this is the
checklist:

1. **An `SmsChannel` implementing `MessageChannel`**, plus a webhook parser
   returning `(provider_native_id, from_address, to_address, body)`. It must
   supply the vendor's message id — a channel that cannot is a channel that
   cannot be idempotent. `requires_consent()` returns `True`.
2. **An unauthenticated inbound webhook**, org resolved from the number that
   was texted, never from the payload. `routers/track.py` remains the pattern;
   `routers/messaging.py` already follows it.
3. **Enqueue `KIND_INTAKE_TURN`** rather than running inline. Return 200 to the
   vendor immediately.
4. **Resolve the sender to a thread** — an active one for that number, or a new
   one. `intake.phones.lookup_key` matches the number to a candidate, and it
   refuses to guess a country code for the reason
   `packages/core/juicebox/intake/AGENTS.md` records: a wrong phone match
   discloses somebody else's record.
5. ~~**Close the suppression gap.**~~ Done. `Suppression` grew a phone column,
   `handle_stop` writes it and `is_blocked` reads it; the stopped-thread lookup
   stays as the second read, because `lookup_key` refuses to guess a country
   code and a number it cannot parse has no `Suppression` row.
6. ~~**HELP.**~~ Done — `suppression.looks_like_help`, carrier-mandated
   alongside STOP.
7. ~~**Quiet hours.**~~ Done — `messaging/cadence.py`, with the
   unknown-timezone window and the reply carve-out from
   `CANDIDATE_AGENT_PLAN.md` §3. Deferral reuses `Job.run_at`; there is no
   scheduler.

Items 1 through 4 are the ones that remain, and none of them can be exercised
until the 10DLC campaign clears.

## Before this reached production — both gates are closed

Kept because both are the kind of thing that gets re-opened.

1. **The chat routes are unauthenticated and have model spend behind them.**
   That is by design — a stranger on a careers page has no principal — and the
   signed widget token stops a forged org. What it does not stop is volume: the
   token is *meant* to be embedded in a public page, so it is readable in page
   source, and every inbound message costs an extraction call.
   `MAX_INBOUND_PER_THREAD` bounds one conversation, not how many a script can
   open. This is the billing-DoS shape `VOICE_INTAKE_PLAN.md` flags for voice
   minutes, arriving here as tokens.

   **Closed by a rate limit in `packages/web/nginx.conf`** — `limit_req_zone
   intake_chat` at 20r/m with `burst=10`, on a `location /v1/intake/chat/` block
   above `location /v1/` so it wins the prefix match. It is nginx rather than
   the edge because `docker-compose.prod.yml` runs the stock `caddy:2-alpine`
   and Caddy's `rate_limit` is a third-party module needing an `xcaddy` build,
   while `limit_req` is compiled into the stock nginx image. The
   `set_real_ip_from` / `real_ip_header` lines beside it are load-bearing:
   behind Caddy, `$binary_remote_addr` is the Caddy container's IP, so without
   them every visitor shares one bucket and the limit throttles everyone or
   nobody.

   The durable version is still a per-org thread-creation cap in the route,
   where it can be counted per tenant rather than per IP. Not built.

2. **Migration rehearsal.** `deploy/README.md` asks for a rehearsal against a
   restored dump on staging before merging to `main`. `b7e2c94af013` creates
   two tables and adds one column to `candidate_preferences` through
   `batch_alter_table` — on SQLite that rebuilds the table, on Postgres it is a
   plain `ALTER`. The Postgres path is the one to watch, and CI covers it. This
   is standing practice now, not a one-off: `deploy/STAGING.md`.

---

## The web chat surface

Web chat works end to end with or without an `ANTHROPIC_API_KEY`. A recruiter
gets a widget token from `GET /v1/intake/widget-token` (authenticated as
normal); the page posts to `POST /v1/intake/chat/sessions` and then
`.../messages`. A finished conversation writes a `CandidatePreference` that
`jobmatch.engine.score_pair` can read.

The merge-order reasoning that used to sit here is spent — both branches landed.
The one rule it produced outlived it and is now in the root `AGENTS.md`: one
Alembic revision per change, never stacked onto a mid-chain parent, and two
parallel branches' migrations are never combined into one.

---

## Decisions taken since the first pass

**Nudging: yes, at low frequency.** Confirmed by Tushar. Not yet implemented.
The wrinkle to settle first is that **web chat has no push** — a nudge on a web
thread is only seen if the person returns to the tab, so nudging is either
inert until SMS lands or it goes out by email through the existing outreach
stack, which is a different channel with its own consent question.

**Consent, from how the comparable products actually do it.** Poke, Instinct
and Tomo all avoid the hard case rather than solving it:

| Product | Mechanism |
|---|---|
| Poke (Interaction Co) | The user texts *them* first. Terms characterise the traffic as transactional, not promotional. Web signup ties the number, verification code proves control of it. |
| Instinct (Spear Street) | Invite-only. Number typed into a web form, which is where the disclosure sits; the invite gate also caps volume while sending reputation builds. |
| Tomo (Mapo Labs) | iMessage, no app download — sidesteps the carrier regime entirely. No 10DLC, no toll-free verification, no carrier filtering. |

None of them cold-text. In all three the person texting is the person who
signed up, which is *not* Juicebox's harder population: a candidate sourced
from Foray never asked us for anything. **Recommended posture: inbound-only
plus replies.** Consent for a sourced candidate is collected through a channel
we already have — the outreach email stack, or the voice call — and never by
texting them to ask.

---

## Open decisions

### Blocking — these change what gets built

1. ~~Sourced or inbound only?~~ — **decided 2026-08-23 (Tushar): both, mostly
   inbound.** So consent-provenance capture is in scope: source, timestamp, the
   exact disclosure text and its version. The rule that keeps "mostly inbound"
   architectural rather than aspirational is that first contact never happens by
   text — it happens on a channel we already have a basis for (outreach email,
   or the voice call now that `intake_calls.sms_opt_in` is captured), and the
   thread is opened by *their* reply. See `CANDIDATE_AGENT_PLAN.md` §2.
2. ~~One Juicebox number, or one per org?~~ — **decided 2026-08-23 (Tushar):
   one shared number for everything**, `+1 (628) 386-5454`, calls and texts
   alike. This takes on the cross-tenant reputation coupling knowingly, so the
   three mitigations are not optional: a per-org outbound budget in the send
   path, an opt-out-rate circuit breaker per org, and the org named in the first
   message of every thread — a shared number cannot identify the sender, so the
   message has to. `CANDIDATE_AGENT_PLAN.md` §2.
3. ~~**Nudge cadence.**~~ Settled and encoded: first nudge at 48h of silence,
   at most 2 per thread, at least 72h apart, never after a wrap-up or an
   opt-out. `cadence.py` enforces it. Nothing sends a nudge yet, so the budget
   has never been spent.
4. **Nudge channel for web threads** — email fallback through the outreach
   stack once `contact_email` is known, or leave nudging inert until SMS?

### Open — worth deciding, not blocking

5. **Opt-out detection is under-inclusive.** `suppression.looks_like_stop`
   matches whole-message keywords only, chosen so "stop me if you've heard this
   one" is not read as an opt-out. But the FCC's revocation rule expects *any
   reasonable expression* of opt-out to be honoured — "please stop texting me",
   "remove me" — not just the keyword. The fix is keyword match for the certain
   cases plus an intent classifier for the rest, and the precedent is the
   eight-intent reply classifier in `llm/prompts.py:REPLY_SYSTEM`, which §3 of
   the brief already pointed at. Roughly half a day. Confirm current TCPA
   specifics with counsel regardless — the consent and revocation rules have
   moved recently.
6. **Does a human get told when an intake lands?** Still nothing.
   `record_event` writes an audit row and stops there, and
   `messaging/notify.py` notifies the *candidate*, not a recruiter. Voice
   Phase 4 has "recruiter alert on material change"; the same need applies
   here.
7. **Should the candidate review what was captured?** Voice Phase 3 has a
   public review page. In chat it is more natural and nearly free — "here is
   what I have, anything wrong?" before wrap-up. It changes the definition of
   done, so it is a decision rather than an assumption.
8. **Where does the widget live?** Not `packages/web`, which is auth-gated and
   is not the marketing site. Is there a marketing repo to ship into, or should
   this be an embeddable `<script>` snippet handed over?
9. **Consent capture for SMS, concretely** — once (1) is answered. Web chat is
   self-evident; an outbound text needs consent recorded somewhere auditable
   before the first message. Widget, voice call, or recruiter attestation?
10. **Retention** for thread transcripts, alongside the voice recording policy.
11. **Whether `IntakeThread` and `IntakeCall` merge** into an `IntakeSession`
    with a `channel` column. Both branches landed, so the conflict argument for
    deferring it has expired; what remains is that a thread has no duration or
    recording and a call has no message counts, which is why
    `GET /v1/intake/threads` is a sibling of `/calls` rather than a widened
    response. Still worth doing, and a review queue should not be what forces
    it.
