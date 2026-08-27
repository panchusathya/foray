"""Wording for the text channel.

Deliberately *not* in ``llm/prompts.py``. That file is the shared prompt
library and the voice branch is appending to it too, so a second append here
is a merge conflict for no benefit -- nothing outside this package composes an
intake text. It should move there when the branches meet.

The model's job is narrow on purpose. It never decides *what* to ask: the field
comes from ``intake.schema.next_questions()``, and the canned ``asks`` string
is already a usable question. All the model does is make the next question
sound like a reply to what the person just said. That is why the fallback is
not a degradation -- with no ``ANTHROPIC_API_KEY`` the thread sends the canned
question, which is a plain, correct thing to ask.

The output is validated rather than trusted. One question mark, no invented
second ask, length capped -- see ``valid_question``. An instruction not to ask
two things at once is a hope; a check that rejects the message is a rule.

Outbound copy is lowercase. A recruiting text that shouts reads like a survey;
the confirm/gap-fill batches are written that way and ``lowercase_outbound``
enforces it on anything a model rewrites, while leaving URLs alone.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import get_settings

# Roughly one SMS segment and a bit. Long enough for an acknowledgement plus a
# question, short enough that the channel stays a conversation.
MAX_QUESTION_CHARS = 320
# Batched gap-fill messages are allowed to run longer: several dashed
# questions in one iMessage, so the candidate can answer in one reply.
MAX_BATCH_CHARS = 700

_URL = re.compile(r"https?://\S+", re.IGNORECASE)

COMPOSE_SYSTEM = """\
You write one short text message for a recruiting intake assistant.

You are given the question that must be asked next and, usually, what the \
candidate just said. Rewrite the question so it reads as a natural reply: \
acknowledge their last message in a few words where there is something to \
acknowledge, then ask.

Hard rules:
- Always lowercase. No capital letters except inside URLs.
- Ask exactly one thing — the question you were given, and nothing else.
- Keep it under 300 characters. This is a text message, not an email.
- No greeting or sign-off. The conversation is already open.
- Never evaluate the candidate, their answers, or their chances. No "great \
background", no "you'd be a strong fit". You are collecting information, not \
judging it.
- Never promise a role or an interview, never name a client company, never \
give legal, visa or immigration advice.
- If they declined to answer something, do not push and do not comment on it.
- Plain text. No markdown, no emoji, no bullet points.
"""

COMPOSE_BATCH_SYSTEM = """You write one short text message for a recruiting assistant, in reply to what the candidate just said.

You are given the exact content that has to go out: sometimes one question, sometimes two or three, sometimes a statement. Rewrite it so it sounds like a person replying, not a form. Open by acknowledging what they just told you in a few words, using their first name if you were given one, then say the rest.

Hard rules:
- Always lowercase. No capital letters except inside URLs.
- Cover everything you were given and nothing more. Do not drop a question because it seems minor, and never add a question of your own — an invented question gets a real answer that nobody wanted.
- Never state a fact you were not given. You know only what is in the context below; if you did not receive their name, their employer or their title, you do not know it, and guessing puts a stranger's details in front of them.
- Keep it under 480 characters. It is a text message.
- No greeting, no sign-off, no markdown, no emoji, no bullet points. Write it as flowing sentences, not a dashed list.
- Never evaluate the candidate or their chances. No "great background", no "you would be a strong fit". You are collecting information, not judging it.
- Never promise a role or an interview, never name a client company, never give legal, visa or immigration advice.
- If they declined something, do not push and do not comment on it.
"""


def compose_batch_prompt(
    *, says: str, last_inbound: str, already_known: str, first_name: str = ""
) -> str:
    lines = [f"What has to go out:\n{says}"]
    if first_name:
        lines.append(f"Their first name: {first_name}")
    if last_inbound.strip():
        lines.append(f"They just said: {last_inbound.strip()}")
    if already_known.strip():
        lines.append(f"Already on file (do not ask again):\n{already_known.strip()}")
    return "\n\n".join(lines)


COMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["message"],
    "properties": {
        "message": {
            "type": "string",
            "description": "The message to send. One question, under 300 characters, all lowercase.",
        }
    },
}


def compose_prompt(
    *, asks: str, last_inbound: str, already_known: str, topic: str = ""
) -> str:
    return (
        f"<question_to_ask>\n{asks.strip()}\n</question_to_ask>\n\n"
        f"<what_it_is_for>\n{topic.strip() or '(no further context)'}\n</what_it_is_for>\n\n"
        f"<they_just_said>\n{(last_inbound or '').strip() or '(nothing yet)'}\n</they_just_said>\n\n"
        f"<already_covered>\n{already_known.strip() or '(nothing yet)'}\n</already_covered>\n\n"
        "Write the next message."
    )


def valid_question(text: str, *, batched: bool = False) -> bool:
    """Is this safe to send as-is?

    Structural checks only, and all of them exist because an instruction alone
    does not hold: a model asked for one question will occasionally send two,
    and the second one is always the one nobody wanted to answer.

    Batched messages (LinkedIn confirm / gap-fill) are allowed multiple
    questions and line breaks; they are canned, not model-written.
    """
    body = (text or "").strip()
    if not body:
        return False
    if batched:
        return len(body) <= MAX_BATCH_CHARS
    if len(body) > MAX_QUESTION_CHARS:
        return False
    if body.count("?") > 1:
        return False
    # A blank line means the model wrote two paragraphs, which on this channel
    # is two messages wearing one coat.
    return "\n\n" not in body


# Carrier keywords are held out of the voice transform for the same reason
# URLs are: they are not our prose. STOP and HELP are matched
# case-insensitively by us and by every carrier, so lowercasing them would
# still *work* -- but the disclosure exists to be seen by somebody skimming,
# and "reply STOP" is the shape people have been trained to recognise.
_KEYWORDS = re.compile(r"\b(STOP|HELP|START|UNSTOP)\b")


def lowercase_outbound(text: str) -> str:
    """Lowercase a message while leaving URLs and carrier keywords untouched."""
    placeholders: dict[str, str] = {}

    def _hold(match: re.Match[str]) -> str:
        key = f"@@HOLD{len(placeholders)}@@"
        placeholders[key] = match.group(0)
        return key

    held = _URL.sub(_hold, text or "")
    held = _KEYWORDS.sub(_hold, held)
    lowered = held.lower()
    for key, original in placeholders.items():
        lowered = lowered.replace(key.lower(), original)
    return lowered


def recruiter_name() -> str:
    return (get_settings().intake_recruiter_name or "Foray").strip() or "Foray"


# The greeting carries exactly two disclosures: who we are, and that this is an
# AI. It used to carry a third -- "you can stop any time" -- and that clause is
# gone from every greeting in this package. See INTAKE_COMPLIANCE.md section 6a
# for the reasoning and for the exposure it creates; the short version is that
# an opt-out recital in a *greeting* reads as a marketing blast to the person
# receiving it, which is the read that produces the complaint the recital was
# there to prevent.
#
# **Keyword handling is untouched and must stay that way.** STOP still stops,
# HELP still answers, the acknowledgement still goes out, and
# ``lowercase_outbound`` still holds the keywords out of the voice transform.
# Removing a sentence about the mechanism is not removing the mechanism, and
# ``TestKeywordsStillWork`` exists to keep those two things separate.
def greeting_for(*, org: str, recruiter: str | None = None) -> str:
    name = (recruiter or recruiter_name()).strip() or "vivian"
    org_name = (org or "our team").strip() or "our team"
    return (
        f"hey, i'm {name} from {org_name}. i'm an ai, and i'm here to take a "
        f"few details so we can match you to the right roles. it takes a "
        f"couple of minutes."
    )


# Kept as a format-string for callers that still pass ``{org}``. Prefer
# ``greeting_for``.
GREETING = (
    "hey, i'm {recruiter} from {org}. i'm an ai, and i'm here to take a "
    "few details so we can match you to the right roles. it takes a "
    "couple of minutes."
)

# The question actually sent, per intake field.
#
# ``IntakeField.asks`` is not this. It is a topic line written for a voice
# agent's goal checklist -- "Which roles they are actually going after." -- and
# sending it verbatim asks somebody to describe themselves in the third person.
# Voice hands those lines to a model that does the phrasing; a text channel
# with no API key has to have the phrasing already written.
#
# So this table is messaging's own, and it is a *copy* in the same sense
# ``llm.prompts._INTAKE_FIELD_KEYS`` is: it has to stay in step with ``FIELDS``,
# and ``test_every_intake_field_has_a_question`` is what keeps it honest. Add a
# field to the schema and that test says where the second edit goes.
QUESTIONS: dict[str, str] = {
    # --- the gate ---------------------------------------------------------
    "spec.job_titles": "what kind of role are you looking for next?",
    "spec.seniority": (
        "what level are you aiming for, and do you want to stay hands-on or move "
        "into management?"
    ),
    "spec.locations": "whereabouts are you based, and which locations would you work in?",
    "spec.remote_ok": "would you want fully remote, hybrid, or to be in an office?",
    "spec.must_have_skills": "what do you want to be hired to actually do day to day?",
    "employment_status": "are you working at the moment, and how actively are you looking?",
    "timeline": "how soon would you want to move?",
    "compensation": "what are you targeting on compensation? base or total is fine.",
    "work_authorization": (
        "do you have the right to work where you are looking, or would you need "
        "sponsorship?"
    ),
    "active_processes": "are you interviewing anywhere else at the moment?",
    "ok_to_share_with_clients": (
        "is it ok for us to put your profile in front of companies we are hiring for?"
    ),
    # --- enrichment -------------------------------------------------------
    "highlights": "is there a project or piece of work you are proudest of?",
    "spec.company_stages": (
        "what stage of company suits you best — early startup, growth, or established?"
    ),
    "spec.company_sizes": "any preference on company size?",
    "spec.industries": "are there industries you particularly want to work in?",
    "spec.nice_to_have_skills": (
        "anything else you have worked with that you would be happy to use?"
    ),
    "reason_for_looking": "what is prompting the move?",
    "spec.excluded_companies": (
        "are there companies you would not consider, or a non-compete we should know about?"
    ),
    "spec.target_companies": "any companies you are already excited about?",
    "spec.freeform_criteria": "anything that would be a dealbreaker for you?",
    "offers_in_hand": "do you have any offers in hand, and are there deadlines on them?",
    "willing_to_relocate": "would you consider relocating, and on what terms?",
    "equity_importance": "how much does equity matter to you against cash?",
    "urgency_driver": "is there anything making the timing urgent?",
    "confidential_items": "is there anything you would rather we did not repeat to a company?",
}


def question_for(field_key: str, fallback: str) -> str:
    """The candidate-facing wording, or the schema's topic line if it is new.

    A field added to ``FIELDS`` with no question here still gets asked --
    badly, but asked. Skipping it silently would leave a hole in the gate that
    nothing surfaces; the test makes sure it never gets that far.
    """
    return QUESTIONS.get(field_key) or fallback


WRAP_UP_COMPLETE = (
    "that's everything i needed — thank you. a recruiter will pick this up from "
    "here and will be in touch about roles that fit."
)

WRAP_UP_PARTIAL = (
    "thanks — i'll pass on what we covered. a recruiter will follow up if they "
    "need anything else."
)

STOP_ACKNOWLEDGEMENT = "understood — i won't message you again."

HELP_REPLY = (
    "juicebox intake assistant. reply stop to opt out. "
    "for help, contact the recruiter who shared this number."
)


# ---------------------------------------------------------------------------
# the recruiting-manager modes (CANDIDATE_AGENT_PLAN + the candidate agent)
# ---------------------------------------------------------------------------

# The resume nudge: the flexibility is the feature (either works), the nudge
# is the resume — it carries more than a profile scrape does.
ASK_LINKEDIN = (
    "Best way to get you set up: send your LinkedIn URL, or better, upload "
    "your resume here: {upload_link}"
)

# The Linq (iMessage) variant: files travel in the thread itself, so no
# upload link — a URL in the ask is one more tap than the answer needs. The
# link reappears only in the fallback copy below, when the direct route fails.
ASK_RESUME_OR_LINKEDIN = (
    "Best way to get you set up: send your resume right here (PDF or Word), "
    "or your LinkedIn URL."
)

# The connect-your-apps variants of the two asks above. One message, both
# routes: the connect link AND the send-it-yourself alternative, so somebody
# who would rather paste a URL loses nothing and nobody gets a second pitch.
# Sent as batched (they exceed the single-question budget on purpose).
CONNECT_OFFER_LINQ = (
    "Fastest way to get set up: connect your {apps} and I'll pull your resume "
    "and details myself, so you don't have to type anything — {connect_link}\n\n"
    "Or just send your resume right here (PDF or Word), or your LinkedIn URL."
)

CONNECT_OFFER_WEB = (
    "Fastest way to get set up: connect your {apps} and I'll pull your resume "
    "and details myself, so you don't have to type anything — {connect_link}\n\n"
    "Or send your LinkedIn URL, or upload your resume here: {upload_link}"
)

# Both fallbacks keep the thread moving with the two routes that still work:
# a different file via the upload page, or the LinkedIn URL.
ATTACHMENT_UNREADABLE = (
    "I can't read that kind of file, sorry — a PDF or Word doc works. "
    "You can also send your LinkedIn URL, or upload here: {upload_link}"
)

ATTACHMENT_FAILED = (
    "That file didn't come through, sorry. Mind uploading it here instead: "
    "{upload_link} — or just send your LinkedIn URL."
)

MANAGING_READY = (
    "You're set. I'll text you when I find roles worth your time — or ask me "
    "anything: \"what's my status\", \"find me roles\", \"who should I talk to\"."
)

MANAGER_FALLBACK = (
    "I can help with roles, applications, and status here — for anything else "
    "a recruiter will follow up. Want me to look for roles?"
)

# Paired copy: the second line is what goes out when the first would repeat
# what we just said. Saying the identical canned sentence twice in a row is
# the clearest possible sign nobody is listening, so each of these moves the
# conversation somewhere instead of restating it.
MANAGER_NOTED = "Noted — thanks. Anything else you want me looking for?"
MANAGER_NOTED_AGAIN = (
    "Got it, that's on your profile. Want me to go find roles with it?"
)

# The fallback is the line most likely to repeat, because everything the
# agent does not understand lands on it.
MANAGER_FALLBACK_AGAIN = (
    "I'm still not sure what you're after — the things I'm good at are finding "
    "roles, applying, and telling you where an application stands. Want me to "
    "start with roles?"
)

MANAGER_ACK_DECLINE = "Sounds good — I'll go look for roles and text you what I find."
MANAGER_STANDING_BY = (
    "All good — I'll keep an eye out and text you when something worth your "
    "time comes up."
)

# Gratitude ACK (love tapback) always gets a line; like/laugh stay silent
# when the reaction landed. Canned, never model-written.
MANAGER_ACK_WARM = "anytime — i'll text you when something good turns up."

DETAIL_UNAVAILABLE = "I don't have more detail on that one yet — want me to flag it to a recruiter?"

CLARIFY_WHICH = "Which one do you mean? Reply with the number from the list."

APPLY_ACK = (
    "On it — I'm preparing your application for {title} at {company}. "
    "I'll text you when it's ready to review."
)

# Names the budget that actually fires here. `create_application` refuses a
# tapback for exactly one reachable reason -- the per-company cap in
# `applications.limits.may_create_application`, two applications to one company
# in thirty days. The copy used to say "this week's application limit", which
# is the weekly *submission* cap: a different rule, a different number, and an
# answer that sends somebody looking for a limit they have not hit.
APPLY_PARKED = (
    "I've noted you want {title} at {company}. You already have applications in "
    "with them this month, so I'm holding this one rather than have them see you "
    "twice — I'll come back to you on it."
)

# Apply by link. The ack promises a review, never a submission -- we prepare
# the application; pressing submit stays the candidate's move
# (CANDIDATE_AGENT_PLAN §8, and the copy must not imply otherwise).
LINK_ACK = (
    "On it — I'm pulling that role up now. I'll text you when your "
    "application is ready to review."
)

LINK_NEED_IDENTITY = "Got the link — before I can fill it out, {ask}"

# Proactive notifications: canned, never model-written. Nothing to reply to
# means nothing to anchor a model, and a hallucinated claim texted unprompted
# is the worst place for one.
NOTIFY_PROFILE_READY = (
    "Your profile's in. I'm scanning for roles that fit — I'll text you the "
    "good ones."
)

NOTIFY_PACKAGE_READY = (
    "Your application for {title} at {company} is ready to review — resume, "
    "cover letter, and answers: {link}"
)

# Two different facts, and the copy must never blur them: one says we sent it,
# the other says you send it. CANDIDATE_AGENT_PLAN §8 -- the message must not
# imply we submitted somewhere we did not.
NOTIFY_APPLICATION_SENT = (
    "Your application for {title} at {company} is in — I submitted it and "
    "I'll tell you the moment they reply. What I sent: {link}"
)

NOTIFY_STATUS_CHANGED = "Update on your applications: {summary}"

# ---------------------------------------------------------------------------
# group-chat referrals (messaging/referrals.py)
# ---------------------------------------------------------------------------
#
# The one message we send into a group chat, and the only message most of
# these people will ever get from us. Three jobs, in this order: identify
# ourselves as an AI, tell the newcomer how to reach us in their *own* thread,
# and say what the intro is worth. Then we leave.
#
# It carries the AI disclosure even though a greeting normally owns that,
# because for the referred friend this *is* the greeting -- it is the first
# thing they have ever received from us, in a chat they did not open. The
# opt-out recital stays absent for the reason INTAKE_COMPLIANCE.md section 6a
# gives, and STOP still works here like anywhere else.
#
# No link, deliberately. Linq refuses links in a first outbound, a group can
# silently be on SMS where a link is a spam signal, and a bare number is
# something the newcomer can act on with one tap.
REFERRAL_GROUP_THANKS = (
    "hey — i'm {recruiter}, an ai that helps people apply to jobs. thanks for "
    "the intro. if you're the one being introduced: text me directly at "
    "{number} and i'll take it from there. that's what earns whoever made the "
    "intro {bonus} free applications. once you've messaged me i'll step out of "
    "this chat, so nothing of yours ends up in here."
)

# Same chat, nobody new in it: everyone here is already someone we talk to, or
# Linq could not tell us who is in the room. Still answered rather than
# ignored -- being added to a group and saying nothing reads as broken -- but
# it promises nothing, because there is no referral to pay for.
REFERRAL_GROUP_KNOWN_ONLY = (
    "hey — i'm {recruiter}, an ai that helps people apply to jobs. thanks for "
    "the add. i keep things to one-on-one chats, so text me directly at "
    "{number} whenever you need me. stepping out of this one."
)

# Somebody keeps talking in the group after we have answered. Sent once, then
# we go quiet: the number has now been given twice and a third repetition is
# just us talking over people. Says the only thing worth saying -- this is not
# the room for it -- and does not re-pitch the referral.
REFERRAL_GROUP_REDIRECT = (
    "text me separately at {number} — happy to help one-on-one. i'll stay out "
    "of the way in here."
)

# The last thing we say in a group, as we leave. The heads-up was given in the
# first message; this is what stops us simply vanishing, since the thing that
# triggered it -- the newcomer texting us privately -- is invisible from in
# here. Deliberately says nothing about who messaged or what about: the group
# may contain people with no business knowing either.
REFERRAL_GROUP_SIGN_OFF = (
    "all set — stepping out of here now. text me any time at {number}."
)

# The answer, plus at most one redirect. A third message into a group nobody
# asked us to join is us not taking the hint.
MAX_GROUP_REPLIES = 2

# What the referrer gets, once their friend has actually texted in. "banked",
# not "available": the bonus is recorded and not yet spendable, and copy that
# said otherwise would be found out by the first person who tried to use it.
NOTIFY_REFERRAL_CONFIRMED = (
    "your intro came through — they just texted me. that's {bonus} free "
    "applications banked for you."
)

# The first message of a chat we opened after a voice call — the one outbound
# message that is not a reply, sent only on an explicit opt-in captured on the
# recorded call (messaging/voice_handoff.py). Canned like every notification.
# No time promise ("in a moment") — the match cards behind it ride the notify
# queue and may defer to the send window.
VOICE_VERIFICATION_GREETING = (
    "Hi{name} — it's {recruiter} with Go4A. Great talking just now. Reply "
    "\"hey\" or \"hello\" so I know this is the right number, and I'll send "
    "the roles we discussed here."
)

# Apply-by-link refusals. A link that vanishes into silence reads as the
# product losing the message, so every dead end says so -- without ever
# implying anything was submitted.
NOTIFY_LINK_PROBLEM = (
    "I couldn't open the job link you sent — mind double-checking it, or "
    "sending the page it came from?"
)

NOTIFY_LINK_DUPLICATE = (
    "You're already in flight for that role — text \"status\" any time for "
    "where it stands."
)

NOTIFY_LINK_PARKED = (
    "I've saved that link. You're at this week's application limit, so I'll "
    "prepare it as soon as a slot opens."
)

# Presenting cards relaxes the 320-char single-question budget: a numbered
# block of three roles is legitimately longer. Still hard-capped, and still
# exactly one question (the footer's "reply with a number").
MAX_CARD_MESSAGE_CHARS = 900

# One tail after the cards, and every clause in it is something the candidate
# can do without leaving the thread. What it used to open with -- "tap a card
# for details" -- was an instruction to go elsewhere to find out what the job
# was, because the card was a vCard and carried almost nothing. The card
# carries the detail now, so the tail is the two gestures and then, last, the
# dashboard: that link is for tracking what is already in flight, not for
# reading these or applying to them.
PRESENT_TAIL = (
    "👍 any of these and i'll apply for you. 👎 and i'll drop it. "
    "reply with a number if you want more first. "
    "everything in flight, any time: {link}"
)

# The last line of the never-repeat guard (`conversation._unrepeated`): the
# turn produced the byte-identical bubble it sent last time, and the model's
# one job is to say the same thing differently and move the conversation
# somewhere. Same validation posture as COMPOSE: the output is checked, not
# trusted.
REPEAT_SYSTEM = """You rewrite one text message for a recruiting assistant.

The assistant is about to send the EXACT same message it sent last time, \
which reads as a bot stuck in a loop. Rewrite it so it says the same thing \
in different words, acknowledging in passing that nothing has changed since \
last time.

Hard rules:
- Always lowercase. No capital letters except inside URLs.
- Keep every fact, name, number and URL from the original, verbatim.
- Do not add any new question, promise, or fact.
- Keep it under 480 characters. It is a text message.
- No greeting, no sign-off, no markdown, no emoji, no bullet points.
"""


INTENT_SYSTEM = """\
You classify one inbound text from a job candidate talking to their recruiting
assistant. Pick exactly one intent:

- jobs_more: they want to see roles ("find me roles", "got anything?")
- jobs_detail: they want more about a specific role already shown
- apply: they want to apply to a specific role already shown
- status: they ask where their applications stand
- networking: they ask who to talk to / how to get an intro
- profile_answer: they are answering a profile/preference question
- acknowledgment: a pure thanks / great / haha with nothing else to do
- other: anything else

`selection` is the 1-based number of the role they mean when they reference
one ("the second one", "the Goldman role") against the numbered list provided;
0 when they don't. Never guess a selection you cannot ground in their words.
"""

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "selection"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "jobs_more",
                "jobs_detail",
                "apply",
                "status",
                "networking",
                "profile_answer",
                "acknowledgment",
                "other",
            ],
        },
        "selection": {"type": "integer", "minimum": 0},
    },
}


def intent_prompt(*, body: str, presented: list[dict[str, Any]]) -> str:
    lines = [
        f"{index}. {item.get('title', '')} — {item.get('company', '')}"
        for index, item in enumerate(presented or [], start=1)
    ]
    shown = "\n".join(lines) or "(none shown yet)"
    return (
        f"<roles_shown>\n{shown}\n</roles_shown>\n\n"
        f"<their_message>\n{(body or '').strip()[:1000]}\n</their_message>\n\n"
        "Classify it."
    )
