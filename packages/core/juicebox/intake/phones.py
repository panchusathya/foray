"""Turning a phone number into a key you can safely look a person up by.

The dial-in line has to answer "who is calling?" from caller ID alone, and
``Candidate.phones`` is a JSON list, so the question cannot be a join — the same
problem ``candidate_emails`` was built to solve, with a sharper failure mode. A
mismatched email lookup finds nobody; a mismatched *phone* lookup can find the
wrong person and read their salary and interview pipeline to a stranger.

So this is deliberately stricter than ``enrich.normalize.clean_phones``, which
exists to tidy numbers for storage and keeps anything with seven or more digits.
A lookup key is only produced for a number whose country is actually known:

* already E.164 (``+``-prefixed) — trusted as given, which is what a voice
  platform normally hands us
* 11 digits starting with 1, or 10 digits — North American, the default region
* anything else — **no key**

Refusing to guess is the point. A bare 9-digit string is a valid subscriber
number in several countries, and picking one would silently attach a caller to
somebody else's record. No key means the caller is treated as unknown, which
costs one extra question on the call and leaks nothing.

The projection is therefore a subset of the JSON, exactly as
``candidate_emails`` is: a number can be stored on a candidate and still have no
row here.
"""

from __future__ import annotations

import re

# Digits only, plus a leading '+' if the caller gave one.
_KEEP = re.compile(r"[^\d+]")

DEFAULT_REGION_CODE = "1"


def lookup_key(value: str | None, *, region_code: str = DEFAULT_REGION_CODE) -> str:
    """E.164 key for ``value``, or ``""`` when the country is not knowable."""
    if not value:
        return ""
    cleaned = _KEEP.sub("", str(value))
    if not cleaned:
        return ""

    explicit = cleaned.startswith("+")
    digits = cleaned.lstrip("+").replace("+", "")
    if not digits:
        return ""

    if explicit:
        # E.164 allows up to 15 digits; a country code is at least one, so
        # anything shorter than a plausible subscriber number is junk.
        return f"+{digits}" if 8 <= len(digits) <= 15 else ""

    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+{region_code}{digits}"
    return ""


def lookup_keys(values: list[str] | None, *, region_code: str = DEFAULT_REGION_CODE) -> list[str]:
    """Keys for a candidate's stored numbers, order preserved, deduped.

    Numbers that cannot produce a key are dropped rather than approximated, so
    the caller sees a short list instead of a wrong one.
    """
    out: list[str] = []
    for value in values or []:
        key = lookup_key(value, region_code=region_code)
        if key and key not in out:
            out.append(key)
    return out
