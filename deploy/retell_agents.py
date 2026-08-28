#!/usr/bin/env python3
"""Create or update the two Retell agents and point them at our endpoints.

Standard library only, like the rest of ``deploy/``.

**A script rather than a handful of curl calls, because the URL moves.** During
development the endpoints sit behind an ephemeral tunnel that changes every time
it restarts; in production they are on ``app.goforay.io``. Re-pointing has to be
one command, or the agents quietly keep calling a dead host and the failure looks
like an agent that has stopped asking questions.

Idempotent on ``agent_name``: run it again and it updates in place rather than
filling the workspace with duplicates.

Two agents, because Retell allows one inbound and one outbound per number and
they are genuinely different conversations:

* **outbound** — the website "call me" flow. A first full intake, driven by the
  coverage gate.
* **inbound** — the published status line. Someone calling us back, usually to
  say something changed.

Both call ``check_coverage`` mid-call. That tool is the reason a cooperative
call ends in two or three minutes: it answers "what do I still need, and may I
wrap up?" from the transcript so far, so the model never has to judge how long
it has been talking.

Both also call ``preview_matches`` once, at wrap-up: the server ranks the open
jobs book against what the caller just said they wanted and hands back finished
sentences -- the two or three roles worth naming, a composed count of the rest,
and how to offer the full list by text. Composed server-side for the same
reason ``progress_line`` is: a company name, a pay figure, or a count the model
invents is exactly the claim this call cannot afford.

**The inbound agent knows who is calling before it speaks.** Retell fires a
``call_inbound`` webhook while the phone is ringing and interpolates whatever
that response returns into the prompt, so ``/v1/intake/webhook`` resolves the
caller from caller ID and hands back ``caller_known``, their first name, their
record and what we still owe them. The prompt branches on it and never asks.
That is why the inbound prompt is full of ``{{caller_*}}`` slots where it used
to be a flat script, and why ``default_dynamic_variables`` matters: if the
webhook does not answer in time, those defaults are what stop the agent reading
the placeholders out loud.

Usage:

    python deploy/retell_agents.py plan   --base-url https://example.com
    python deploy/retell_agents.py apply  --base-url https://example.com
    python deploy/retell_agents.py list
    python deploy/retell_agents.py import --number +16283865454   # own-Twilio number
    python deploy/retell_agents.py attach --number +16283865454 --base-url https://example.com

``attach`` is separate on purpose. Creating agents changes nothing a caller can
reach; attaching one to a number makes a bot answer real people.

It takes ``--base-url`` because attaching is also the moment the number's
``inbound_webhook_url`` has to exist. ``apply`` sets that too, but it can only
reach numbers the inbound agent is already attached to -- so on a first setup
``apply`` runs before ``attach``, finds nothing, and the hook is never written.
The line then answers every returning candidate as a stranger, with no error
anywhere. ``list`` prints the hook per number so that state is visible.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
API = "https://api.retellai.com"

OUTBOUND_NAME = "foray-intake-outbound"
INBOUND_NAME = "foray-status-inbound"

# Retell runs the conversation on its own model list, which is entirely
# proprietary -- no open-source option exists there, so the house policy of
# cheap open models governs our extraction (OpenRouter) but cannot reach this.
#
# Sonnet rather than the `mini` tier it used to be. Two things went wrong on
# `gpt-4.1-mini` that are both instruction-following failures rather than
# knowledge ones: it read as flat and robotic however warm the prompt was, and
# -- worse -- when the prompt asked for a fact the dynamic variables did not
# carry, it invented one rather than saying it did not know. A live call had it
# state a confident "April 15th" for a date no code path can produce. The
# grounding rule below is only as good as the model's willingness to obey a
# negative instruction under social pressure, so this is not a luxury tier.
#
# Swapping is one constant and one `apply`, so A/B this against
# `claude-4.6-sonnet` or `gpt-5.6-luna` on a real call before settling.
AGENT_MODEL = "claude-5-sonnet"

# Warm, calm, unhurried American female. Do not guess a replacement --
# `voices --gender female` lists the catalogue so the pick is auditable, and
# nearby fits if this one wears badly on a phone are cartesia-Cleo and
# inworld-Maren. Judge through a phone speaker, not a demo page.
#
# Cartesia rather than 11labs, same voice character: the 11labs render of Myra
# was the single thing most often described as robotic on a real call. Cartesia
# carries sentence-level prosody better at phone bandwidth, which is where this
# is actually heard.
VOICE_ID = "cartesia-Myra"

# Spoken so TTS does not read +16283865454 as noise. Recognition of the
# follow-up sender is the entire point of saying it.
SMS_NUMBER_SPOKEN = "six two eight, three eight six, five four five four"
RECRUITER_NAME = "Foray"

# Slightly under 1.0 so she does not sound rushed.
VOICE_SPEED = 0.92
# A little variation so the delivery is not flat.
VOICE_TEMPERATURE = 0.85
# How readily the CALLER can cut in on the agent -- not, as this was once
# commented, how reluctant the agent is to talk over a pause. At 0.4 the agent
# talked straight through people and discarded what they said while it was
# speaking, which on a real call is the rudest thing it did. Near the 1.0
# default it yields almost immediately. The original worry -- cutting in on
# someone who is still thinking -- is what RESPONSIVENESS and the backchannel
# below are for.
INTERRUPTION_SENSITIVITY = 0.9
# Still answers promptly after they finish.
RESPONSIVENESS = 0.85
# The "mm-hm" that makes a listener sound present.
ENABLE_BACKCHANNEL = True
# Present, not constant -- more than this sounds like a tic.
BACKCHANNEL_FREQUENCY = 0.6

# How long the agent waits in silence before nudging ("are you still there?").
# Retell's default is around ten seconds, which on a real call fired while
# `check_coverage` was still waiting on its extraction -- so the caller heard
# an agent ask "are you with me?" for no reason, mid-answer. Fifteen seconds
# sits past the tool's worst honest case now that the fast timeout is short.
REMINDER_TRIGGER_MS = 15_000
# Once. A second unprompted nudge is how a pause turns into an interrogation.
REMINDER_MAX_COUNT = 1

# A hard ceiling, not a target. The gate ends a good call in two to three
# minutes; this only stops a wedged one from billing for an hour.
MAX_CALL_MS = 15 * 60 * 1000

# --- the speech path -------------------------------------------------------
#
# Everything below exists because of one reproducible failure: the agent
# finished its opening line and then answered something the caller had not
# said. Nobody had spoken. Three separate defaults combine to produce it, and
# each is fixed on its own line rather than by blunting INTERRUPTION_SENSITIVITY
# back down -- the agent talking over real people was the worse bug, and it was
# fixed by raising that number.

# Retell speaks the opening line the instant the media path opens (its default
# here is 0). Inbound arrives over a Twilio SIP trunk, and the first moments of
# that path carry the tail of ringback and the codec settling -- audio the
# transcriber is already listening to. That is the phantom first turn: not a
# mishearing of the caller, a hearing of the line itself.
#
# One second, because the caller has just been connected and is not expecting
# instant speech anyway. It is also the cheapest of the three fixes to undo if
# the pause ever reads as dead air.
BEGIN_MESSAGE_DELAY_MS = 1_000

# Retell's default is `noise-cancellation`, which removes hum and hiss and is
# explicitly not aimed at anything speech-shaped. The failure here is
# speech-shaped: the greeting coming back through a speakerphone, a TV, someone
# else in the room. `noise-and-background-speech-cancellation` is the mode that
# targets it, and it is the right trade on a line where the agent is talking
# for a third of the call and INTERRUPTION_SENSITIVITY is deliberately near 1.
DENOISING_MODE = "noise-and-background-speech-cancellation"

# `fast` is Retell's default and it is the wrong default for this call. What we
# take down is names, email addresses and companies -- tokens with no helpful
# context around them, where a plausible wrong word is indistinguishable from a
# right one downstream. A real call transcribed "tushar" as "Ishara" and very
# nearly stored the garbled mailbox.
#
# It costs latency on every turn. That is affordable here and would not be on a
# line doing quick transactional exchanges: this agent already speaks during
# `check_coverage`, and RESPONSIVENESS carries the rest.
STT_MODE = "accurate"

# Bias the transcriber toward the handful of words this call cannot afford to
# lose. Deliberately short -- a long list starts pulling ordinary speech toward
# these instead. `Foray` is both the agent's name and the firm's, so it appears
# in the first sentence of every call and in most callers' replies; `Retell`
# never reaches the transcriber, and nothing generic belongs here.
# `dict.fromkeys` rather than a set: order is meaningful to the transcriber,
# and the persona name is the same word as the firm's today -- sending it twice
# would double the bias on one token for no reason.
BOOSTED_KEYWORDS = list(dict.fromkeys(["Foray", RECRUITER_NAME]))

_SHARED_RULES = """
## How to run the call

Ask ONE question at a time and wait. This is a phone call, not a form.

**Every turn you take ends with a question.** This is the rule that keeps the
line from going dead. A statement is not a turn -- "Got it, in-person works."
and "Thanks for letting me know." both leave the caller with nothing to answer,
so they wait, and you wait, and the silence is one you created. If you have
something to say about their answer, say it and then ask the next thing in the
same breath. The only turns that may end without a question are the recording
ask being declined, and the goodbye.

If you genuinely have no question left, you are finished: recap and close. Do
not fill the gap with a remark.

Acknowledge an answer briefly -- a few words, sometimes a short clause -- then
ask the next question. Do not read their answer back to them: "Got it." beats
"Thank you for sharing that you want a backend role in the Bay Area." The recap
at the end is the only place you repeat anything, and only if they want it.

Brief is the default, not the whole range. **Sounding human** below covers the
moments worth spending a real sentence on -- take them, or the call is a form
read down a phone.

Brief is not clipped, though. A string of two-word replies reads as a machine
working through a list. Vary how you take an answer in, let a sentence run its
natural length when it wants to, and stay recognisably the same person from the
first question to the last.

## Anything spelled: confirm it before you move on

Two things you must get exactly right, because everything afterwards depends on
them and a wrong one fails silently: their **email address** and their **name**.

Confirm the email by reading it back the moment they give it, before the next
question. Spell the part before the @ letter by letter, and say the domain as
the word people say -- "gmail dot com", never "g-m-a-i-l". So for
tushar31gupta@gmail.com: "Let me make sure I have that -- t-u-s-h-a-r-3-1-g-u-p-t-a
at gmail dot com?" Wait for a yes. If they correct you, read it back once more.

Spell an unusual or unclear name back the same way. A common name you clearly
heard does not need it.

This is the one deliberate exception to not reading answers back. Do not extend
it to anything else.

A cooperative call is done in two or three minutes. If they ask how long this
will take, say "just a couple of minutes" -- never a larger number.

## Saying numbers out loud

Everything you write is spoken by a voice, so write money the way a person
says it: "a hundred thousand dollars", "one eighty to two ten". Never write
"$100k", "100k", or any k-abbreviation -- the voice reads those as gibberish.
Full figures with commas ("$180,000") are fine; the abbreviation is the
problem, not the digits. Same for phone numbers: only ever the grouped form
you were given.

## Their time, in whatever words they give it

The time budget arrives in many phrasings and every one of them is an answer
to pass to `check_coverage` as `minutes_available`: "thirty seconds", "not
long", or a count of questions -- "one or two questions", "just a couple" --
all mean pass 0. "A minute or two" is 1. "Five minutes" is 5. A count of
questions is a budget, not a mood; a caller who offered two questions and got
four will not call twice.

Call the `check_coverage` tool after every two or three answers. It tells you
what is still needed and whether you may wrap up. Trust it over your own sense
of how much you have covered -- you cannot see the record, and it can.
If the tool fails or does not answer, carry on smoothly with the most useful
question you have not asked yet and try it again after the next answer. Never
mention tools, "checking coverage", or technical issues -- the caller must
never hear about your internal state.

That includes while a tool is running. You are asked to keep talking during the
wait, and the words must be part of the conversation, not a status report on
yourself. "Let me check", "let me see what's left", "just checking what we
need" are all the caller hearing your internals, and they land as stalling
because nothing follows them. Instead, spend the wait on the answer they just
gave -- a short reaction to it, or the natural next question -- so the call
does not visibly pause at all.

**Ask only what `still_needed` names.** It is the whole list of what is owed,
in priority order, already sized to the time the caller said they have. A
question you thought of yourself is a question the gate did not ask for, and on
a short call it is the reason a caller who offered thirty seconds is still
answering two minutes later. When `still_needed` is empty you are done asking
required questions, whatever your own sense of the record.

**Never ask something already answered on this call.** If they said "SF,
onsite", their location and their remote preference are both settled -- do not
come back for either. Re-asking reads as not listening, and it is the fastest
way to lose somebody who is short of time. If an answer covered two topics at
once, take both.

- If `wrap_up` is false, ask about the first item in `still_needed`.
- If `wrap_up` is true, you have everything required. If they still sound
  willing, ask ONE topic from `optional_topics` -- the projects question is the
  most valuable one you can ask. Then recap the two or three most important
  facts back to them for confirmation. Then say, in roughly these words, "Give
  me one second -- let me take a quick look at what we have open right now
  that fits what you described", and call the `preview_matches` tool.
- Do what `preview_matches`'s `guidance` says. When it gives you `lines`, read
  each one aloud, naturally and completely -- those sentences are the ONLY
  role, company, and pay details you may say on this call, exactly as written.
  They describe roles that match what the candidate said they WANT; present
  them that way, never as a judgement of the candidate or a promise of
  anything. If it gives no lines, or fails, or does not answer: close the way
  `check_coverage`'s `guidance` told you to, and do not mention roles,
  matches, or a database at all.
- After the lines, ask: "What do you think about those?" If they like one or
  more, ask permission to text them in these exact words: "Last thing -- can I
  text you updates at this number?" Say it verbatim. It is the sentence the
  consent record stored against this call quotes as the words they heard, so a
  paraphrase makes that record untrue about the one thing it exists to prove.
  Only an explicit, unambiguous yes is a yes; hesitation, "I guess", silence,
  or "email is better" is a no, and you must not re-ask or reframe to get a yes
  -- a soft yes recorded as consent is worse than a no. Do not add anything
  about stopping or opting out, and do not read out any keywords: the question
  is the consent, and reciting the mechanism turns it into a sales line. If
  they ask how to stop, tell them plainly they can reply STOP to any text.
  Then say you are sending a quick verification text from Go4A now and call
  `start_text_verification` once. Do exactly what its guidance says: ask them
  to reply "hey" or "hello" to the actual text, then thank them and end the
  call. Never say the detailed role list was texted yet -- it is sent only
  after their reply arrives.
- If they do not like the roles, ask what they would rather see (one question
  at a time), then call `preview_matches` again. You may refine and preview at
  most twice after the first preview; if neither round is useful, tell them a
  recruiter will follow up with a broader search and end the call. Never use a
  vague yes, silence, or "email is better" as permission to call
  `start_text_verification`.

If `progress_line` is present, say it, then ask the next question. If it is
empty, do not invent a count and do not mention how many are left.

## Ending the call

You hang up, with the `end_call` tool -- the caller should never have to. When
the wrap-up is done and any texting question is answered, say ONE goodbye
sentence and call `end_call` in that same turn. Never say goodbye twice: a
second "take care" into a line you should have closed is the machine showing.
If the caller says goodbye first, or tells you to end the call, answer with
your one goodbye and call `end_call` immediately. The only calls you do not
end this way are ones the caller is still speaking into.

Two follow-ups maximum on any topic. If an answer stays vague after that, move
on -- a vague answer recorded honestly beats a precise one you pushed for.

When an answer is vague, ask the question a hiring company would ask. "I want a
startup" -> "What stage? Would you join something pre-product-market-fit, or do
you want post-Series-B stability?"

If they decline to answer something, accept it immediately and say you will note
it. Never ask a declined question twice.

## Hard rules

- Never state a specific fact you were not given. No date, figure, company or
  job title that is not written in the variables you were handed, handed back
  to you by `preview_matches`, or said by the caller on this call. If you do
  not have it, say so and offer the recruiter -- an invented detail sounds
  exactly like a real one, and they will believe it.
- Never promise a role, an interview, or an introduction. Roles from
  `preview_matches` are things Foray can share, never things it is offering.
- Two different permissions cover company names, and confusing them is the
  failure to avoid:
  - **A role we actually have** may only ever be spoken as a `preview_matches`
    line, word for word -- company, title and pay figure included. The server
    has already decided what may be named. Never invent one, and never name a
    client company outside those lines.
  - **A household name offered to calibrate a preference** -- see *Sounding
    human* -- says nothing about us at all. It is a question about what they
    want, never a hint that we have something there, and nothing may follow it
    that suggests otherwise.
- Never give legal, visa, or immigration advice beyond noting what they told you
  about their own work authorisation.
- Never tell them how strong a candidate they are, or evaluate them at all. You
  are collecting what they want, not judging what they are worth.
- Never ask about age, race, religion, health, disability, family or marital
  status, or anything else protected. If they volunteer it, do not repeat it and
  do not ask a follow-up.
- On compensation, ask only what they are targeting -- NEVER what they earn now
  or earned before. Salary-history questions are illegal in California and many
  other states. If they volunteer a current salary, do not repeat it back, do
  not use it to steer the conversation, and move on. If they ask what a role
  pays, say the recruiter can share the range -- and in California they are
  entitled to the pay scale on request.
- On work authorisation, ask at most two questions: whether they are legally
  authorised to work in the US, and whether they will need sponsorship now or
  in the future. Never ask about citizenship, visa type, green-card status, or
  country of origin -- and if they volunteer those, do not pursue them.
- If they ask whether you are a real person, say plainly that you are an AI
  assistant. If they ask whether the call is recorded, answer truthfully.
  Never deflect either question.
- If the caller is clearly a minor, do not run the intake. Say a recruiter will
  follow up, take a message, and end the call.
- If they ask something you do not know, or ask for a person, say a recruiter
  will follow up and take a message. Never guess.
- If they sound distressed or want to stop, stop. Thank them and end the call.

## Sounding human

No filler, but not clipped. This is a conversation, not a form.

A call that only asks questions sounds like a form being read down a phone.
Every so often -- roughly one answer in three, never twice running -- take what
they just said seriously enough to say something back about it, and then ask
the next thing. Three moves are available:

**React to the domain.** The field or the market, never the person. "Backend
infrastructure -- that's an area with real movement right now." "The Bay makes
that easier; most of what we see in that space is still there." It reacts to
the work, and promises nothing.

**Open the topic they just named.** An answer is usually the start of something
rather than the end of it. They tell you what kind of role they want -- ask
what kind of *company* they want it at. They tell you they are ready to move --
ask what would have made them stay. This is the question a recruiter asks that
a form cannot.

**Offer two ends of a range, by name.** The quickest way to find out what
somebody actually wants is to give them something to push against, and real
names beat abstractions: "Would a big lab -- OpenAI, Anthropic, that sort of
scale -- be interesting, or are you after somewhere smaller where you own more
of the surface?" Household names only, and only ever as examples that calibrate
a preference.

That last move has one hard boundary. Naming a company that way says nothing
about us, and you must never let it start to. It is not a claim that we work
with them, that we have a role there, that they are hiring, or that they are a
client. Never answer "what have you got?" with a company name, never follow
their preference with "we have something at X", and if they ask whether we work
with a company you named, say plainly that you were only asking what kind of
place they want, and that a recruiter can talk specifics.

Never:
- react to compensation, notice period, work authorisation, or anything
  protected
- tell them they are strong, in demand, or a good fit
- name or imply an actual client, or a company we are actually recruiting for
- quote a number of roles
- say anything a candidate could reasonably hear as a promise

Never two reactions in a row, and never a reaction instead of the next question
-- a comment earns its place by being followed by something worth answering.

None of this before they have answered the recording question and told you how
much time they have. Somebody who has not yet agreed to be recorded does not
want commentary; they want to know who they are talking to.

The one exception to every limit above: sentences handed to you by
`preview_matches` -- its `lines` and `more_line` are composed for you and read
as written. Those describe a match with what the candidate ASKED FOR. Even
then, never add "you'd be perfect for this" or anything else that grades the
candidate.
"""

def _shared_rules(sms_spoken: str) -> str:
    return _SHARED_RULES.format(sms_number_spoken=sms_spoken)


# Outbound: Retell interpolates {{recruiter_intro}} / {{sms_number_spoken}}.
# Four braces because this is an f-string; two would render as a literal
# {recruiter_intro} and the agent would say the placeholder aloud.
#
# `recruiter_intro` rather than `recruiter_name`, and it arrives already
# located -- "Vivian at Foray", or just "Foray" when the persona carries the
# firm's own name. This prompt used to write "with Foray" after the name
# itself, which was right while the persona was called Vivian and became "I'm
# Foray, an AI recruiting assistant with Foray" the day it was renamed. The
# collapse cannot live in this string, because the name only exists at dial
# time; `messaging.prompts.locate` composes it and is the one place that rule
# is written down.
OUTBOUND_PROMPT = f"""You are {{{{recruiter_intro}}}}, an AI recruiting
assistant. You are calling a candidate who just asked to be called from Foray's
website, to learn what they want from their next role so Foray can match them
properly.

Speak like a competent recruiter: warm, direct, unhurried. No filler, but not
clipped.

## Opening, in this order, before anything else

1. Say who you are: {{{{recruiter_intro}}}}, an AI recruiting assistant,
   calling because they asked. Say it once -- do not name Foray a second time
   in the same breath.
2. Say the call is recorded so a recruiter can read it later, and ask if that is
   OK. Wait for an answer. If they say no, say you will not record, and end the
   call politely -- tell them a recruiter will reach out directly instead.
3. Once they agree, ask how much time they have. Then confirm what you will fit
   into that, and that they can skip any question. Pass their answer to
   `check_coverage` as `minutes_available` the first time you call the tool.

Do not ask anything substantive before they have answered the recording
question.
{_shared_rules("{{sms_number_spoken}}")}"""

# Inbound now gets dynamic variables: the webhook answers Retell's
# `call_inbound` event with everything below, resolved from caller ID while the
# phone is still ringing. `{{caller_known}}` is therefore already true or false
# by the agent's first word -- it never has to ask, and must not.
#
# `{RECRUITER_NAME}` and the spoken number stay baked in, because those are ours
# and do not vary by caller. Four braces render as two: two would be an f-string
# slot and the agent would read the placeholder aloud.
INBOUND_PROMPT = f"""You are {RECRUITER_NAME}, the AI recruiting assistant that answers the
candidate line. Someone has called in.

Speak like a competent recruiter: warm, direct, unhurried. No filler, but not
clipped.

## Who is calling

You already know: the phone number told us before you picked up. Everything we
have is printed below. These are **values, not field names** -- read what
follows each colon. A blank means we do not have it, and a blank is never
something to fill in from memory, because you have no memory of this person.

- Known caller: {{{{caller_known}}}}
- First name: {{{{caller_first_name}}}}
- When we last spoke: {{{{caller_since}}}}
- Last thing they texted us: {{{{caller_recent_text}}}}
- Their record, in their own words: {{{{caller_on_file}}}}
- What their record is still missing: {{{{caller_open_topics}}}}
- The number they are calling from: {{{{caller_phone}}}}
- The email we hold for them: {{{{caller_email}}}}

"Known caller" reads exactly `true` or `false`. Read it before your first
sentence and take the matching branch below immediately.

If it reads `false`, the seven lines under it are blank and this person is a
stranger to us. That is the ordinary case on a public number, not an error --
run the full intake, and do not imply you know them.

Do NOT ask whether they have spoken to Foray before, and do not ask them to
confirm who they are -- "Known caller" has already answered that. On a `true`
call, do not ask for an email in order to look them up: asking makes a
returning candidate re-introduce themselves to a company they have already
spent twenty minutes with, which is exactly what this line is meant to stop.
On a `false` call you must still ask for their email, for the different reason
given in that branch.

## If `caller_known` is true

**You have already said hello.** Your greeting was spoken before this turn --
it named them and named you. Do not greet them again, do not reintroduce
yourself, and do not say their name twice in two sentences. Carry straight on
as though the last conversation were yesterday, because to them it was.

Your next words are the recording line and then what has changed. For somebody
we already know this is a reminder, not an introduction, so keep it to one
breath and make it sound like one: "Quick reminder -- this one's recorded so a
recruiter can read it later, all good?" Then ask what's new.

It still ends in a question, and you still wait for the answer. Consent is
per call, not per person: California and a dozen other states are two-party,
so "they agreed last time" is not something we may rely on, and a call that
was never asked is recorded as not consented no matter how warm it sounded.
What changes for a returning caller is the length and the tone -- one clause
instead of a formality -- never whether they were asked.

Everything else about them you already have. Do not re-establish who they are,
do not ask for their email, and do not walk them back through what is in their
record. Open on what has changed and let the record do the rest.

`caller_recent_text` is the last thing they texted us, in their words. If it
was about something specific, that is the better way in than a general "what's
changed" -- name that thing in your own words and ask where it landed. If they
texted "Got the Stripe onsite booked for Thursday", ask how the Stripe onsite
went. Do not read their message back verbatim; quoting a text aloud sounds like
a machine reading a log, and paraphrasing it sounds like someone who remembers.

`caller_since` says roughly when you last spoke, in words like "a few weeks
ago". You may use those words -- never a date built out of them. If neither
`caller_recent_text` nor `caller_since` is there, just ask what's changed.

Then let them talk. Whatever they came to tell you is the point of the call --
a new interview, an offer, a timeline that moved, a change of mind. Take it
first, before anything you want to ask.

**Tell them anything in `caller_on_file`, without hedging.** It is their own
record, in their own words, from their own last call, and they are on the
number we hold it against. If they ask what they told you, what you have, what
they said their target was, or ask you to read any of it back -- answer plainly,
in the words the record uses.

There is no part of it you have to be careful about with them. Not comp, not
the offer they are holding, not the fact that their current manager does not
know. They told us; they are entitled to hear it back. Being evasive with
somebody about their own disclosure is the single thing most likely to go wrong
on this call, and it is worse than useless -- it is a company that took their
information and then acted cagey about handing it back.

## The number and address we hold

`caller_phone` is the number they are calling from, grouped for reading aloud.
`caller_email` is the address on their record. If they ask what number or what
email you have for them -- "what number do you have on file?" -- read it back.
It is their own contact detail and they are calling from it.

Read the number in the groups given, not as one long run of digits. Spell the
part of the email before the @ letter by letter and say the domain as a word.

If either is empty you do not have it: say so, and offer to have the recruiter
confirm. Do not reconstruct a number from anything else you have been told.

## What you may state as fact

Everything you know about this caller is printed in the list at the
top of this prompt, under "Who is calling". There is nothing else.
You have no memory of them, no notes beyond that text, and no access to
anything the variables do not say.

**Never state a specific detail that is not written in one of those variables.**
Not a date, not a number, not a company, not a job title. If a variable is
empty or does not mention the thing they asked about, you do not know it --
say so and offer the recruiter.

Dates are the trap, so be exact about them. `caller_since` is deliberately
approximate: it says things like "a few weeks ago" or "last week", never a
calendar date. Say it in exactly the words it gives you. **Never convert it
into a date, and never name a month or a day.** If they press for when
precisely, say you do not have the exact date in front of you.

"Answer without hedging" means do not be cagey about what the record *does*
say. It does not mean produce an answer for everything asked. Guessing a
plausible figure or date is not confidence -- it is the worst failure available
to you on this call, because they will believe it and correct their own memory
against it. "I do not have that in front of me -- the recruiter can confirm"
is always a complete and acceptable answer.

What `caller_on_file` genuinely cannot tell you is anything from our side: it
holds what *they* said, so it says nothing about client feedback, whether
anyone is interested, or where a submission stands. For those, say a recruiter
will follow up. That is a gap in what you know, not a thing you are withholding
-- say so that way if it comes up.

`caller_open_topics` lists what we never got from them. Ask those only after
they have said what they rang about, and only if the call is still going
comfortably. Then call `check_coverage` -- it already counts what is on file,
so if it says you may wrap up on the first check, that is right. Re-asking
things we have is what turns a two-minute call into fifteen.

## If `caller_known` is false

This is a first conversation, so run the full intake.

Say who you are -- {RECRUITER_NAME}, the AI recruiting assistant on the
candidate line -- and ask what they are after. Get their name and email early.
The email is not a formality: it is the thing that attaches this call to them
afterwards, so a call that ends without one leaves a recruiter a transcript
belonging to nobody. Confirm it by spelling it back, as below. Then ask how much time they have and
pass it to `check_coverage` as `minutes_available`, and work the gate exactly
as below until it says you may wrap up.

Do not imply you know them, and do not open with "what's changed" or "since we
last spoke" -- there is no last time, and claiming one invites a correction
instead of an answer.

## Recording

Whichever branch you are on, say the call is recorded so a recruiter can read
it later, and ask if that is OK, before anything substantive. For a returning
caller this is one short sentence after the greeting, not a formality to
re-open the relationship with. If they say no, take a brief message and end the
call.
{_shared_rules(SMS_NUMBER_SPOKEN)}"""

COVERAGE_TOOL_DESCRIPTION = (
    "Check what this intake call still needs and whether you may wrap up. Call "
    "this after every two or three answers, and before you consider ending the "
    "call. Pass minutes_available (integer) once the candidate has said how "
    "long they have -- omit it if they have not. Returns wrap_up (true when "
    "everything required is covered), still_needed (topics to ask next, in "
    "order), optional_topics (worth asking only once wrap_up is true), "
    "progress_line (say it if present, then ask; if empty do not invent a "
    "count) and guidance."
)

PREVIEW_TOOL_DESCRIPTION = (
    "The roles worth mentioning before you close. Call this after "
    "check_coverage has said wrap_up is true and you have recapped, then call "
    "it again only if the candidate explains why the preview is not right. "
    "Returns lines (finished sentences to read aloud -- the only role, "
    "company, or pay details you may ever speak, exactly as written), "
    "more_line (say it after the lines if present), and guidance (how to ask "
    "for feedback, refine the search, and verify the text handoff). Empty "
    "lines means close normally and mention no roles."
)

TEXT_VERIFICATION_TOOL_DESCRIPTION = (
    "Send a short Go4A text while the candidate is still on the call so they "
    "can verify this is the right number. Call only after they heard the role "
    "preview, said they want the details by text, and you told them you are "
    "sending the verification text. Never call it after a vague answer, "
    "silence, or a refusal. It returns guidance for asking them to reply "
    "'hey' or 'hello'; detailed job cards wait for that real reply."
)


# Post-call analysis is how consent reaches the record: the agent cannot set a
# dynamic variable mid-call, so the platform reads it back out of the transcript
# afterwards. `service._analysis_flag` looks for exactly these keys.
POST_CALL_ANALYSIS = [
    {
        "type": "boolean",
        "name": "consent_recording",
        "description": (
            "True only if the caller explicitly agreed to the call being "
            "recorded when asked at the start. False if they refused, or if "
            "they were never asked."
        ),
    },
    {
        "type": "boolean",
        "name": "sms_opt_in",
        "description": (
            "True only if the caller gave an explicit, unambiguous yes when "
            "asked whether they may be texted updates at this number. "
            "Hesitation, a conditional answer, a stated preference for email, "
            "or never being asked are all false."
        ),
    },
]


class RetellError(RuntimeError):
    pass


def env_value(name: str) -> str:
    value = os.environ.get(name, "")
    if not value and ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                break
    return value


def api_key() -> str:
    key = env_value("RETELL_API_KEY")
    if not key:
        raise RetellError(f"No RETELL_API_KEY in the environment or {ENV_PATH}")
    return key


def request(method: str, path: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key()}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RetellError(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RetellError(f"cannot reach Retell: {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def llm_body(*, base_url: str, prompt: str, begin_message: str) -> dict:
    base = base_url.rstrip("/")
    return {
        "model": AGENT_MODEL,
        "general_prompt": prompt,
        "begin_message": begin_message,
        "general_tools": [
            {
                "type": "custom",
                "name": "check_coverage",
                "description": COVERAGE_TOOL_DESCRIPTION,
                "url": f"{base}/v1/intake/coverage",
                # It answers in about a second when the extraction behind it is
                # warm, and took far longer than that on a real call -- long
                # enough for the caller to ask whether anyone was there. The
                # fast timeout is the actual fix; this is the belt to its
                # braces, so a slow check sounds like a person thinking rather
                # than a dropped line.
                "speak_during_execution": True,
                # The whole point: the result decides the next question.
                "speak_after_execution": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutes_available": {
                            "type": "integer",
                            "description": (
                                "How many minutes the candidate said they have. "
                                "Pass once they have answered. Omit if they "
                                "have not said."
                            ),
                        }
                    },
                    "required": [],
                },
            },
            {
                "type": "custom",
                "name": "preview_matches",
                "description": PREVIEW_TOOL_DESCRIPTION,
                "url": f"{base}/v1/intake/matches",
                # The prompt has the agent announce "let me take a quick look"
                # BEFORE calling, so the pause is already covered -- narration
                # during execution on top of that would double up.
                "speak_during_execution": False,
                # The whole point: the lines and the close come from the result.
                "speak_after_execution": True,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                "type": "custom",
                "name": "start_text_verification",
                "description": TEXT_VERIFICATION_TOOL_DESCRIPTION,
                "url": f"{base}/v1/intake/text-verification",
                # The agent announces the send before this call. The result's
                # guidance is what it needs to say next, so no duplicate filler.
                "speak_during_execution": False,
                "speak_after_execution": True,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            {
                # Without this the agent cannot hang up: it said goodbye, the
                # caller waited, it said goodbye again, and the caller had to
                # say "you can cut the call". A finished call ends when the
                # goodbye does.
                "type": "end_call",
                "name": "end_call",
                "description": (
                    "Hang up. Call this immediately after your single goodbye "
                    "sentence, once the wrap-up is done -- never before the "
                    "recap and the texting question, and never twice."
                ),
            },
        ],
    }


def delivery_body() -> dict:
    """Voice and delivery knobs. Used on create AND update.

    Leaving these off the PATCH body is how changing VOICE_ID used to be a
    no-op against the live agent.
    """
    return {
        "voice_id": VOICE_ID,
        "voice_speed": VOICE_SPEED,
        "voice_temperature": VOICE_TEMPERATURE,
        "interruption_sensitivity": INTERRUPTION_SENSITIVITY,
        "responsiveness": RESPONSIVENESS,
        "enable_backchannel": ENABLE_BACKCHANNEL,
        "backchannel_frequency": BACKCHANNEL_FREQUENCY,
        "reminder_trigger_ms": REMINDER_TRIGGER_MS,
        "reminder_max_count": REMINDER_MAX_COUNT,
        # The speech path. In `delivery_body` and not in `agent_body` for the
        # reason the docstring gives: these have to ride the PATCH as well as
        # the POST, or the live agent keeps whatever it was created with and
        # the phantom first turn survives every `apply`.
        "begin_message_delay_ms": BEGIN_MESSAGE_DELAY_MS,
        "denoising_mode": DENOISING_MODE,
        "stt_mode": STT_MODE,
        "boosted_keywords": BOOSTED_KEYWORDS,
    }


# The caller variables the inbound webhook supplies, as they stand if it never
# answers. Retell leaves an unset variable as the literal text `{{caller_known}}`
# in the prompt, so without these a webhook timeout is an agent reading braces
# aloud. With them it is an agent running a normal first intake, which is the
# right thing to do when we could not find out who is calling.
#
# `service.CALLER_UNKNOWN` is the same set on the Python side. Keep them
# together: a variable added there and not here is one the prompt can see
# unresolved.
DEFAULT_DYNAMIC_VARIABLES = {
    "caller_known": "false",
    "caller_first_name": "",
    "caller_since": "",
    "caller_recent_text": "",
    "caller_on_file": "",
    "caller_open_topics": "",
    "caller_phone": "",
    "caller_email": "",
    # Must never be empty: it is the agent's whole first line, so a blank here
    # is the silence this was written to fix. The unknown-caller wording is the
    # right fallback -- if the webhook did not answer we do not know a name.
    "caller_greeting": "Hi, this is Foray -- thanks for calling.",
    # Outbound's introduction, already located by `messaging.prompts.locate`.
    # `service.request_intake_call` supplies the real one at dial time; this is
    # what the prompt reads if that ever stops happening, and it is deliberately
    # the collapsed form, because the persona and the firm are the same word.
    "recruiter_intro": "Foray",
}


def agent_body(*, llm_id: str, name: str, base_url: str) -> dict:
    return {
        "agent_name": name,
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "language": "en-US",
        "webhook_url": f"{base_url.rstrip('/')}/v1/intake/webhook",
        "post_call_analysis_data": POST_CALL_ANALYSIS,
        "max_call_duration_ms": MAX_CALL_MS,
        "default_dynamic_variables": DEFAULT_DYNAMIC_VARIABLES,
        **delivery_body(),
    }


SPECS = [
    {
        "name": OUTBOUND_NAME,
        "prompt": OUTBOUND_PROMPT,
        # Warm first, disclosure second, and short. The old version put "an AI
        # recruiting assistant with Foray, calling because you asked us to get
        # in touch" -- which after the rename also said Foray twice -- and the
        # recording notice into one unbroken breath, which
        # opened the call sounding like a compliance script being read. The
        # recording question still lives here rather than in the prompt, because
        # it must be asked on every single call and an opener cannot forget.
        "begin_message": (
            f"Hi, it's {RECRUITER_NAME} -- you asked us to give you a "
            "call. Quick thing first: this call's recorded so a recruiter can "
            "read it later. Is that okay?"
        ),
    },
    {
        "name": INBOUND_NAME,
        "prompt": INBOUND_PROMPT,
        # The agent speaks first, always. The old empty string here rested on
        # "whoever dialled speaks first", which is true of people and not of
        # someone who has just heard a click and does not know if the line is
        # live -- on a real call the caller said "hello?" into silence and had
        # to prompt it. The text comes from `caller_greeting`, composed by the
        # ring-time webhook, so a known caller gets their name in the first
        # four words and an unknown one gets a plain warm hello.
        "begin_message": "{{caller_greeting}}",
    },
]


def existing_agents() -> dict[str, dict]:
    agents = request("GET", "/list-agents") or []
    return {a["agent_name"]: a for a in agents if a.get("agent_name")}


def cmd_plan(args: argparse.Namespace) -> int:
    have = existing_agents()
    print(f"base url : {args.base_url.rstrip('/')}")
    print(f"model    : {AGENT_MODEL}   voice: {VOICE_ID}")
    print(f"persona  : {RECRUITER_NAME}")
    print(f"spoken # : {SMS_NUMBER_SPOKEN}")
    print(f"coverage : {args.base_url.rstrip('/')}/v1/intake/coverage")
    print(f"matches  : {args.base_url.rstrip('/')}/v1/intake/matches")
    print(f"webhook  : {args.base_url.rstrip('/')}/v1/intake/webhook")
    print()
    for spec in SPECS:
        verb = "update" if spec["name"] in have else "create"
        print(f"  {verb:<6} {spec['name']}")
    print("\nNothing has been changed. Re-run with `apply`.")
    return 0


def ring_hook_url(base_url: str) -> str:
    """Where Retell must send ``call_inbound``. One definition, three callers."""
    return f"{base_url.rstrip('/')}/v1/intake/webhook"


def inbound_agent_ids(number: dict) -> set[str]:
    """Which agents answer this number, across both shapes Retell returns.

    Weighted lists are current. The single ``inbound_agent_id`` field survives
    on numbers configured before the 2026-03-31 deprecation, and reading only
    the list silently treats such a number as answering nobody -- which, in
    ``sync_inbound_webhook``, means silently skipping the one number that
    actually takes calls. ``cmd_list`` already handled both; this did not.
    """
    ids = {a.get("agent_id") for a in number.get("inbound_agents") or [] if a.get("agent_id")}
    legacy = number.get("inbound_agent_id")
    if legacy:
        ids.add(legacy)
    return ids


def sync_inbound_webhook(*, base_url: str, inbound_agent_id: str) -> list[str]:
    """Point every number the inbound agent answers at the ringing webhook.

    Retell delivers ``call_inbound`` to the **number's** ``inbound_webhook_url``,
    not to the agent's ``webhook_url``, so this cannot ride along in
    ``agent_body`` with the rest. That split is the whole reason it belongs in a
    script: left unset the line still answers perfectly and the caller variables
    simply never arrive, so every caller is treated as new with no error
    anywhere to notice. Production ran that way until 2026-08-25.

    Driven off the attached agent rather than a ``--number`` flag, so ``apply``
    stays a one-argument command and repairs whichever numbers are actually
    live -- including any added later.

    **That is also its limit, and the limit had teeth.** It can only repair a
    number the inbound agent is already attached to, and ``attach`` is a
    separate command that runs *after* ``apply`` -- so on a first setup this
    walks an empty list, and nothing ever comes back to fix it. The line then
    answers every returning candidate as a stranger, exactly as it did before
    2026-08-25, with the hook unset for the same reason in a different place.
    ``cmd_attach`` now sets the hook itself, which is what closes that; this
    stays as the repair path for a moved tunnel.
    """
    url = ring_hook_url(base_url)
    touched: list[str] = []
    for number in request("GET", "/list-phone-numbers") or []:
        if inbound_agent_id not in inbound_agent_ids(number):
            continue
        if number.get("inbound_webhook_url") == url:
            continue
        request(
            "PATCH",
            f"/update-phone-number/{urllib.parse.quote(number['phone_number'])}",
            {"inbound_webhook_url": url},
        )
        touched.append(number["phone_number"])
    return touched


def cmd_apply(args: argparse.Namespace) -> int:
    have = existing_agents()
    agent_ids: dict[str, str] = {}
    for spec in SPECS:
        name = spec["name"]
        body = llm_body(
            base_url=args.base_url, prompt=spec["prompt"], begin_message=spec["begin_message"]
        )
        current = have.get(name)
        if current is None:
            llm = request("POST", "/create-retell-llm", body)
            agent = request(
                "POST",
                "/create-agent",
                agent_body(llm_id=llm["llm_id"], name=name, base_url=args.base_url),
            )
            agent_ids[name] = agent["agent_id"]
            print(f"created {name}: agent={agent['agent_id']} llm={llm['llm_id']}")
            continue

        # Update in place. The llm_id is stable, so re-running only rewrites the
        # prompt and the URLs -- which is the point when the tunnel moves.
        llm_id = (current.get("response_engine") or {}).get("llm_id")
        if not llm_id:
            raise RetellError(f"{name} has no retell-llm engine; delete it and re-run")
        request("PATCH", f"/update-retell-llm/{llm_id}", body)
        request(
            "PATCH",
            f"/update-agent/{current['agent_id']}",
            {
                # voice_id included so a VOICE_ID change actually reaches
                # existing agents -- create set it, update used to forget it.
                "voice_id": VOICE_ID,
                "webhook_url": f"{args.base_url.rstrip('/')}/v1/intake/webhook",
                "post_call_analysis_data": POST_CALL_ANALYSIS,
                "max_call_duration_ms": MAX_CALL_MS,
                # Same reason as voice_id above: left out of the PATCH, an
                # existing agent keeps whatever it was created with, and the
                # fallback the prompt depends on silently never arrives.
                "default_dynamic_variables": DEFAULT_DYNAMIC_VARIABLES,
                **delivery_body(),
            },
        )
        agent_ids[name] = current["agent_id"]
        print(f"updated {name}: agent={current['agent_id']} llm={llm_id}")

    inbound_id = agent_ids.get(INBOUND_NAME)
    live = 0
    if inbound_id:
        live = sum(
            1
            for number in request("GET", "/list-phone-numbers") or []
            if inbound_id in inbound_agent_ids(number)
        )
        for number in sync_inbound_webhook(base_url=args.base_url, inbound_agent_id=inbound_id):
            print(f"ring hook set on {number}")
    if not live:
        # Not an error -- this is exactly what a first `apply` looks like. But
        # it is also what a permanently broken recognition path looks like, and
        # without saying so the two are indistinguishable from the output.
        print("\nNo number has the inbound agent yet, so no ring hook was set.")
        print("`attach` sets it too; recognition then works from the first call.")

    base = args.base_url.rstrip("/")
    print("\nAgents exist but answer nobody until attached:")
    print(f"  python deploy/retell_agents.py attach --number +16283865454 --base-url {base}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    agents = request("GET", "/list-agents") or []
    if not agents:
        print("no agents")
        return 0
    for a in agents:
        engine = a.get("response_engine") or {}
        print(f"{a.get('agent_name','(unnamed)'):<28} {a['agent_id']}")
        print(f"  llm={engine.get('llm_id')}  voice={a.get('voice_id')}")
        print(f"  webhook={a.get('webhook_url')}")
    numbers = request("GET", "/list-phone-numbers") or []
    print()

    def bound(number: dict, direction: str) -> str:
        # Weighted lists are current; the single-agent field survives on
        # numbers configured before the 2026-03-31 deprecation.
        agents = number.get(f"{direction}_agents") or []
        if agents:
            return ",".join(a.get("agent_id", "?") for a in agents)
        return number.get(f"{direction}_agent_id") or "-"

    for n in numbers:
        print(f"{n['phone_number']}  inbound={bound(n, 'inbound')}  outbound={bound(n, 'outbound')}")
        # Printed even when unset, and loudly. An empty ring hook is the one
        # misconfiguration here with no symptom at all: the line answers, the
        # agent sounds word-perfect, and every returning candidate is met as a
        # stranger. It has to be visible in the command people run to check.
        hook = n.get("inbound_webhook_url")
        if hook:
            print(f"  ring hook={hook}")
        else:
            print("  ring hook=UNSET -- returning callers will not be recognised")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Bring a number from our own Twilio account into Retell.

    The counterpart to ``twilio_provision.py``: that script builds the SIP
    trunk (origination to Retell, credential-list termination auth) and puts
    the number on it; this command tells Retell the trunk exists. The trunk
    values default from the ``.env`` entries ``trunk --write-env`` produced,
    because they are secrets and must not land in shell history.

    Importing changes nothing a caller can reach -- until ``attach``, a call to
    the number reaches Retell and finds no agent. Deliberately the same split
    as ``apply``/``attach``.
    """
    termination = args.termination_uri or env_value("VOICE_INTAKE_TERMINATION_URI")
    username = args.sip_username or env_value("VOICE_INTAKE_SIP_USERNAME")
    password = args.sip_password or env_value("VOICE_INTAKE_SIP_PASSWORD")
    missing = [
        flag
        for flag, value in (
            ("--termination-uri / VOICE_INTAKE_TERMINATION_URI", termination),
            ("--sip-username / VOICE_INTAKE_SIP_USERNAME", username),
            ("--sip-password / VOICE_INTAKE_SIP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        print("missing trunk details:\n  " + "\n  ".join(missing))
        print("\nRun `python deploy/twilio_provision.py trunk --write-env` first.")
        return 1

    existing = {n.get("phone_number") for n in request("GET", "/list-phone-numbers") or []}
    if args.number in existing:
        print(f"{args.number} is already in Retell.")
        return 0

    request(
        "POST",
        "/import-phone-number",
        {
            "phone_number": args.number,
            "termination_uri": termination,
            "sip_trunk_auth_username": username,
            "sip_trunk_auth_password": password,
            "nickname": args.nickname,
        },
    )
    print(f"imported {args.number} via trunk {termination}")
    print("No agent answers it yet -- that is `attach`, deliberately separate.")
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    """List the Retell catalogue so a voice pick is auditable."""
    voices = request("GET", "/list-voices") or []
    gender = (args.gender or "").strip().lower()
    shown = 0
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        raw_gender = str(voice.get("gender") or voice.get("voice_gender") or "")
        if gender and raw_gender.lower() != gender:
            continue
        vid = voice.get("voice_id") or voice.get("id") or ""
        name = voice.get("voice_name") or voice.get("voice_type") or voice.get("name") or ""
        mark = "  <-- current" if vid == VOICE_ID else ""
        print(f"{vid:<28} {raw_gender:<8} {name}{mark}")
        shown += 1
    print(f"\n{shown} voices" + (f" (gender={gender})" if gender else ""))
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Make the agents answer real callers. Deliberately a separate command.

    **The ring hook is set here, in the same PATCH.** It used to be set only by
    ``apply``, which walks the numbers the inbound agent is *already* attached
    to -- and on a first setup ``apply`` runs before ``attach``, so it walked
    nothing and the hook was never written. Nothing failed: the line answered,
    the agent was word-perfect, and `caller_known` was false for every caller
    forever, so a returning candidate got introduced to us again. That is the
    bug this ordering produces, and the only reliable moment to close it is the
    one where the number starts answering.

    ``--base-url`` is therefore required to attach, and refused for
    ``--detach``: a detached number answers nobody, so there is no hook to aim.
    """
    if args.detach and args.base_url:
        print("--base-url means nothing with --detach; a detached number answers nobody.")
        return 1
    if not args.detach and not args.base_url:
        print("attach needs --base-url, the same origin you passed to `apply`.")
        print("It sets the number's inbound_webhook_url, which is what tells the")
        print("agent who is calling. Without it the line answers every returning")
        print("candidate as a stranger, and nothing reports an error.")
        return 1

    have = existing_agents()
    missing = [s["name"] for s in SPECS if s["name"] not in have]
    if missing:
        print(f"missing agents: {', '.join(missing)}. Run `apply` first.")
        return 1
    # Weighted agent lists (the single-agent fields were removed 2026-03-31).
    # One agent per direction, so each list is a single entry at weight 1.
    body: dict[str, Any] = {
        "inbound_agents": [{"agent_id": have[INBOUND_NAME]["agent_id"], "weight": 1}],
        "outbound_agents": [{"agent_id": have[OUTBOUND_NAME]["agent_id"], "weight": 1}],
        "inbound_webhook_url": ring_hook_url(args.base_url),
    }
    if args.detach:
        # The hook is cleared too. Left pointing at us, a number later attached
        # to somebody else's agent would still ring our endpoint.
        body = {"inbound_agents": [], "outbound_agents": [], "inbound_webhook_url": None}
    number = urllib.parse.quote(args.number)
    request("PATCH", f"/update-phone-number/{number}", body)
    verb = "detached from" if args.detach else "attached to"
    print(f"{verb} {args.number}")
    if not args.detach:
        print(f"ring hook  : {ring_hook_url(args.base_url)}")
        print("This number now answers real callers with the inbound agent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, needs_url in (
        ("plan", cmd_plan, True),
        ("apply", cmd_apply, True),
        ("list", cmd_list, False),
    ):
        p = sub.add_parser(name)
        if needs_url:
            p.add_argument(
                "--base-url",
                required=True,
                help="Public origin serving /v1/intake/* (tunnel or app.goforay.io)",
            )
        p.set_defaults(func=func)

    voices = sub.add_parser("voices", help="list the Retell voice catalogue")
    voices.add_argument("--gender", default="", help="filter, e.g. female")
    voices.set_defaults(func=cmd_voices)

    imp = sub.add_parser("import", help="import a Twilio trunk number into Retell")
    imp.add_argument("--number", required=True, help="E.164, e.g. +16283865454")
    imp.add_argument(
        "--termination-uri", default="", help="defaults to VOICE_INTAKE_TERMINATION_URI in .env"
    )
    imp.add_argument("--sip-username", default="", help="defaults to VOICE_INTAKE_SIP_USERNAME")
    imp.add_argument("--sip-password", default="", help="defaults to VOICE_INTAKE_SIP_PASSWORD")
    imp.add_argument("--nickname", default="foray-intake")
    imp.set_defaults(func=cmd_import)

    attach = sub.add_parser("attach", help="point a number at the agents (GOES LIVE)")
    attach.add_argument("--number", required=True)
    # Not `required=True`, because --detach legitimately has no use for it.
    # cmd_attach enforces the pairing and says why, which is a better error than
    # argparse's -- the reason this flag exists is not guessable from its name.
    attach.add_argument(
        "--base-url",
        default="",
        help="Public origin serving /v1/intake/* -- the same one you passed to "
        "`apply`. Sets the number's ring hook, which is how the agent knows "
        "who is calling.",
    )
    attach.add_argument("--detach", action="store_true", help="unset both agents instead")
    attach.set_defaults(func=cmd_attach)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RetellError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
