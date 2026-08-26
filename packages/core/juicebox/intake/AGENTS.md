# Working in `intake/`

Read the root `AGENTS.md` first for repo-wide conventions, and
`docs/INTAKE_COMPLIANCE.md` before changing anything an agent says on a call —
that document is binding, and code and tests cite it by section.

`intake/` owns *what* to ask and the record both channels write.
`messaging/` owns what is different about text and has its own `AGENTS.md`.
Design record and phasing: `docs/VOICE_INTAKE_PLAN.md`.

## Voice intake (`packages/core/juicebox/intake`)

Candidate-side intake by phone: a web widget places a call, and a dial-in number
takes process updates. Retell is the agent platform, on a Twilio number
imported into it (+1 628 386-5454). Plan and phasing live in
`docs/VOICE_INTAKE_PLAN.md`. Non-obvious points:

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

The messaging notes already record that `messaging.records.persist_preference`
and `intake.service.upsert_preference` upsert the same row and never conflict
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

