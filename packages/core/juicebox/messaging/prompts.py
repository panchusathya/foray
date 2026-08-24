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
    return (get_settings().intake_recruiter_name or "Vivian").strip() or "Vivian"


def greeting_for(*, org: str, recruiter: str | None = None) -> str:
    name = (recruiter or recruiter_name()).strip() or "vivian"
    org_name = (org or "our team").strip() or "our team"
    return (
        f"hey, i'm {name} from {org_name}. i'm an ai, and i'm here to take a "
        f"few details so we can match you to the right roles. it takes a "
        f"couple of minutes and you can stop any time."
    )


# Kept as a format-string for callers that still pass ``{org}``. Prefer
# ``greeting_for``.
GREETING = (
    "hey, i'm {recruiter} from {org}. i'm an ai, and i'm here to take a "
    "few details so we can match you to the right roles. it takes a "
    "couple of minutes and you can stop any time."
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

# The first message of a candidate-agent thread. Compliance requirements all
# in one breath, because a shared number means the candidate cannot recognise
# the sender: who we are (the org), what this is (an AI), and how to stop.
MANAGER_GREETING = (
    "Hi — I'm the AI recruiting assistant for {org}. I can find roles that fit "
    "you, prepare applications for your review, and keep you posted — all here. "
    "Reply STOP any time to opt out, HELP for info."
)

ASK_EMAIL_MANAGER = "First — what's the best email to reach you on?"

ASK_NAME_MANAGER = "And what name should I put on things?"

# The resume nudge: the flexibility is the feature (either works), the nudge
# is the resume — it carries more than a profile scrape does.
ASK_LINKEDIN = (
    "Best way to get you set up: send your LinkedIn URL, or better, upload "
    "your resume here: {upload_link}"
)

PROFILE_INGEST_ACK = (
    "Got it — pulling your profile together now. A couple of quick questions "
    "while that runs."
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

DETAIL_UNAVAILABLE = "I don't have more detail on that one yet — want me to flag it to a recruiter?"

CLARIFY_WHICH = "Which one do you mean? Reply with the number from the list."

APPLY_ACK = (
    "On it — I'm preparing your application for {title} at {company}. "
    "I'll text you when it's ready to review."
)

APPLY_PARKED = (
    "I've noted you want {title} at {company}. You're at this week's "
    "application limit, so I'll queue it and check back with you."
)

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

NOTIFY_STATUS_CHANGED = "Update on your applications: {summary}"

# Presenting cards relaxes the 320-char single-question budget: a numbered
# block of three roles is legitimately longer. Still hard-capped, and still
# exactly one question (the footer's "reply with a number").
MAX_CARD_MESSAGE_CHARS = 900


INTENT_SYSTEM = """\
You classify one inbound text from a job candidate talking to their recruiting
assistant. Pick exactly one intent:

- jobs_more: they want to see roles ("find me roles", "got anything?")
- jobs_detail: they want more about a specific role already shown
- apply: they want to apply to a specific role already shown
- status: they ask where their applications stand
- networking: they ask who to talk to / how to get an intro
- profile_answer: they are answering a profile/preference question
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
