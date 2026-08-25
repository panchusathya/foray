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
* a caller-ID sentinel — **no key**, see below
* anything else — **no key**

Refusing to guess is the point. A bare 9-digit string is a valid subscriber
number in several countries, and picking one would silently attach a caller to
somebody else's record. No key means the caller is treated as unknown, which
costs one extra question on the call and leaks nothing.

The sentinel rule exists because a withheld caller ID is not delivered as a
blank. The network substitutes a fixed placeholder — most often the word
spelled on a keypad, ``ANONYMOUS`` arriving as ``+266696687`` — and every
caller who withholds their number gets the *same* one. That is the failure this
module was written to prevent, wearing a valid-looking E.164 shape: without the
rule, the placeholder is a well-formed key, so it can be stored as a person's
number and then matched against, and the second anonymous caller is identified
as the first. Refusing the key here covers matching, verification and storage at
once, because this function is the only thing that writes ``candidate_phones``.
A sentinel means "no caller ID was delivered", so the honest key is no key.

The projection is therefore a subset of the JSON, exactly as
``candidate_emails`` is: a number can be stored on a candidate and still have no
row here.
"""

from __future__ import annotations

import re

# Digits only, plus a leading '+' if the caller gave one.
_KEEP = re.compile(r"[^\d+]")

DEFAULT_REGION_CODE = "1"

# What a carrier sends instead of a number when caller ID is withheld or the
# network cannot supply one. These are the keypad spellings of the words that
# show up on a handset, which is how they reach us as digits. A word that
# survives `_KEEP` as no digits at all ("ANONYMOUS", "unknown") already gets no
# key; these are the ones that arrive pre-translated and look like real numbers.
SENTINEL_NUMBERS = frozenset(
    {
        "266696687",  # ANONYMOUS
        "7378742833",  # RESTRICTED
        "86282452253",  # UNAVAILABLE
        "8656969",  # UNKNOWN
        "7748283",  # PRIVATE
        "94844353",  # WITHHELD
        "2562533",  # BLOCKED
    }
)


def is_sentinel_number(digits: str, *, region_code: str = DEFAULT_REGION_CODE) -> bool:
    """True when ``digits`` is a placeholder rather than somebody's number.

    Checked with the region code stripped as well, because a sentinel that
    reaches a North American carrier is often passed on with a ``1`` glued to
    the front, and ``+1266696687`` is the same non-number as ``+266696687``.

    A single repeated digit is folded in here rather than listed: ``0000000000``
    and ``9999999999`` are the other thing switches emit for "no caller ID", and
    no real E.164 number is one digit repeated. Like the listed sentinels it is
    tested against the region-stripped form too, because ``+10000000000`` is the
    same non-number as ``0000000000`` and only the stripped form is repeated.
    """
    candidates = {digits}
    if region_code and digits.startswith(region_code):
        candidates.add(digits[len(region_code) :])
    return any(
        form in SENTINEL_NUMBERS or len(set(form)) == 1 for form in candidates if form
    )


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

    # Before any shape check: a placeholder is not a person, whatever it looks
    # like. Ordered first so a sentinel cannot slip through on a branch that a
    # later change adds below.
    if is_sentinel_number(digits, region_code=region_code):
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
