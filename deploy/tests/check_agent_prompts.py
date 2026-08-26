#!/usr/bin/env python3
"""Guardrails for the Retell agent prompts.

Run from the repo root:

    python deploy/tests/check_agent_prompts.py

Stdlib only, like the rest of ``deploy/``. Imports the prompt text out of
``retell_agents.py`` rather than re-reading the file, so a rename or an f-string
change cannot leave this passing against text nobody ships.

**Why the prompts get a guard at all.** They are the one piece of this system
with no test around them -- not code, run on Retell's model, and live to real
callers the moment someone runs ``apply``. Their failure modes are also the
quietest: a line that assumes the wrong thing reads fine to whoever wrote it and
only misfires for the callers it is wrong about.

Two things are pinned here, and they pull in opposite directions:

* the inbound agent must **use** what caller ID told it -- greeting a returning
  candidate by name, and reading their own record back when they ask. The
  interrogation this replaced ("have you spoken to us before?", "I can't tell
  you anything about your record") was the bug.
* it must not **claim** that for a caller we could not place. `caller_known` is
  false for a new number and for a withheld one, and that branch runs a normal
  first intake with no implied history.

What decides which is `caller_known`, resolved from the phone number before the
agent speaks. Neither branch involves asking, and the check below is that the
asking has not crept back in.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

from retell_agents import (
    DEFAULT_DYNAMIC_VARIABLES,
    INBOUND_PROMPT,
    OUTBOUND_PROMPT,
)


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


def _instructions_only(text: str) -> str:
    """The prompt with its prohibitions dropped.

    A rule forbidding a phrase necessarily contains that phrase, so scanning the
    raw text for "have you spoken to Foray before" matches the very line that
    bans it. Sentences telling the agent *not* to do something are removed
    before the ban list runs, leaving only what the agent is being told to do.
    """
    kept = []
    for sentence in re.split(r"(?<=[.:])\s+", text):
        if re.search(r"\b(do not|don't|never|must not|no need to)\b", sentence):
            continue
        kept.append(sentence)
    return " ".join(kept)


INBOUND_INSTRUCTIONS = _instructions_only(INBOUND)


# Ways of making the caller establish their own identity. Every one of these is
# something the phone number already answered before the agent spoke.
_INTERROGATIONS = (
    "have you spoken with anyone at foray before",
    "have you spoken to foray before",
    "whether they have spoken to foray",
    "ask whether they have spoken",
    "confirm who they are",
    "unsure counts as new",
)


def check_inbound_does_not_ask_who_it_is_talking_to() -> None:
    """The interrogation is the bug, not the safeguard.

    `caller_known` is resolved from caller ID before the first word, so any
    question re-establishing identity is a question we already had the answer
    to -- and it lands hardest on the returning candidate it inconveniences.
    """
    for phrase in _INTERROGATIONS:
        _must(
            phrase not in INBOUND_INSTRUCTIONS,
            f"inbound prompt still asks the caller to establish identity ({phrase!r}); "
            "caller_known already says who this is",
        )
    _must(
        "do not ask whether they have spoken to foray before" in INBOUND,
        "inbound prompt must explicitly forbid asking whether they have called before",
    )


def check_inbound_branches_on_caller_known() -> None:
    _must("`caller_known`" in INBOUND, "inbound prompt must branch on caller_known")
    _must(
        "if `caller_known` is true" in INBOUND and "if `caller_known` is false" in INBOUND,
        "inbound prompt must spell out both branches",
    )
    _must(
        "{{caller_first_name}}" in INBOUND_PROMPT,
        "inbound prompt must greet a known caller by the name we already have",
    )


def check_inbound_can_read_the_record_back() -> None:
    """The other half of the same bug: refusing a candidate their own answers."""
    _must(
        "caller_on_file" in INBOUND,
        "inbound prompt must be given the caller's record to work from",
    )
    _must(
        "tell them anything in `caller_on_file`, without hedging" in INBOUND,
        "inbound prompt must permit reading the record back to a known caller",
    )
    _must(
        "being evasive with somebody about their own disclosure" in INBOUND,
        "inbound prompt must name the evasion as the failure, or it will drift back to it",
    )


def check_nothing_in_the_record_is_held_back_from_its_owner() -> None:
    """A recognised caller is entitled to everything they themselves said.

    This is the check that would have caught the previous version, which handed
    the record over but carved out `confidential_items` as "theirs to raise".
    That carve-out is about not putting those items in front of a *client*; the
    person on the phone is the one who dictated them, and hedging with them was
    the same refusal in a smaller shape.
    """
    _must(
        "there is no part of it you have to be careful about with them" in INBOUND,
        "inbound prompt must not carve any of the caller's own record out of the read-back",
    )
    for carve_out in ("theirs to raise", "asked us to keep quiet", "do not bring it up"):
        _must(
            carve_out not in INBOUND,
            f"inbound prompt still fences off part of the caller's own record ({carve_out!r})",
        )


def check_the_identify_tool_is_gone() -> None:
    """Caller ID answers this before the agent speaks, so the tool was removed.

    Pinned because a half-removal is worse than either state: a prompt still
    telling the agent to call `identify_caller` against a tool that no longer
    exists is an agent pausing mid-sentence for a 404.
    """
    for name in ("identify_caller", "/v1/intake/identify"):
        _must(
            name not in INBOUND_PROMPT and name not in OUTBOUND_PROMPT,
            f"prompt still references {name!r}, which no longer exists",
        )


def check_unknown_callers_still_get_a_clean_first_intake() -> None:
    """The branch that must not inherit any of the recognition language."""
    _must(
        "this is a first conversation" in INBOUND,
        "inbound prompt must treat an unknown caller as a first conversation",
    )
    _must(
        "do not imply you know them" in INBOUND,
        "the unknown branch must forbid implying a prior relationship",
    )
    _must(
        "there is no last time" in INBOUND,
        "the unknown branch must say why 'what's changed' is wrong there",
    )


def check_recording_consent_survives_both_branches() -> None:
    """Two-party consent is law, not tone, and the rewrite must not have lost it."""
    _must(
        "whichever branch you are on" in INBOUND,
        "the recording question must apply to both branches explicitly",
    )
    _must(
        "the call is recorded" in INBOUND,
        "inbound prompt must still disclose recording",
    )
    _must(
        "the call is recorded" in OUTBOUND,
        "outbound prompt must still disclose recording",
    )


def check_every_slot_has_a_default() -> None:
    """An unset Retell variable renders as literal braces in the prompt.

    So every `{{caller_*}}` the prompt interpolates needs an entry in
    `default_dynamic_variables`, or a webhook that times out becomes an agent
    saying "caller first name" out loud.
    """
    used = set(re.findall(r"\{\{(caller_[a-z_]+)\}\}", INBOUND_PROMPT))
    # Referenced by name in prose as well as interpolated; both need defaults,
    # because the prose ones are the branch conditions.
    used |= {name for name in DEFAULT_DYNAMIC_VARIABLES if name in INBOUND}
    missing = sorted(used - set(DEFAULT_DYNAMIC_VARIABLES))
    _must(
        not missing,
        f"inbound prompt uses {missing} with no entry in DEFAULT_DYNAMIC_VARIABLES",
    )
    _must(
        DEFAULT_DYNAMIC_VARIABLES.get("caller_known") == "false",
        "the default for caller_known must be false -- an unanswered webhook "
        "must degrade to a first intake, never to a claimed relationship",
    )


def check_the_pitch_reads_only_composed_lines() -> None:
    """The wrap-up pitch must stay a read-back, never an improvisation.

    ``preview_matches`` hands the agent finished sentences precisely so a
    company name, a pay figure, or a count of roles can never be invented on a
    recorded call. The prompt has to keep saying so, in both agents, or the
    model will helpfully embellish -- and it must keep framing the lines as a
    match with what the candidate ASKED FOR, because a fit judgement of the
    candidate is the thing the intake channel must never emit.
    """
    for name, prompt in (("inbound", INBOUND), ("outbound", OUTBOUND)):
        _must(
            "preview_matches" in prompt,
            f"{name} prompt no longer calls preview_matches at wrap-up",
        )
        _must(
            "exactly as written" in prompt,
            f"{name} prompt must require the pitch lines be read as written",
        )
        _must(
            "never as a judgement of the candidate" in prompt
            or "never how good the candidate is" in prompt
            or "never as a judgement" in prompt,
            f"{name} prompt must frame the pitch as preference fit, not a verdict "
            "on the candidate",
        )
        _must(
            "close the way `check_coverage`'s `guidance` told you to" in prompt,
            f"{name} prompt must keep the no-lines fallback: close normally and "
            "mention no roles",
        )


def check_the_live_text_verification_flow_is_pinned() -> None:
    """Cards must wait for a genuine reply from the number we just texted."""
    for name, prompt in (("inbound", INBOUND), ("outbound", OUTBOUND)):
        _must(
            "start_text_verification" in prompt,
            f"{name} prompt lost the live text-verification tool",
        )
        _must(
            'reply "hey" or "hello"' in prompt,
            f"{name} prompt must ask for the verification reply",
        )
        _must(
            "only an explicit, unambiguous yes counts" in prompt,
            f"{name} prompt must not allow a vague answer to trigger a text",
        )
        _must(
            "never say the detailed role list was texted yet" in prompt,
            f"{name} prompt must hold cards until the reply arrives",
        )
        _must(
            "`preview_matches` again" in prompt,
            f"{name} prompt must support refining an unwanted first preview",
        )


def check_outbound_is_unchanged_in_posture() -> None:
    """The outbound call is the other case, and must not be over-corrected.

    We dialled them because they filled in our form minutes ago, so referring to
    that is accurate. The guard exists to stop the inbound work leaking here and
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
        check_inbound_does_not_ask_who_it_is_talking_to,
        check_inbound_branches_on_caller_known,
        check_inbound_can_read_the_record_back,
        check_nothing_in_the_record_is_held_back_from_its_owner,
        check_the_identify_tool_is_gone,
        check_unknown_callers_still_get_a_clean_first_intake,
        check_recording_consent_survives_both_branches,
        check_every_slot_has_a_default,
        check_the_pitch_reads_only_composed_lines,
        check_the_live_text_verification_flow_is_pinned,
        check_outbound_is_unchanged_in_posture,
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
