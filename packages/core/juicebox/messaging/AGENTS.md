# Working in `messaging/`

Read the root `AGENTS.md` first for repo-wide conventions, and
`docs/INTAKE_COMPLIANCE.md` before changing any candidate-facing copy — §6a in
particular records why the opt-out recitals were removed and what that costs;
several tests cite it.

Everything that decides *what* to ask is `intake/` and is shared verbatim; this
package owns only what is different about text. Design record and phasing:
`docs/MESSAGING_INTAKE_PLAN.md`, and `docs/CANDIDATE_AGENT_PLAN.md` §3–§4 for
quiet hours and the three application budgets.

## Messaging intake (`packages/core/juicebox/messaging`)

Candidate intake over text: a web chat widget now, Linq iMessage as the interim
text channel (tester-only, inbound-first), with Twilio SMS deferred until 10DLC
registration clears. It runs the *same* conversation as
the voice agent and writes the same `CandidatePreference`; everything that
decides **what** to ask is `juicebox/intake/` and is shared verbatim. This
package owns only what is different about text. Plan and phasing live in
`docs/MESSAGING_INTAKE_PLAN.md`. Non-obvious points:

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

### Proposing a role in a thread (`messaging/cards.py`, `presentation/cardimage.py`)

- **The text card is the message; the image is added to it.** A role used to go
  out as a media-only vCard bubble reading `[contact card] Title - Company`,
  with a tail saying "tap a card for details" -- so everything that made the
  role worth considering lived inside an attachment, reading it took a tap, and
  acting on it took a link. Now `render_card` always produces the body and
  `cards.card_media` adds a rendered PNG where one will render inline. Both
  halves ride one message, because a tapback resolves to a single message id:
  split them and half the thumbs-up gestures land on a bubble holding no
  proposal.

- **`last_service` gates the image, and empty is not SMS.** An image over SMS is
  an MMS -- carrier cost, recompression, and some carriers strip it -- so only
  `iMessage` and `RCS` get one. Empty means Linq never told us, which is a third
  state: presentation falls back to text for both, but a guess either way is
  wrong about half the time and bills MMS for half of those. The message's
  service beats the chat's, because an iMessage thread drops to SMS per-message
  often enough that only the last delivery is evidence.

- **Every failure in the card path returns "no media", never raises.** A role
  the candidate never hears about is far worse than a role without a picture.
  `card_version` on `job_card_assets` is what makes the cache safe to keep: one
  posting renders and uploads once for every candidate shown it, and a bump
  re-renders rather than serving pixels the current code would not produce. A
  refused upload stores the render with a *blank* version, so the bytes are not
  rebuilt but the upload is retried -- storing the version there would cache a
  row whose attachment id is empty and attach nothing forever.

- **Nothing in a card asks the candidate to leave.** The posting URL was dropped
  from the body: tapping it landed them on the employer's own form, which is the
  job we offered to do for them. The tail is the two gestures and then the
  dashboard, which is for tracking what is in flight -- not for reading these or
  applying to them. `url` stays on the projection for recruiter-side views.

- **The two tapback directions are deliberately asymmetrical.** A yes (`like`,
  `love`) files an application that cannot be unsent, so it goes through a job
  with every rail. A no (`dislike` only) is a row update that sends nothing --
  a confirmation bubble per dismissed role is noise, and the tapback is already
  on the candidate's own screen. `question` is a question, not a refusal.
  `reaction.removed` counts as neither: taking a thumb back is the absence of a
  decision, and reading it as one invents intent out of a corrected mis-tap. An
  already-answered proposal is never flipped, or a filed application would hang
  off a proposal the record says was refused. The decline lands on
  `JobProposal`, never `JobMatch.status` -- the proposal is the candidate-consent
  axis -- and nothing extra was needed to stop the role returning, because
  `eligible_matches` already skips any posting with a proposal of any status.

- **An answer belongs to the application that asked for it**
  (`manager._absorb_application_answer`). `ask_for_missing` marks the rows it
  asked about; before this nothing on the messaging side read them back, so an
  answer to "what should we put for comp expectations?" got the generic "noted,
  thanks. anything else you want me looking for?" -- no role named, and an
  invitation to start a fresh search over an application still waiting on the
  candidate. A following "no" then read as declining a job search. Worse, and
  the half that mattered: `refill_from_preference` was only ever called from the
  portal route, so answering over text folded nothing into the form. The branch
  runs before the no-command intents, requires a row that was actually asked
  about (or it becomes a catch-all), distinguishes "understood" from "did not
  catch that", and retries the submission once the answers are complete --
  the first attempt ran at package time, before the answers existed. `INTENT_AFFIRM`
  stays ahead of it: a bare "yes" against "sponsorship needed, or are you
  authorised?" could be either half, and the extractor cannot tell either.

- **`asked_blanks` is not `blank_answers`.** The latter reads a key *absent*
  from `form_answers` as blank, which is right for what a package still owes and
  wrong for whether a conversation is finished -- a key nobody was asked about
  would keep the set from ever reading complete, so the acknowledgement would
  say "one more thing" forever and the submission retry would never fire.

- **Portal links are a short code, not a JWT** (`portal/links.py`). The URL is
  `/m/<20 hex chars>`; it used to carry a full signed token, which made every
  texted link several hundred characters of bearer credential travelling into
  screenshots, forwards and preview fetches. The signature was doing no work
  the row was not: `consumed_at` is what makes a link single-use. `next_path`
  moved onto the row for the same reason it was never a query parameter --
  where somebody is going should not survive a forward -- and `safe_next`
  refuses anything that is not a same-origin path, on the way in *and* out. The
  legacy JWT branch in `exchange` exists only for links already in candidates'
  phones and is deletable one `PORTAL_LINK_TTL_SECONDS` after shipping.

## Consent copy (`messaging/prompts.py`, `messaging/consent.py`)

- **No greeting recites the opt-out any more, and that is a deliberate trade
  with a documented cost.** "you can stop any time", "Reply STOP any time to
  opt out, HELP for info", and the sentence after the voice consent question
  are all gone. `docs/INTAKE_COMPLIANCE.md` §6a carries the argument (an opt-out
  recital in a *first* message is the strongest available cue that what follows
  is bulk, which produces the complaint it was meant to prevent) **and the
  exposure** (it cuts against `CANDIDATE_AGENT_PLAN` §2's three-disclosure
  rule, and 10DLC campaign review may simply reject a first message without
  opt-out language, which throttles a shared sender for every tenant on it).
  `docs/SMS_ENABLEMENT.md` makes re-reviewing it a blocker on enablement rather
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

