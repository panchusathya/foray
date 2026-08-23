# Messaging Intake — Brief for the Parallel Build

You are building a **messaging channel for candidate intake**, on top of the
voice intake work already committed on this branch's parent. A separate instance
is concurrently building the voice side (Phase 2 of `VOICE_INTAKE_PLAN.md`) in
`C:\Users\TusharGupta\juicebox` on `feat/voice-intake`. You are on
`feat/messaging-intake`. Both branches share history at commit `d481e52`.

**Read first:** `AGENTS.md` (repo-wide conventions, and the "Voice intake"
section at the end), `README.md`, then `VOICE_INTAKE_PLAN.md`.

---

## 0. Confirm the goal before building

"A messaging app on top of the voice functionality" has more than one reading.
The one this brief is written for, because it reuses the most and duplicates
nothing:

> **A text channel that runs the same intake conversation as the voice agent** —
> web chat first, SMS later — producing the same `CandidatePreference` record.

Two other plausible readings, which would change the work substantially:

- A recruiter-facing inbox for texting candidates manually (that is closer to
  extending the existing `outreach` stack than to intake).
- Automated SMS follow-ups after a call (e.g. delivering the profile-review
  link), which is a small feature of the voice plan rather than an app.

**Ask the user which one before writing code.** If it is the first reading,
everything below applies.

---

## 1. The thing you must not rebuild

`packages/core/juicebox/intake/` is already written, tested (69 tests) and
**channel-agnostic**. A message thread is just a slower transcript.

| Module | Reuse verbatim |
|---|---|
| `intake/schema.py` | The 25-field set, the required/enrichment tiers, `coverage()`, `next_questions()`, `empty_record()` |
| `intake/extract.py` | `extract_intake()` (transcript → record), `normalize_record()`, `diff_records()` |
| `intake/phones.py` | `lookup_key()` for matching an inbound SMS sender to a candidate |

**Do not add fields to the intake schema without reading
§"Schema decision" of `VOICE_INTAKE_PLAN.md` first.** The preference `spec` is
deliberately `ParsedQuery`-shaped and normalised through `enrich/taxonomy.py`,
because `jobmatch.spec` compiles job descriptions into that same shape and
`jobmatch.engine.score_pair` scores against it. A preference field in any other
vocabulary cannot be scored against a posting and silently degrades into notes.
If a messaging-only field is genuinely needed, it goes on the preference row as
an extension field, never inside `spec`.

Every field in `schema.py` carries the client question it answers. That is the
admission price for a new field, not documentation.

---

## 2. What is actually different about messaging

This is the design work. Four things do not carry over from voice.

**The gate changes job.** In voice, `coverage().required_complete` decides when to
hang up. In messaging there is nothing to hang up — a thread stays open for days.
So the gate decides **cadence**: whether to send another question now, hold, or
stop. Voice's failure mode is talking too long; messaging's is sending too many
messages, and it gets you blocked rather than merely disliked. Use
`next_questions(record, limit=1)` per outbound message, not a batch.

**A thread is not a call.** `IntakeCall` assumes one bounded session with a
duration, a transcript and a recording. A thread has many inbound events, can go
dormant for a week and resume, and has no duration. **Add a sibling table
(`IntakeThread` + `IntakeMessage`) rather than renaming `IntakeCall`** — a rename
guarantees a merge conflict with the concurrent voice branch. Unifying both into
an `IntakeSession` with a `channel` column is a good refactor *after* both
branches merge, not before.

`CandidatePreference.source_call_id` is an FK to `intake_calls`. Add a nullable
`source_thread_id` sibling rather than making it polymorphic.

**Consent is a different problem.** No recording consent to capture — but SMS
brings TCPA: you need prior express consent before the first message, and
honouring `STOP` is mandatory and immediate. The repo already has the mechanism:
`Suppression` + `outreach/suppressions.py` (`is_suppressed`, `suppress`). Wiring
into it is **not optional**. A candidate on the do-not-contact list must not
receive an intake text.

**Idempotency is the same problem with a different name.** Inbound message
webhooks get retried and duplicated exactly like call webhooks. The precedent is
`Message.provider_native_id` — read the `AGENTS.md` note on it. A provider that
cannot supply its own message id cannot be idempotent.

---

## 3. Reuse the messaging stack that already exists

The repo already has a full outbound-messaging system for email. Do not build a
parallel one. Study and extend:

- `juicebox/models.py` → `Message`, `Suppression`
- `juicebox/integrations/providers.py` → the `MailProvider` interface and how a
  provider is registered. **This is the pattern for an SMS provider.** Read the
  `AGENTS.md` "Mail providers" section: no provider wire format may escape its
  adapter, and reply matching / intent classification / suppression are shared.
- `juicebox/integrations/matching.py` → reply matching, and the thread-sync
  privacy guard
- `juicebox/outreach/` → `sequencer.py` for how scheduled sends and deferral work
- `juicebox/jobs.py` + `worker.py` → the durable job queue. Anything that sends
  or extracts belongs in a job, not in a webhook handler.

Reply intent classification already exists (eight intents, `llm/prompts.py`
`REPLY_SYSTEM`). An intake thread needs something similar but different — "this
message answers `timeline`" rather than "this reply is a decline". Consider
whether to extend or add alongside.

---

## 4. Start with web chat, not SMS

The provisioned number `+1 (888) 260-0142` is **toll-free**, and the Retell API
confirms `custom_sms_enabled: false` — **SMS is not available on it today.**
Toll-free SMS also requires Toll-Free Verification (not A2P 10DLC), which has
real lead time and has not been started. So web chat is not merely the
recommended first channel, it is the only one available. So:

1. **Web chat first.** No carrier gating, works today, and it is the channel the
   website widget can offer next to the "call me" button. For a chat channel you
   may not need a third-party platform at all — we already own the gate and the
   extraction, and `llm.json()` can drive the conversation directly. Evaluate
   build-vs-buy explicitly; Retell does have chat agents, but a third party here
   buys less than it did for voice, where telephony was the hard part.
2. **SMS behind a channel adapter**, so it drops in when verification clears.

Everything must still work with **no `ANTHROPIC_API_KEY`** — heuristic fallback
for every model path, degrade with a warning, never fail. That is a hard repo
constraint, not a preference.

---

## 5. Guardrails (identical to voice, non-negotiable)

- No fit/quality scoring output. Intake collects; the moment it emits an
  evaluative judgement used in screening, NYC Local Law 144 AEDT obligations
  attach.
- No EEO-protected attributes collected, and if volunteered, not stored in
  extractable fields.
- `confidential_items` never appears in any client-facing view.
- Never promise a role, name clients unprompted, or give legal/visa advice.
- A declined field is an answer. Do not re-ask it on the next session.

---

## 6. Merge discipline — read this before your first commit

The voice branch is concurrently editing these files. Keep your changes out of
them where you can, and expect small mechanical conflicts where you cannot:

| File | Why it will conflict |
|---|---|
| `juicebox/main.py` | Router registration, two lines |
| `juicebox/jobs.py` | New `KIND_*` constants |
| `juicebox/worker.py` | New job-kind dispatch |
| `juicebox/config.py` | New provider settings |
| `juicebox/intake/` | Voice branch is adding a provider adapter here |
| `AGENTS.md`, `VOICE_INTAKE_PLAN.md` | Both branches document |

**Alembic will conflict, and it is the one that is not mechanical.** Migration
history is linear, and both branches will add a revision whose `down_revision`
is `c1a7f4e05b93`. Whoever merges second must rebase their migration's
`down_revision` onto the new head and re-run `pytest tests/test_migrations.py`,
which fails on drift. Do not "fix" it by merging the two migrations into one.

Put new code in a **new package** — `juicebox/messaging/` — so the bulk of your
work cannot conflict at all.

---

## 7. Environment setup in this worktree

This is a git worktree, so it has its own working directory but shares the repo.
It has **no `.venv`** and **no `.env`** (gitignored, so not carried over):

```bash
cd C:/Users/TusharGupta/juicebox-messaging
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e "packages/core[dev]"
./.venv/Scripts/python.exe -m juicebox.cli seed      # demo workspace
```

Then, per `AGENTS.md`:

```bash
cd packages/core && ../../.venv/Scripts/python.exe -m pytest -q   # 808 pass + 69 intake
../../.venv/Scripts/python.exe -m ruff check packages/core eval   # must stay clean
```

Any API keys you need go in a `.env` you create here; do not copy secrets between
worktrees in commits.

---

## 8. Definition of done for the first slice

1. A stranger completes a text-based intake in web chat and the thread produces a
   schema-valid `CandidatePreference` — the *same* record shape a call produces.
2. The coverage gate paces the conversation: one question per message, required
   tier first, and it stops asking once the gate closes.
3. A returning candidate resumes a dormant thread without being re-asked
   anything already answered or declined.
4. Suppression is honoured: a do-not-contact candidate receives nothing.
5. A duplicated inbound webhook is a no-op.
6. Full suite green, ruff clean, migration drift test passing.
