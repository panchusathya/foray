"""Work-email pattern inference.

We never claim a guessed address is verified — every guess carries a confidence
so the outreach layer can decide whether to send or ask a human first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .taxonomy import slugify_text

_NON_ALNUM = re.compile(r"[^a-z0-9]")

PATTERNS: dict[str, str] = {
    "first.last": "{first}.{last}",
    "firstlast": "{first}{last}",
    "first_last": "{first}_{last}",
    "flast": "{f}{last}",
    "firstl": "{first}{l}",
    "first": "{first}",
    "last.first": "{last}.{first}",
    "f.last": "{f}.{last}",
}

# Rough prior on how common each layout is; used when we have no observed sample.
PATTERN_PRIORS: dict[str, float] = {
    "first.last": 0.45,
    "firstlast": 0.15,
    "flast": 0.15,
    "first": 0.10,
    "first_last": 0.05,
    "firstl": 0.04,
    "f.last": 0.04,
    "last.first": 0.02,
}


@dataclass
class EmailGuess:
    email: str
    pattern: str
    confidence: float
    verified: bool = False


def _slug(value: str) -> str:
    return _NON_ALNUM.sub("", slugify_text(value))


def render_pattern(pattern_key: str, first: str, last: str, domain: str) -> str:
    first_s, last_s = _slug(first), _slug(last)
    if not first_s or not domain:
        return ""
    template = PATTERNS.get(pattern_key)
    if template is None:
        return ""
    if "{last}" in template and not last_s:
        return ""
    local = template.format(
        first=first_s, last=last_s, f=first_s[:1], l=last_s[:1] if last_s else ""
    )
    return f"{local}@{domain.lower().lstrip('@')}"


def detect_pattern(known_email: str, first: str, last: str) -> str | None:
    """Infer which layout an already-known address uses."""
    if "@" not in known_email:
        return None
    local, domain = known_email.lower().split("@", 1)
    for key in PATTERNS:
        if render_pattern(key, first, last, domain).split("@")[0] == local:
            return key
    return None


def guess_emails(
    first: str,
    last: str,
    domain: str,
    *,
    known_pattern: str | None = None,
    limit: int = 3,
) -> list[EmailGuess]:
    """Ranked guesses for someone's work address at ``domain``."""
    if not domain:
        return []

    ordered = sorted(PATTERN_PRIORS.items(), key=lambda kv: kv[1], reverse=True)
    guesses: list[EmailGuess] = []

    if known_pattern and known_pattern in PATTERNS:
        email = render_pattern(known_pattern, first, last, domain)
        if email:
            guesses.append(EmailGuess(email=email, pattern=known_pattern, confidence=0.92))

    for key, prior in ordered:
        if known_pattern and key == known_pattern:
            continue
        email = render_pattern(key, first, last, domain)
        if not email or any(g.email == email for g in guesses):
            continue
        confidence = prior * (0.35 if known_pattern else 1.0)
        guesses.append(EmailGuess(email=email, pattern=key, confidence=round(confidence, 3)))
        if len(guesses) >= limit:
            break
    return guesses[:limit]


def learn_org_pattern(samples: list[tuple[str, str, str]]) -> str | None:
    """Given (email, first, last) samples from one domain, pick the dominant layout."""
    tally: dict[str, int] = {}
    for email, first, last in samples:
        pattern = detect_pattern(email, first, last)
        if pattern:
            tally[pattern] = tally.get(pattern, 0) + 1
    if not tally:
        return None
    return max(tally.items(), key=lambda kv: kv[1])[0]
