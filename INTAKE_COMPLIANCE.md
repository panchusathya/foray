# Voice intake — compliance guardrails

One page. What the intake agents may not do, where each rule is enforced in
this repo, and the two decisions that still need an owner. "Enforced" here
means the rule lives in code or in the deployed agent definition
(`deploy/retell_agents.py`), not in a policy doc someone has to remember.

## 1. AI disclosure and recording consent

Both agents identify as AI in the first breath and ask for recording consent
before any substantive question. If the caller declines: outbound ends the
call politely, inbound takes a message. Neither proceeds with intake.

- Consent is captured **per call** — two-party-consent states (California
  included) make "they agreed last time" the wrong answer.
- It reaches the record via post-call analysis and is **latched true only**
  (`service._analysis_flag`): a later webhook omitting the field cannot
  silently withdraw a consent, and a call with no recorded consent reads as
  not consented (`intake_calls.consent_recording`, default false).
- If a caller asks "are you a real person?" or "is this recorded?", the agent
  answers truthfully. Never deflects.

## 2. Salary history — never asked, never used

Expectations only, forward-looking. The agent never asks current or past
compensation (illegal to ask in California, Colorado, New York, and a growing
list of states). If a caller volunteers it: not repeated back, not used to
steer, not recorded as a preference. If asked what a role pays, the agent
points to the recruiter — and notes that California candidates are entitled to
the pay scale on request.

## 3. Work authorization — exactly two questions

"Are you legally authorized to work in the US?" and "Will you need sponsorship,
now or in the future?" Nothing else: no citizenship, visa type, green-card
status, or country of origin, and no follow-ups if volunteered. These two are
the questions EEOC guidance permits; the rest is national-origin exposure.

## 4. Protected categories

Never asked: age, race, religion, health, disability, family or marital
status, pregnancy, or anything else protected. Volunteered protected
information gets a neutral acknowledgment, no follow-up, and no place in the
extraction — the extraction schema (`intake/schema.py`) has no field it could
land in, which is the stronger guarantee.

## 5. The intake channel evaluates nobody

The agents collect what the candidate wants; they never say or imply how
strong a candidate is, never promise or deny advancement, never compare
candidates. The extraction emits preferences in `ParsedQuery` vocabulary —
no score, no rank, no recommendation field exists in the intake path.

**Boundary to keep an eye on:** the platform's matching engine
(`jobmatch/engine.py:score_pair`) does score candidates against job specs.
That is recruiter-facing decision support, a separate surface from intake —
but if it is ever used to substantially assist hiring decisions for NYC roles,
it is in NYC LL144 territory (bias audit + candidate notice), and the EU AI
Act classifies employment-screening AI as high-risk. Keep the intake channel
evaluation-free, and give the matching engine its own review before it drives
decisions rather than suggestions.

## 6. SMS — TCPA consent, captured now, used later

The agent asks at wrap-up, verbatim: *"Can I text you updates at this number?"*

- Only an explicit, unambiguous yes counts. Hesitation, "I guess", or "email
  is better" is recorded as no. The agent never re-asks or reframes to convert
  a soft answer — a soft yes looks like consent in the database and is not one
  in court.
- The flag is `intake_calls.sms_opt_in`, latched true only. The verbatim
  exchange and its timestamp live in the transcript on the same row: the call
  row is the evidence record.
- Opt-out is a person replying STOP — never a webhook omitting a field.
- Consent is for **application updates**. It does not cover marketing, and the
  10DLC campaign registration will be granted on that use case (see
  `deploy/NUMBER_CHECKLIST.md`).

## 6a. Why the opt-out recitals were removed — and what that costs

Every greeting and the voice wrap-up used to carry a sentence about stopping:
*"you can stop any time"* on the web and LinkedIn-first threads, *"Reply STOP
any time to opt out, HELP for info"* on the candidate-agent greeting, and *"You
can reply STOP any time to opt out"* after the voice consent question. All four
are gone. **Only the sentences are gone.** STOP still stops, HELP still answers,
`handle_stop` still writes the suppression row and the stopped thread, and the
single STOP acknowledgement a carrier expects still goes out. Removing a
sentence about a mechanism is not removing the mechanism, and
`TestKeywordsStillWork` in `test_intake_consent_copy.py` exists so that
distinction cannot quietly collapse.

**The argument for removing them.** An opt-out recital in a *first* message is
the single strongest cue that what follows is bulk. Candidates read it as a
marketing blast, which is the read that produces the complaint the recital was
supposed to prevent — so on a one-to-one conversational channel it is
self-defeating. On the voice call it is worse than self-defeating: it lands
immediately after the consent question, so the last thing the candidate hears
before answering is a reason to say no, and the question stops sounding like a
question.

**The exposure this creates, stated plainly.** This is a judgement call, not a
free win, and it cuts against two things previously written down here:

- `CANDIDATE_AGENT_PLAN` §2 asked the candidate-agent greeting to carry three
  things — who we are, that this is an AI, and how to stop. It now carries two.
  A shared sender serves every tenant, so the candidate cannot identify us from
  the number, and a person who wants out and does not remember the keyword has
  no in-message answer. Their remaining options are replying anything at all
  (the agent handles "stop messaging me" as an intent, but that is a model
  reading, not a carrier keyword) or ignoring us.
- **Carrier review may reject this.** 10DLC campaign review and the periodic
  audits that follow look for opt-out language in message content, and some
  vetting flows treat its absence in a first message as a fail regardless of
  channel semantics. Complaint rate is what actually gets a shared sender
  throttled, and the hypothesis here — that no recital produces *fewer*
  complaints than one — is unmeasured. If it is wrong, it is wrong in the
  direction of a number that stops delivering for every tenant on it.

**Therefore:** this is a reversible copy change with no schema behind it, and it
must be re-examined before SMS goes live rather than after. `SMS_ENABLEMENT.md`
carries that item. If 10DLC review asks for opt-out language, put it back — in
the *first outbound message only*, not in every greeting, and not in the voice
wrap-up, which is not an SMS at all.

**Disclosure records are versioned, so nothing about the past is rewritten.**
`web-widget-v2`, `inbound-text-v2` and `voice-wrapup-v2` describe the new
wording; rows written under `-v1` keep describing what those people actually
saw. That is the whole reason `ConsentRecord` stores the disclosure text instead
of a pointer to it.

## 7. Data handling

- Transcripts and recordings live on `intake_calls`, org-scoped; org
  resolution comes from the number dialled or a signed widget token, never
  from the webhook body.
- Retell processes every call. **Open decision #1:** DPA signed, retention
  terms in writing, and the PII-scrubbing add-on accepted or declined — before
  volume.
- **Open decision #2:** our own retention window for transcripts of callers
  who never become candidates. Today they are kept indefinitely because an
  unattributable call is "exactly the call worth looking at"; that is a
  reasonable engineering default and an unexamined privacy position.

## 8. Minors

A caller who is clearly a minor gets no intake: message taken, recruiter
follows up, call ends.

## 9. Connected apps (Gmail / Outlook / GitHub)

Onboarding may offer "connect one of your apps" instead of asking for a
LinkedIn URL or resume. The posture is targeted extraction, not indexing, and
it is enforced structurally, not editorially:

- **Scopes requested**, and nothing wider: Google `gmail.readonly` (plus
  openid/email/profile), Microsoft `Mail.Read` + `User.Read` (a separate app
  registration from the recruiter mailbox one — never `Mail.Send`), GitHub
  `read:user`. Candidate connector credentials are distinct env vars from
  every other OAuth client in the deployment.
- **Queries run** are a fixed list in code (`connectors/google.py`
  `RESUME_QUERIES`, the two-folder walk in `connectors/microsoft.py`):
  attachment-bearing mail, their own sent items first. Adding a query is a
  privacy-posture change and must update this section.
- **What is stored**: one resume file (a `CandidateDocument`, the same row an
  upload produces), derived profile facts in the thread record, and counts in
  `candidate_connections.index_state`. The connector DTOs have no field for a
  subject, body or snippet, and a test pins that absence — the same rule
  `discovered_contacts` follows. Mail bodies are never read for extraction;
  losing attachments' bytes are sniffed and discarded.
- **Consent**: recorded at the OAuth callback as a `ConsentRecord`
  (`scope="app_connect.<provider>"`) carrying the connect page's disclosure
  verbatim, versioned (`connect-v1`). Disconnect stamps `revoked_at`, wipes
  our token copy, and revokes upstream where the provider supports it
  (Google, GitHub; Microsoft exposes no endpoint — the page says our copy is
  deleted, which is the true claim).
- **Tokens** are Fernet-encrypted at rest (`integrations/crypto.py`), indexed
  once at connect time, and re-read only on an explicit re-index. There is no
  sync loop over candidate mailboxes.
- **No EEO inference** from anything a connector returns, same as §4; no
  candidate-facing scores, same as §5.

**Ops gate, not a code gate:** Google's `gmail.readonly` is a restricted
scope. Past Testing mode (≤100 test users) it requires app verification plus
an annual CASA Tier 2 assessment — weeks-to-months of lead time. The pilot
runs in Testing mode; Outlook and GitHub can ship to production first, since
`is_configured()` per provider decides what the conversation offers.
