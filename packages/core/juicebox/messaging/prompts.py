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
"""

from __future__ import annotations

from typing import Any

# Roughly one SMS segment and a bit. Long enough for an acknowledgement plus a
# question, short enough that the channel stays a conversation.
MAX_QUESTION_CHARS = 320

COMPOSE_SYSTEM = """\
You write one short text message for a recruiting intake assistant.

You are given the question that must be asked next and, usually, what the \
candidate just said. Rewrite the question so it reads as a natural reply: \
acknowledge their last message in a few words where there is something to \
acknowledge, then ask.

Hard rules:
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
            "description": "The message to send. One question, under 300 characters.",
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


def valid_question(text: str) -> bool:
    """Is this safe to send as-is?

    Structural checks only, and all of them exist because an instruction alone
    does not hold: a model asked for one question will occasionally send two,
    and the second one is always the one nobody wanted to answer.
    """
    body = (text or "").strip()
    if not body or len(body) > MAX_QUESTION_CHARS:
        return False
    if body.count("?") > 1:
        return False
    # A blank line means the model wrote two paragraphs, which on this channel
    # is two messages wearing one coat.
    return "\n\n" not in body


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
    "spec.job_titles": "What kind of role are you looking for next?",
    "spec.seniority": (
        "What level are you aiming for, and do you want to stay hands-on or move "
        "into management?"
    ),
    "spec.locations": "Whereabouts are you based, and which locations would you work in?",
    "spec.remote_ok": "Would you want fully remote, hybrid, or to be in an office?",
    "spec.must_have_skills": "What do you want to be hired to actually do day to day?",
    "employment_status": "Are you working at the moment, and how actively are you looking?",
    "timeline": "How soon would you want to move?",
    "compensation": "What are you targeting on compensation? Base or total is fine.",
    "work_authorization": (
        "Do you have the right to work where you are looking, or would you need "
        "sponsorship?"
    ),
    "active_processes": "Are you interviewing anywhere else at the moment?",
    "ok_to_share_with_clients": (
        "Is it OK for us to put your profile in front of companies we are hiring for?"
    ),
    # --- enrichment -------------------------------------------------------
    "highlights": "Is there a project or piece of work you are proudest of?",
    "spec.company_stages": (
        "What stage of company suits you best — early startup, growth, or established?"
    ),
    "spec.company_sizes": "Any preference on company size?",
    "spec.industries": "Are there industries you particularly want to work in?",
    "spec.nice_to_have_skills": (
        "Anything else you have worked with that you would be happy to use?"
    ),
    "reason_for_looking": "What is prompting the move?",
    "spec.excluded_companies": (
        "Are there companies you would not consider, or a non-compete we should know about?"
    ),
    "spec.target_companies": "Any companies you are already excited about?",
    "spec.freeform_criteria": "Anything that would be a dealbreaker for you?",
    "offers_in_hand": "Do you have any offers in hand, and are there deadlines on them?",
    "willing_to_relocate": "Would you consider relocating, and on what terms?",
    "equity_importance": "How much does equity matter to you against cash?",
    "urgency_driver": "Is there anything making the timing urgent?",
    "confidential_items": "Is there anything you would rather we did not repeat to a company?",
}


def question_for(field_key: str, fallback: str) -> str:
    """The candidate-facing wording, or the schema's topic line if it is new.

    A field added to ``FIELDS`` with no question here still gets asked --
    badly, but asked. Skipping it silently would leave a hole in the gate that
    nothing surfaces; the test makes sure it never gets that far.
    """
    return QUESTIONS.get(field_key) or fallback


GREETING = (
    "Hi — I'm the intake assistant for {org}. I'm an AI, and I'm here to take a "
    "few details so a recruiter can match you to the right roles. It takes a "
    "couple of minutes and you can stop any time."
)

WRAP_UP_COMPLETE = (
    "That's everything I needed — thank you. A recruiter will pick this up from "
    "here and will be in touch about roles that fit."
)

WRAP_UP_PARTIAL = (
    "Thanks — I'll pass on what we covered. A recruiter will follow up if they "
    "need anything else."
)

STOP_ACKNOWLEDGEMENT = "Understood — I won't message you again."

HELP_REPLY = (
    "Juicebox intake assistant. Reply STOP to opt out. "
    "For help, contact the recruiter who shared this number."
)
