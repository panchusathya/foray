"""What did the candidate just ask for?

Deterministic first, model second, and the failure mode is always OTHER: a
turn must never die on classification, and OTHER lands in a canned deflection
rather than a wrong action. The keyword pass exists so the whole managing mode
works with no API key — flatter, not broken, same contract as everything else
in this package.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ..llm.client import get_llm
from . import prompts

logger = logging.getLogger(__name__)

INTENT_JOBS_MORE = "jobs_more"
INTENT_JOBS_DETAIL = "jobs_detail"
INTENT_APPLY = "apply"
INTENT_APPLY_LINK = "apply_link"
INTENT_STATUS = "status"
INTENT_NETWORKING = "networking"
INTENT_PROFILE_ANSWER = "profile_answer"
INTENT_DECLINE = "decline"
INTENT_AFFIRM = "affirm"
INTENT_OTHER = "other"

# Bare confirmations and refusals, matched on the whole message only -- "no"
# inside "no rush, show me more" must not read as a refusal.
_NEGATIVE = frozenset(
    {"no", "nope", "nah", "no thanks", "no thank you", "not now", "nothing", "nah im good",
     "im good", "i'm good", "all good", "not right now", "no im good", "no i'm good"}
)
_AFFIRMATIVE = frozenset(
    {"yes", "yep", "yeah", "yup", "sure", "ok", "okay", "sounds good", "please", "yes please",
     "go ahead", "do it"}
)


@dataclass(frozen=True)
class Intent:
    kind: str
    # 1-based index into the presented list; 0 = no specific role referenced.
    selection: int = 0
    # The job link an APPLY_LINK message carried; "" for every other intent.
    url: str = ""


_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "last": -1,
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
    "4th": 4,
    "5th": 5,
}
_NUMBER_RE = re.compile(r"(?:^|\s|#)(\d{1,2})(?:\s|$|\b)")

_APPLY_WORDS = ("apply", "go for it", "submit", "yes to", "i want that", "let's do")
_MORE_WORDS = ("more roles", "more jobs", "find me", "got anything", "other roles",
               "what else", "show me roles", "show me jobs", "any roles", "any jobs")
_STATUS_WORDS = ("status", "where do things stand", "any update", "hear back",
                 "what's happening with", "did they respond")
_NETWORK_WORDS = ("who should i talk", "who do you know", "intro", "referral",
                  "connect me", "who can i reach")
_DETAIL_WORDS = ("tell me more", "more about", "more info", "details on", "what about",
                 "which one is", "say more")


def resolve_reference(body: str, presented: list[dict[str, Any]]) -> int:
    """Which shown card a message points at. 0 when it doesn't, or is ambiguous.

    Ambiguity resolves to 0 on purpose: the manager asks a one-line clarifier,
    which costs one message; acting on the wrong role costs an application.
    """
    if not presented:
        return 0
    text = (body or "").strip().lower()

    match = _NUMBER_RE.search(text)
    if match:
        number = int(match.group(1))
        if 1 <= number <= len(presented):
            return number

    for word, position in _ORDINALS.items():
        if re.search(rf"\b{word}\b", text):
            index = len(presented) if position == -1 else position
            if 1 <= index <= len(presented):
                return index

    hits: list[int] = []
    for index, item in enumerate(presented, start=1):
        company = str(item.get("company") or "").strip().lower()
        title = str(item.get("title") or "").strip().lower()
        # Tokens under 4 chars substring-match half of English ("a", "IB");
        # a reference that short goes to the clarifier instead of a guess.
        company_token = company.split(" ")[0] if company else ""
        if len(company_token) >= 4 and company_token in text:
            hits.append(index)
            continue
        if len(title) >= 4 and title in text:
            hits.append(index)
    if len(hits) == 1:
        return hits[0]
    return 0


def _bare(text: str) -> str:
    """The message with surrounding punctuation gone, for whole-message matching."""
    return text.strip().strip(".!?,;: ").strip()


def classify(body: str, *, presented: list[dict[str, Any]]) -> Intent:
    text = (body or "").strip().lower()
    if not text:
        return Intent(INTENT_OTHER)
    selection = resolve_reference(body, presented)

    # A job link outranks every keyword: "apply to this <url>" is about the
    # URL, not about the cards on screen. Deterministic, so a texted link
    # works with no API key. LinkedIn profile URLs are excluded inside
    # find_job_link -- those are identity, absorbed elsewhere.
    from .apply_link import find_job_link

    link = find_job_link(body or "")
    if link:
        return Intent(INTENT_APPLY_LINK, selection, url=link)

    if any(word in text for word in _APPLY_WORDS):
        return Intent(INTENT_APPLY, selection)
    if any(word in text for word in _STATUS_WORDS):
        return Intent(INTENT_STATUS)
    if any(word in text for word in _NETWORK_WORDS):
        return Intent(INTENT_NETWORKING, selection)
    if any(word in text for word in _MORE_WORDS):
        return Intent(INTENT_JOBS_MORE)
    if any(word in text for word in _DETAIL_WORDS) or (
        selection and len(text) <= 40
    ):
        # "2" or "the Goldman one" on its own is a detail request.
        return Intent(INTENT_JOBS_DETAIL, selection)

    # A bare yes or no, decided here rather than by the model. "No" was being
    # classified as a profile answer, so the agent replied "Noted -- thanks"
    # to somebody declining, then asked the same question again. These are
    # the cheapest words in the language to get wrong and the most obvious
    # ones to a reader.
    if _bare(text) in _NEGATIVE:
        return Intent(INTENT_DECLINE)
    if _bare(text) in _AFFIRMATIVE:
        return Intent(INTENT_AFFIRM)

    llm = get_llm()
    if not llm.available:
        # OTHER, never a guess at PROFILE_ANSWER. Preference corrections are
        # already absorbed by the manager's re-extraction every turn, so
        # nothing is lost by admitting we did not understand -- whereas
        # answering "noted, thanks" to a question makes the agent look like
        # it is ignoring people.
        return Intent(INTENT_OTHER)
    try:
        result = llm.json_fast(
            system=prompts.INTENT_SYSTEM,
            prompt=prompts.intent_prompt(body=body, presented=presented),
            schema=prompts.INTENT_SCHEMA,
        )
        data = result.data or {}
        kind = str(data.get("intent") or INTENT_OTHER)
        model_selection = int(data.get("selection") or 0)
        if not 0 <= model_selection <= len(presented):
            model_selection = 0
        return Intent(kind, selection or model_selection)
    except Exception as exc:  # noqa: BLE001 - degrade, never fail a turn
        logger.warning("intent classification failed, treating as other: %s", exc)
        return Intent(INTENT_OTHER)
