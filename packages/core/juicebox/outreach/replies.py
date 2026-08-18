"""Classify inbound replies so the sequencer knows whether to keep going."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..llm.client import get_llm
from ..llm.prompts import REPLY_SCHEMA, REPLY_SYSTEM, reply_prompt

logger = logging.getLogger(__name__)

INTENTS = (
    "interested",
    "not_interested",
    "not_now",
    "referral",
    "question",
    "out_of_office",
    "unsubscribe",
    "bounce",
)

# Ordered: the first pattern group that hits wins, so opt-outs and bounces are
# checked before softer signals.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("bounce", (
        "delivery status notification", "undeliverable", "mail delivery failed",
        "address not found", "550 5.1.1", "recipient rejected",
    )),
    ("unsubscribe", (
        "unsubscribe", "stop contacting", "remove me", "take me off", "do not contact",
        "opt out", "opt-out",
    )),
    ("out_of_office", (
        "out of office", "out of the office", "on annual leave", "on vacation",
        "away from my desk", "automatic reply", "auto-reply", "parental leave",
    )),
    # Referral outranks a decline: "not for me, but talk to Sam" is a lead, and
    # routing it to the not-interested pile throws that lead away.
    ("referral", (
        "you should talk to", "i can refer", "know someone", "colleague of mine",
        "introduce you to", "reach out to my", "might be a fit", "worth talking to",
    )),
    ("not_interested", (
        "not interested", "no thanks", "no thank you", "not looking", "happy where i am",
        "not a fit", "pass on this", "i'll pass", "not for me",
    )),
    ("not_now", (
        "not right now", "maybe later", "check back", "in a few months", "next year",
        "circle back", "keep me in mind", "not at the moment",
    )),
    ("interested", (
        "sounds interesting", "i'd be interested", "id be interested", "happy to chat",
        "let's talk", "lets talk", "tell me more", "send over", "available on",
        "book a time", "calendar", "would love to hear",
    )),
    ("question", (
        "what is the", "what's the", "how much", "can you share", "is this remote",
        "which team", "what does the role", "salary", "compensation",
    )),
]


@dataclass
class ReplyVerdict:
    intent: str
    confidence: float
    should_stop_sequence: bool
    summary: str = ""
    suggested_next_step: str = ""
    classified_by: str = "heuristic"


def _stop_for(intent: str) -> bool:
    return intent != "out_of_office"


_WHITESPACE_RE = re.compile(r"\s+")


def heuristic_classify(subject: str, body: str) -> ReplyVerdict:
    collapsed = _WHITESPACE_RE.sub(" ", f"{subject} {body}").lower()
    haystack = f" {collapsed} "
    for intent, needles in _PATTERNS:
        for needle in needles:
            if needle in haystack:
                return ReplyVerdict(
                    intent=intent,
                    confidence=0.7,
                    should_stop_sequence=_stop_for(intent),
                    summary=f"Matched phrase “{needle}”",
                )
    if "?" in body:
        return ReplyVerdict(
            intent="question",
            confidence=0.4,
            should_stop_sequence=True,
            summary="Reply contains a question",
        )
    return ReplyVerdict(
        intent="interested",
        confidence=0.25,
        should_stop_sequence=True,
        summary="Replied without a clear signal — routed to a human",
    )


def classify_reply(subject: str, body: str, *, use_llm: bool | None = None) -> ReplyVerdict:
    llm = get_llm()
    want_llm = llm.available if use_llm is None else (use_llm and llm.available)
    if want_llm:
        try:
            result = llm.json(
                system=REPLY_SYSTEM,
                prompt=reply_prompt(subject=subject, body=body),
                schema=REPLY_SCHEMA,
                effort="low",
                max_tokens=2000,
            )
            data = result.data or {}
            intent = str(data.get("intent", "")).strip()
            if intent in INTENTS:
                return ReplyVerdict(
                    intent=intent,
                    confidence=float(data.get("confidence", 0.8)),
                    should_stop_sequence=bool(
                        data.get("should_stop_sequence", _stop_for(intent))
                    ),
                    summary=str(data.get("summary", "")).strip(),
                    suggested_next_step=str(data.get("suggested_next_step", "")).strip(),
                    classified_by="llm",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reply classification failed: %s", exc)
    return heuristic_classify(subject, body)
