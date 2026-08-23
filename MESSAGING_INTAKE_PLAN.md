# Messaging Intake — Build Plan

Companion to `VOICE_INTAKE_PLAN.md`, not a replacement for it. Read `AGENTS.md`
first for repo-wide conventions, and its "Messaging intake" section for the
non-obvious failure modes this build found.

## Status

**§0 confirmed with Tushar: a text channel that runs the same intake
conversation as the voice agent** — web chat first, SMS behind an adapter.
Not a recruiter texting inbox, and not post-call SMS follow-ups.

**Phase 1 complete.** Web chat is working end to end against the real seeded
database with no `ANTHROPIC_API_KEY`. Post-merge baseline: **1003 passed**, 5
skipped (after voice intake landed on `main`).

**Phase 2 code complete** (2026-08-23). Rate-limit gate, embeddable widget, and
SMS stack are built and tested. **Live SMS traffic still waits on Toll-Free
Verification** — set `SMS_ENABLED=true` only after TFV clears on +1 888 260-0142.

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

**Two things stay in this package on purpose.**
`messaging/prompts.py` and `messaging/schemas.py` were kept out of
`llm/prompts.py` and `schemas.py` during the voice merge to avoid conflicts.
After voice landed, the self-contained `messaging/` boundary is the better
home — do not promote them.

**Inbound-only SMS.** We never cold-text. Consent for a sourced candidate is
collected through email outreach or the voice call; an inbound text is prior
express consent and sets `consent_messaging=True` at thread open.

**Widget: embeddable bundle.** `packages/widget/` builds
`juicebox-intake-widget.js` — not a page in auth-gated `packages/web`.

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

## Phase 2 — SMS (code complete; TFV gates live traffic)

The seam is in; this is the checklist, not a redesign.

1. **An `SmsChannel` implementing `MessageChannel`**, plus a webhook parser
   returning `(provider_native_id, from_address, to_address, body)`. **Done**
   — `messaging/channels.py`, `messaging/sms.py`, `POST /v1/intake/sms/webhook`.
2. **An unauthenticated inbound webhook**, org resolved from the number that
   was texted, never from the payload. **Done** — `IntakeNumber.sms_enabled`
   must be true; voice-only numbers do not accept texts.
3. **Enqueue `KIND_INTAKE_TURN`** rather than running inline. Return 200 to the
   vendor immediately. **Done**.
4. **Resolve the sender to a thread** — an active one for that number, or a new
   one. **Done** — `find_active_thread`, `lookup_key` for candidate match.
5. **Phone column on `Suppression`.** **Done on `main`** (voice slice 2) before
   this branch merged. `handle_stop` now writes the phone too.
6. **HELP.** **Done** — whole-message keyword, canned reply, no turn enqueued.
7. **Quiet hours.** **Deferred until nudging lands.** Under inbound-only every
   outbound is a reply inside a conversation the candidate just started; TCPA
   quiet hours bind *initiated* messages. They become mandatory the moment
   nudging sends the first unsolicited text — see `AGENTS.md`.

### Production gates

1. **Rate limit.** **Done** — nginx `limit_req` on `/v1/intake/chat/` plus
   `MAX_NEW_THREADS_PER_ORG_PER_HOUR` in the route.
2. **Migration rehearsal.** Still required on staging before merge to `main`.
   Revisions: `b7e2c94af013` (threads), `f3a8d51b62c0` (`sms_enabled`).

---

## Handover

**Branch:** stacked on `feat/messaging-intake` after merging `main` (voice intake
landed as `fb2cc74`). Open as PRs against `feat/messaging-intake`, then one PR
to `main` after staging rehearsal.

**What works today:** web chat and SMS code paths, end to end in tests, with or
without an `ANTHROPIC_API_KEY`. Recruiter gets a widget token from
`GET /v1/intake/widget-token`; embed `packages/widget/dist/juicebox-intake-widget.js`
or use the test page at `/intake-chat-test.html`. SMS inbound is replay-tested via
`TestSmsWebhook`; live Twilio traffic waits on TFV + `SMS_ENABLED=true`.

**What is not built:** nudging (decided, not written), quiet hours (until
nudging), a candidate review step, and any recruiter alert when an intake lands.

**Merge order — satisfied.** Voice landed on `main`; messaging rebased with
`down_revision = c8e3f91d24b0`. Do not combine the migrations into one.

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

1. **Sourced or inbound only?** **Decided: inbound-only plus replies.** We never
   cold-text. Consent for a sourced candidate is collected through email
   outreach or the voice call; the inbound message is the artifact.
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
8. **Where does the widget live?** **Decided: `packages/widget/`** — an
   embeddable bundle (`juicebox-intake-widget.js`), not a page in auth-gated
   `packages/web`.
9. **Consent capture for SMS, concretely** — **Decided with (1).** Inbound-only:
   they texted us first, so `consent_messaging=True` at thread open. Outbound
   to sourced candidates waits on consent collected elsewhere.
10. **Retention** for thread transcripts, alongside the voice recording policy.
11. **Whether `IntakeThread` and `IntakeCall` merge** into an `IntakeSession`
    with a `channel` column once both branches land. Worth doing; a rename
    before the merge is a guaranteed conflict.
