# Messaging Intake — Build Plan

Companion to `VOICE_INTAKE_PLAN.md`, not a replacement for it. Read `AGENTS.md`
first for repo-wide conventions, and its "Messaging intake" section for the
non-obvious failure modes this build found.

## Status

**§0 confirmed with Tushar: a text channel that runs the same intake
conversation as the voice agent** — web chat first, SMS behind an adapter.
Not a recruiter texting inbox, and not post-call SMS follow-ups.

**Phase 1 complete.** Web chat is working end to end against the real seeded
database with no `ANTHROPIC_API_KEY`. 50 new tests, full suite green
(858 passed, 5 skipped), ruff clean, migration drift test passing.

Shipped:

- `juicebox/messaging/` — a new package, so the bulk of this cannot conflict
  with the voice branch.
  - `channels.py` — `MessageChannel` protocol + registry, `WebChatChannel`,
    `FakeChannel`. The `MailProvider` pattern applied to messaging.
  - `threads.py` — thread and message persistence, `seq` ordering, inbound
    idempotency, transcript rendering.
  - `conversation.py` — `run_turn`, the cadence rules, extraction routing.
  - `records.py` — the monotonic merge and the `CandidatePreference` upsert.
  - `identity.py` — contact name and address, kept off the spec.
  - `suppression.py` — do-not-contact and STOP.
  - `prompts.py` — candidate-facing question wording and the compose prompt.
  - `schemas.py` — request/response models.
- `IntakeThread`, `IntakeMessage`, `CandidatePreference.source_thread_id`
  + migration `b7e2c94af013`.
- `routers/messaging.py` — one authenticated route for the widget token, three
  unauthenticated chat routes following `track.py`.
- `security.signed_intake_token` / `verify_intake_token` — purpose-scoped HMAC.
- `KIND_INTAKE_TURN` + worker handler, the path SMS will use.
- `tests/test_messaging.py`.

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

**Two things sit outside their usual homes on purpose.**
`messaging/prompts.py` and `messaging/schemas.py` would normally be
`llm/prompts.py` and `schemas.py`, both of which the voice branch is appending
to. Promote after the merge.

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

### Pre-existing, in shared code — for the voice branch, not fixed here

`intake/extract.py:_heuristic_titles` leaks the verb phrase into the title when
the sentence starts "I'm after a …":

```
"I'm after a senior machine learning engineer role."  -> ['m after machine learning engineer']
"I am after a senior machine learning engineer role." -> ['am after machine learning engineer']
"I want a staff backend engineer role."               -> ['backend engineer']       # correct
```

Untouched here because `intake/` is the voice branch's working area. It matters
more on this channel than on voice, because text with no API key leans on the
heuristic path by default.

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

## Phase 2 — SMS, when Toll-Free Verification clears

The seam is in; this is the checklist, not a redesign.

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
   refuses to guess a country code for the reason `AGENTS.md` records: a wrong
   phone match discloses somebody else's record.
5. **Close the suppression gap.** `Suppression` is keyed on email. The stopped-
   thread lookup covers a stranger's STOP, but the durable fix is a phone
   column, which touches a table the voice branch also writes to — hence
   deferred, and it must land before the first outbound text.
6. **HELP.** Carrier-mandated alongside STOP, and not implemented.
7. **Quiet hours.** TCPA is 8am–9pm in the *recipient's* local time, which
   means capturing a timezone. Web chat needs none of this; SMS cannot ship
   without it.

## Before this reaches production

Two gates, neither optional. Both are why the branch is a PR rather than a merge.

1. **The chat routes are unauthenticated and have model spend behind them.**
   That is by design — a stranger on a careers page has no principal — and the
   signed widget token stops a forged org. What it does not stop is volume: the
   token is *meant* to be embedded in a public page, so it is readable in page
   source, and every inbound message costs an extraction call.
   `MAX_INBOUND_PER_THREAD` bounds one conversation, not how many a script can
   open. This is the billing-DoS shape `VOICE_INTAKE_PLAN.md` flags for voice
   minutes, arriving here as tokens.

   Cheapest fix is at the edge, where there is already a precedent:
   `deploy/Caddyfile` blocks `/v1/auth/signup` for the same class of reason. A
   rate limit on `/v1/intake/chat/*` costs no application code. The durable
   version is a per-org thread-creation cap in the route.

   **Until one of those is in, do not point a public widget at this.** The
   routes are harmless while nothing links to them, and the recruiter-facing
   `/v1/intake/widget-token` is authenticated as normal.

2. **Migration rehearsal.** `deploy/README.md` asks for a rehearsal against a
   restored dump on staging before merging to `main`. `b7e2c94af013` creates
   two tables and adds one column to `candidate_preferences` through
   `batch_alter_table` — on SQLite that rebuilds the table, on Postgres it is a
   plain `ALTER`. The Postgres path is the one to watch, and CI covers it.

---

## Handover

**Branch:** `feat/messaging-intake`, off `d481e52`. Open as a PR against `main`,
not merged — see the two gates above, and the merge-order note below.

**What works today:** web chat, end to end, with or without an
`ANTHROPIC_API_KEY`. Recruiter gets a widget token from
`GET /v1/intake/widget-token`; the page posts to `POST /v1/intake/chat/sessions`
and then `.../messages`. A finished conversation writes a `CandidatePreference`
that `jobmatch.engine.score_pair` can read.

**What is not built:** SMS (the number has no SMS), nudging (decided, not
written), HELP, quiet hours, a candidate review step, and any recruiter alert
when an intake lands.

**Merge order matters.** The voice branch is concurrently adding an Alembic
revision whose `down_revision` is also `c1a7f4e05b93`. Whoever merges second
rebases theirs onto the new head and re-runs `tests/test_migrations.py`. Do not
combine the two migrations into one. Nothing else in this branch touches
`intake/` or `config.py`, so the rest should merge mechanically.

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

1. **Sourced or inbound only?** Will we ever initiate a text to a candidate who
   did not ask? If never, written-consent capture disappears and this gets
   materially simpler and safer. If sometimes, we need a consent-provenance
   record — source, timestamp, the exact disclosure text shown and its version
   — because in a dispute the consent artifact *is* the defence.
2. **One Juicebox number, or one per org?** The biggest hidden fork, because
   Juicebox is org-scoped. A shared number means candidates get texts from a
   number they do not recognise and one tenant's complaint rate degrades
   delivery for every other tenant. Per-org means a provisioning and
   brand-registration workflow per customer, with weeks of lead time each.
3. **Nudge cadence.** Proposed and awaiting confirmation: first nudge at 48h of
   silence, at most 2 per thread, at least 72h apart, never after a wrap-up or
   an opt-out.
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
6. **Does a human get told when an intake lands?** Nothing does today.
   `record_event` writes an audit row and stops there. Voice Phase 4 has
   "recruiter alert on material change"; the same need applies here.
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
    with a `channel` column once both branches land. Worth doing; a rename
    before the merge is a guaranteed conflict.
