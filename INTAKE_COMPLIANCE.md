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

The agent asks at wrap-up, verbatim: *"Can I text you updates at this number?
You can reply STOP any time to opt out."*

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
