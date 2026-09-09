"""Classify inbound replies so the sequencer knows whether to keep going."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

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

# A machine wrote these. Everything else is a person, and a person answering
# is the one signal that must stop automated mail -- whichever sequence, and
# whichever mailbox, it reached us through.
AUTOMATED_INTENTS = frozenset({"out_of_office", "bounce"})
HUMAN_INTENTS = frozenset(INTENTS) - AUTOMATED_INTENTS
# The intents where a date in the reply means "resume/contact after this".
DATED_INTENTS = frozenset({"not_now", "out_of_office"})

# An auto-responder announces itself in the subject line. A person who types
# "out of office until Monday" under "Re: your role" is telling us to come
# back later, which is not_now -- routing that to out_of_office kept a real
# sequence firing at someone who had just asked us to wait (a real incident).
_OOO_SUBJECT_MARKERS = (
    "automatic reply",
    "auto-reply",
    "autoreply",
    "auto reply",
    "out of office",
    "out of the office",
)
# Templated auto-responder wording that no candidate types by hand.
_OOO_BODY_TEMPLATES = (
    "this is an automated",
    "this is an automatic",
    "automatic reply",
    "auto-reply",
    "i am currently out of the office",
    "i am out of the office",
    "i'm out of the office",
    "i will be out of the office",
    "currently out of office",
    "limited access to email",
    "limited access to my email",
    "consistent access to email",
    "will respond when i return",
    "respond to your message when i return",
    "upon my return",
    "when i return to the office",
)

# Ordered: the first pattern group that hits wins, so opt-outs and bounces are
# checked before softer signals.
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (
        "bounce",
        (
            "delivery status notification",
            "undeliverable",
            "mail delivery failed",
            "address not found",
            "550 5.1.1",
            "recipient rejected",
        ),
    ),
    (
        "unsubscribe",
        (
            "unsubscribe",
            "stop contacting",
            "remove me",
            "take me off",
            "do not contact",
            "opt out",
            "opt-out",
        ),
    ),
    # Referral outranks a decline: "not for me, but talk to Sam" is a lead, and
    # routing it to the not-interested pile throws that lead away.
    (
        "referral",
        (
            "you should talk to",
            "i can refer",
            "know someone",
            "colleague of mine",
            "introduce you to",
            "reach out to my",
            "might be a fit",
            "worth talking to",
        ),
    ),
    (
        "not_interested",
        (
            "not interested",
            "no thanks",
            "no thank you",
            "not looking",
            "happy where i am",
            "not a fit",
            "pass on this",
            "i'll pass",
            "not for me",
        ),
    ),
    (
        "not_now",
        (
            "not right now",
            "maybe later",
            "check back",
            "in a few months",
            "next year",
            "circle back",
            "keep me in mind",
            "not at the moment",
            # A person saying they are away. The date, when there is one, is
            # picked up separately so the sequencer knows when "after" is.
            "f/u after",
            "follow up after",
            "follow-up after",
            "followup after",
            "follow up in",
            "follow-up in",
            "reach out after",
            "ping me after",
            "try me after",
            "back in touch",
            "get back to you after",
            "on leave until",
            "on vacation until",
            "out of office until",
            "out of the office until",
        ),
    ),
    (
        "interested",
        (
            "sounds interesting",
            "i'd be interested",
            "id be interested",
            "happy to chat",
            "let's talk",
            "lets talk",
            "tell me more",
            "send over",
            "available on",
            "book a time",
            "calendar",
            "would love to hear",
        ),
    ),
    (
        "question",
        (
            "what is the",
            "what's the",
            "how much",
            "can you share",
            "is this remote",
            "which team",
            "what does the role",
            "salary",
            "compensation",
            "more details",
            "more information",
            "more info",
        ),
    ),
]


@dataclass
class ReplyVerdict:
    intent: str
    confidence: float
    should_stop_sequence: bool
    summary: str = ""
    suggested_next_step: str = ""
    classified_by: str = "heuristic"
    # The date the candidate named for hearing from us again ("until 9/10",
    # "back Sept 9th", "in six months"). The date itself, not the day after:
    # the sequencer decides what "after" means.
    follow_up_after: date | None = None


def _stop_for(intent: str) -> bool:
    return intent != "out_of_office"


_WHITESPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# follow-up dates
# --------------------------------------------------------------------------

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_RE = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|20\d{2}))?\b")
_MONTH_DAY_RE = re.compile(
    rf"\b{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b", re.IGNORECASE
)
_DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RE}(?:,?\s+(20\d{{2}}))?\b",
    re.IGNORECASE,
)
_PART_MONTH_RE = re.compile(rf"\b(early|mid|late)[\s-]+{_MONTH_RE}\b", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"\bin\s+(a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|a\s+few|a\s+couple\s+of|couple\s+of|few|\d{1,2})\s+(day|week|month)s?\b",
    re.IGNORECASE,
)
_NEXT_RE = re.compile(r"\bnext\s+(week|month|quarter|year)\b", re.IGNORECASE)
_WORD_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "few": 3,
    "a few": 3,
    "couple of": 2,
    "a couple of": 2,
}
# Anything further out than this is a typo or a quoted year, not a plan.
_MAX_HORIZON = timedelta(days=400)


def _month_number(token: str) -> int | None:
    key = token.lower().rstrip(".")
    return _MONTHS.get(key) or _MONTHS.get(key[:3])


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _resolve_year(month: int, day: int, year: int | None, today: date) -> date | None:
    if year is not None:
        if year < 100:
            year += 2000
        return _safe_date(year, month, day)
    candidate = _safe_date(today.year, month, day)
    if candidate is None:
        return None
    if candidate < today:
        # "until 1/15" written in December means next January.
        candidate = _safe_date(today.year + 1, month, day)
    return candidate


def parse_follow_up_date(text: str, received_at: datetime | date | None) -> date | None:
    """The first date a reply names as when to be in touch again, or None.

    Pure text work, no model: it backs the heuristic classifier and repairs an
    unparseable model date. Relative phrases ("in six weeks", "next month")
    count from ``received_at``. Ambiguous ``m/d`` is read the US way, which is
    where the outreach goes. Past dates are not follow-ups and return None.
    """
    if not text:
        return None
    if isinstance(received_at, datetime):
        today = received_at.date()
    elif isinstance(received_at, date):
        today = received_at
    else:
        today = datetime.now(UTC).date()

    found: list[tuple[int, date]] = []

    for match in _ISO_RE.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        resolved = _safe_date(year, month, day)
        if resolved:
            found.append((match.start(), resolved))
    for match in _NUMERIC_RE.finditer(text):
        month, day = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        resolved = _resolve_year(month, day, year, today)
        if resolved:
            found.append((match.start(), resolved))
    for match in _MONTH_DAY_RE.finditer(text):
        month = _month_number(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None
        if month and 1 <= day <= 31:
            resolved = _resolve_year(month, day, year, today)
            if resolved:
                found.append((match.start(), resolved))
    for match in _DAY_MONTH_RE.finditer(text):
        day = int(match.group(1))
        month = _month_number(match.group(2))
        year = int(match.group(3)) if match.group(3) else None
        if month and 1 <= day <= 31:
            resolved = _resolve_year(month, day, year, today)
            if resolved:
                found.append((match.start(), resolved))
    for match in _PART_MONTH_RE.finditer(text):
        month = _month_number(match.group(2))
        if month:
            day = {"early": 1, "mid": 15, "late": 25}[match.group(1).lower()]
            resolved = _resolve_year(month, day, None, today)
            if resolved:
                found.append((match.start(), resolved))
    for match in _RELATIVE_RE.finditer(text):
        raw = _WHITESPACE_RE.sub(" ", match.group(1).lower())
        count = _WORD_NUMBERS.get(raw)
        if count is None:
            try:
                count = int(raw)
            except ValueError:
                continue
        unit = match.group(2).lower()
        days = {"day": 1, "week": 7, "month": 30}[unit] * count
        found.append((match.start(), today + timedelta(days=days)))
    for match in _NEXT_RE.finditer(text):
        days = {"week": 7, "month": 30, "quarter": 91, "year": 365}[match.group(1).lower()]
        found.append((match.start(), today + timedelta(days=days)))

    for _, resolved in sorted(found, key=lambda item: item[0]):
        if resolved < today or resolved - today > _MAX_HORIZON:
            continue
        return resolved
    return None


def _date_for(intent: str, body: str, received_at: datetime | date | None) -> date | None:
    if intent not in DATED_INTENTS:
        return None
    return parse_follow_up_date(body, received_at)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def heuristic_classify(
    subject: str, body: str, *, received_at: datetime | date | None = None
) -> ReplyVerdict:
    subject_text = f" {_WHITESPACE_RE.sub(' ', subject or '').lower()} "
    body_text = f" {_WHITESPACE_RE.sub(' ', body or '').lower()} "
    haystack = f"{subject_text}{body_text}"

    def verdict(intent: str, confidence: float, summary: str) -> ReplyVerdict:
        return ReplyVerdict(
            intent=intent,
            confidence=confidence,
            should_stop_sequence=_stop_for(intent),
            summary=summary,
            follow_up_after=_date_for(intent, body or "", received_at),
        )

    for intent, needles in _PATTERNS[:2]:  # bounce, unsubscribe
        for needle in needles:
            if needle in haystack:
                return verdict(intent, 0.7, f"Matched phrase “{needle}”")

    for needle in _OOO_SUBJECT_MARKERS:
        if needle in subject_text:
            return verdict("out_of_office", 0.8, f"Auto-reply subject “{needle}”")
    for needle in _OOO_BODY_TEMPLATES:
        if needle in body_text:
            return verdict("out_of_office", 0.7, f"Auto-reply wording “{needle}”")

    for intent, needles in _PATTERNS[2:]:
        for needle in needles:
            if needle in haystack:
                return verdict(intent, 0.7, f"Matched phrase “{needle}”")

    # No phrase, but a date: "In Japan until 9/10" is a person asking us to
    # wait, and the date is the whole message.
    if parse_follow_up_date(body or "", received_at) is not None:
        return verdict("not_now", 0.6, "Reply names a date to come back on")

    if "?" in (body or ""):
        return verdict("question", 0.4, "Reply contains a question")
    return verdict("interested", 0.25, "Replied without a clear signal — routed to a human")


def _model_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def classify_reply(
    subject: str,
    body: str,
    *,
    received_at: datetime | None = None,
    use_llm: bool | None = None,
) -> ReplyVerdict:
    received = received_at or datetime.now(UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    llm = get_llm()
    want_llm = llm.available if use_llm is None else (use_llm and llm.available)
    if want_llm:
        try:
            result = llm.json(
                system=REPLY_SYSTEM,
                prompt=reply_prompt(subject=subject, body=body, received_at=received),
                schema=REPLY_SCHEMA,
                effort="low",
                max_tokens=2000,
            )
            data = result.data or {}
            intent = str(data.get("intent", "")).strip()
            if intent in INTENTS:
                follow_up = None
                if intent in DATED_INTENTS:
                    follow_up = _model_date(data.get("follow_up_after"))
                    today = received.date()
                    if follow_up is not None and (
                        follow_up < today or follow_up - today > _MAX_HORIZON
                    ):
                        follow_up = None
                    if follow_up is None:
                        follow_up = parse_follow_up_date(body, received)
                return ReplyVerdict(
                    intent=intent,
                    confidence=float(data.get("confidence", 0.8)),
                    # Policy, not the model's opinion: a model that returned
                    # should_stop_sequence=false for a human reply let a
                    # sequence keep mailing someone who had answered it.
                    should_stop_sequence=_stop_for(intent),
                    summary=str(data.get("summary", "")).strip(),
                    suggested_next_step=str(data.get("suggested_next_step", "")).strip(),
                    classified_by="llm",
                    follow_up_after=follow_up,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reply classification failed: %s", exc)
    return heuristic_classify(subject, body, received_at=received)
