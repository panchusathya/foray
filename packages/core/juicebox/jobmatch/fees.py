"""Deterministic fee language on a job description.

Ordered like ``outreach/replies.py``: negatives first, then collocations. A bare
"agency" is not a signal — it fires on "no agencies please".
"""

from __future__ import annotations

import re

from ..llm.client import get_llm
from ..llm.prompts import FEE_PARSER_SCHEMA, FEE_PARSER_SYSTEM, fee_parser_prompt

_WHITESPACE_RE = re.compile(r"\s+")

_NEGATIVE = (
    "no agencies",
    "no agency",
    "agencies will not",
    "agencies need not",
    "agencies please do not",
    "please no agencies",
    "agencies will not be considered",
    "not open to agencies",
    "principals only",
)

_FEE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("referral bonus", ("referral bonus", "employee referral bonus", "referral award")),
    ("referral fee", ("referral fee", "referral commission")),
    ("finder's fee", ("finder's fee", "finders fee", "finder fee")),
    ("placement fee", ("placement fee", "placement bonus")),
    (
        "successful referral",
        (
            "for a successful referral",
            "successful referral",
            "$ for a successful",
        ),
    ),
]

_DOLLAR_REFERRAL_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\s*(?:k|usd))?\s+(?:referral|finder's|finder|placement)\b",
    re.IGNORECASE,
)


def fee_signals(jd_text: str, *, use_llm: bool | None = None) -> tuple[bool, str, str]:
    """Return (fee_bearing, terms, evidence)."""
    haystack = _WHITESPACE_RE.sub(" ", jd_text or "").strip()
    if not haystack:
        return False, "", ""
    lowered = haystack.lower()
    for needle in _NEGATIVE:
        if needle in lowered:
            return False, "", f"Matched negative phrase “{needle}”"

    terms, evidence = _regex_hit(haystack, lowered)
    llm = get_llm()
    want_llm = llm.available if use_llm is None else (use_llm and llm.available)
    if want_llm:
        try:
            result = llm.json(
                system=FEE_PARSER_SYSTEM,
                prompt=fee_parser_prompt(haystack[:6000]),
                schema=FEE_PARSER_SCHEMA,
                effort="low",
                max_tokens=800,
            )
            data = result.data or {}
            if data.get("fee_bearing"):
                return (
                    True,
                    (data.get("terms") or terms or "referral fee")[:240],
                    (data.get("evidence") or evidence or "")[:2000],
                )
            if terms:
                # Regex found a collocation; a model "no" does not un-see "$5,000 referral bonus".
                return True, terms, evidence
            return False, "", (data.get("evidence") or "")[:2000]
        except Exception:  # noqa: BLE001 - regex result is the fallback
            pass
    if terms:
        return True, terms, evidence
    return False, "", ""


def _regex_hit(haystack: str, lowered: str) -> tuple[str, str]:
    dollar = _DOLLAR_REFERRAL_RE.search(haystack)
    if dollar:
        snippet = dollar.group(0)
        return "referral bonus", snippet
    for terms, needles in _FEE_PATTERNS:
        for needle in needles:
            if needle in lowered:
                return terms, needle
    return "", ""
