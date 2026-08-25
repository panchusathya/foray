#!/usr/bin/env python3
"""Guardrails for the Retell agent prompts.

Run from the repo root:

    python deploy/tests/check_agent_prompts.py

Stdlib only, like the rest of ``deploy/``. Imports the prompt text out of
``retell_agents.py`` rather than re-reading the file, so a rename or an f-string
change cannot leave this passing against text nobody ships.

**Why the inbound prompt gets a guard at all.** It is the one piece of this
system with no test around it -- it is not code, it runs on Retell's model, and
it reaches real callers the moment someone runs ``apply``. Its failure mode is
also the quietest one here: a line that assumes a prior conversation reads fine
to whoever writes it, and only misfires for the callers who do not have one. The
agent greeting a stranger with "what's changed since we last spoke?" is a false
claim about that person, and it is the sentence a well-meaning edit reintroduces
first. So the assumption is pinned here in the form a reviewer can see.

This checks the prompt does not *presume* a relationship. It cannot check what
the model says, and it is not trying to: the model is instructed to ask, and
this keeps the instruction from quietly reverting to an assertion.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

from retell_agents import INBOUND_PROMPT, OUTBOUND_PROMPT


class Failed(Exception):
    pass


def _must(cond: bool, msg: str) -> None:
    if not cond:
        raise Failed(msg)


def _flat(text: str) -> str:
    """Lowercased with runs of whitespace collapsed.

    The prompts are hand-wrapped at 79 columns, so a phrase to match against
    almost always has a newline somewhere in the middle of it. Matching the
    wrapped form would make these checks pass or fail on where the line breaks
    happen to fall.
    """
    return re.sub(r"\s+", " ", text).lower()


INBOUND = _flat(INBOUND_PROMPT)
OUTBOUND = _flat(OUTBOUND_PROMPT)


# Openers that state a prior conversation as fact. The inbound line cannot know
# it has one -- caller ID is spoofable and frequently withheld -- so every one of
# these is a guess presented to the caller as knowledge.
_PRESUMPTIONS = (
    "since you last spoke",
    "since they last spoke",
    "since we last spoke",
    "when we last spoke",
    "last time we spoke",
    "welcome back",
    "good to hear from you again",
    "speaking again",
)


def check_inbound_does_not_presume_a_prior_call() -> None:
    for phrase in _PRESUMPTIONS:
        # Allowed only where the prompt is forbidding the phrase, or is inside
        # the branch the caller has already confirmed. Both of those spell it
        # out, so require the guard words to be present rather than trying to
        # parse the sentence.
        if phrase not in INBOUND:
            continue
        _must(
            "have you spoken with anyone at foray before" in INBOUND,
            f"inbound prompt uses {phrase!r} without first asking whether the "
            "caller has spoken to Foray -- that asserts a relationship to a "
            "first-time caller",
        )


def check_inbound_asks_before_it_assumes() -> None:
    _must(
        "have you spoken with anyone at foray before" in INBOUND,
        "inbound prompt must ask, in an open question, whether the caller has "
        "spoken to Foray before",
    )
    _must(
        "treat them as someone you have never spoken to" in INBOUND,
        "inbound prompt must default an unconfirmed caller to unknown",
    )
    _must(
        "unsure counts as new" in INBOUND,
        "inbound prompt must send an unsure or non-answering caller down the "
        "full-intake branch, not the returning-caller branch",
    )


def check_inbound_still_refuses_disclosure() -> None:
    """The pre-existing rule this change must not have loosened."""
    _must(
        "cannot verify who is on the phone" in INBOUND,
        "inbound prompt must keep the do-not-disclose rule",
    )
    _must(
        "never greet the caller by a name they have not given you" in INBOUND,
        "inbound prompt must not greet by a name taken from a matched record",
    )


def check_outbound_may_still_presume() -> None:
    """The outbound call is the other case, and must not be over-corrected.

    We dialled them because they filled in our form minutes ago, so referring to
    that is accurate. The guard exists to stop the inbound fix leaking here and
    making the outbound agent act like it has never heard of the person.
    """
    _must(
        "asked to be called" in OUTBOUND,
        "outbound prompt should still say why we are calling",
    )
    _must(
        "{{candidate_first_name}}" in OUTBOUND_PROMPT
        or "{{recruiter_name}}" in OUTBOUND_PROMPT,
        "outbound prompt must keep its dynamic variables",
    )


def main() -> int:
    checks = (
        check_inbound_does_not_presume_a_prior_call,
        check_inbound_asks_before_it_assumes,
        check_inbound_still_refuses_disclosure,
        check_outbound_may_still_presume,
    )
    failed = 0
    for check in checks:
        try:
            check()
            print(f"ok  {check.__name__}")
        except Failed as exc:
            failed += 1
            print(f"FAIL  {check.__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
