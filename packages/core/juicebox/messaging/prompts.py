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


def lowercase_outbound(text: str) -> str:
    """Lowercase a message while leaving URLs untouched."""
    placeholders: dict[str, str] = {}

    def _hold(match: re.Match[str]) -> str:
        key = f"@@URL{len(placeholders)}@@"
        placeholders[key] = match.group(0)
        return key

    held = _URL.sub(_hold, text or "")
    lowered = held.lower()
    for key, url in placeholders.items():
        lowered = lowered.replace(key.lower(), url)
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
