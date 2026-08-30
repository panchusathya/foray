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
    # The branch condition has to be *interpolated*, not merely named. Retell
    # substitutes `{{var}}` and nothing else, so prose like "the answer is in
    # `caller_known`" hands the model the field name and never the value -- and
    # a model told to branch on something it cannot see picks one. This check
    # exists because that shipped: for weeks the rendered prompt carried a
    # single slot, `{{caller_first_name}}`, while eight variables were being
    # supplied. Every caller got guessed at, and the guess came out "known",
    # so strangers were greeted as regulars and read a record invented to fit.
    _must(
        "{{caller_known}}" in INBOUND_PROMPT,
        "inbound prompt must interpolate {{caller_known}}, not just name it -- "
        "a branch condition the model cannot read is a branch it guesses",
    )


def check_every_supplied_variable_reaches_the_model() -> None:
    """Anything we send must appear as a slot, or we never actually sent it.

    The webhook resolves eight caller variables while the phone is ringing and
    hands them to Retell. A variable the prompt does not interpolate is work
    done for nobody: it costs a database read at ring time and arrives nowhere.
    Worse, the prompt goes on to instruct against it by name -- "tell them
    anything in `caller_on_file`" -- which reads as an instruction to produce
    the contents of something invisible.

    `caller_greeting` is the one exception: it is the whole of `begin_message`
    and deliberately never appears in the prompt body, because the agent speaks
    it before the first turn rather than reading it mid-call.

    Scanned across **both** prompts, and across every slot name rather than the
    `caller_*` ones. `default_dynamic_variables` is set on the agent, and
    `agent_body` sets it on both agents, so it is not an inbound-only bag --
    `recruiter_intro` is supplied here and consumed by the outbound prompt.
    Reading only the inbound prompt would call that orphaned, which is the
    check being narrower than the thing it is checking.
    """
    supplied = set(DEFAULT_DYNAMIC_VARIABLES) - {"caller_greeting"}
    interpolated = set(
        re.findall(r"\{\{([a-z_]+)\}\}", INBOUND_PROMPT)
        + re.findall(r"\{\{([a-z_]+)\}\}", OUTBOUND_PROMPT)
    )
    orphaned = sorted(supplied - interpolated)
    _must(
        not orphaned,
        f"the webhook supplies {orphaned} but neither prompt interpolates "
        "them -- the model cannot see a variable it is only told the name of",
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
    # A returning caller gets a shorter, warmer version of the ask -- "quick
    # reminder ... all good?" instead of a formality. Shorter is fine; dropping
    # the question is not. Consent is per call because California and a dozen
    # other states are two-party, so a reminder the caller never answers leaves
    # `consent_recording` false and the recording unconsented. This pins the
    # question mark, which is the part that is tempting to lose while making
    # the line sound natural.
    _must(
        "all good?" in INBOUND,
        "the returning-caller recording reminder must still end in a question",
    )
    _must(
        "consent is per call, not per person" in INBOUND,
        "the inbound prompt must say why the reminder is still asked, or the "
        "next edit that shortens it will drop the ask entirely",
    )


def check_every_slot_has_a_default() -> None:
    """An unset Retell variable renders as literal braces in the prompt.

    So every `{{caller_*}}` the prompt interpolates needs an entry in
    `default_dynamic_variables`, or a webhook that times out becomes an agent
    saying "caller first name" out loud.
    """
    used = set(re.findall(r"\{\{(caller_[a-z_]+)\}\}", INBOUND_PROMPT))
    # Prose references count too, so a variable discussed by name but not yet
    # interpolated still needs a default. It should not stay that way --
    # `check_every_supplied_variable_reaches_the_model` is the check that says
    # so -- but the default is what keeps an unanswered webhook from putting
    # literal braces in the agent's mouth either way.
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


def _flat(text: str) -> str:
    """The prompt with its hard wrapping collapsed.

    These are wrapped paragraphs, so a pinned sentence can be split across two
    lines by a reflow that changed nothing about its meaning. Matching on the
    flattened text stops a cosmetic rewrap from reading as a lost rule.

    Lowercased for the same reason: the prompt is emitted in the house
    lowercase voice, so a rule pinned in the casing it was written in fails
    against the casing it is actually sent in.
    """
    return " ".join(text.split()).lower()


def check_no_role_is_ever_described() -> None:
    """The wrap-up pitch is gone, and the prompt must close the hole it left.

    Removing ``preview_matches`` removed the only sanctioned source of a company
    name or a pay figure -- but a model asked "so what do you have?" by a real
    person will answer, and with the tool gone every word of that answer is
    invented. So absence of the tool is not enough on its own: the prompt has to
    say, in the imperative, that no opening may be described and that the honest
    answer is a recruiter follow-up.
    """
    for name, prompt in (("inbound", INBOUND), ("outbound", OUTBOUND)):
        _must(
            "preview_matches" not in prompt,
            f"{name} prompt still calls preview_matches; the pitch was removed",
        )
        _must(
            "never describe a role, a company, or a pay figure." in _flat(prompt),
            f"{name} prompt must forbid describing an opening",
        )
        _must(
            "a recruiter will come back to them with roles that fit" in _flat(prompt),
            f"{name} prompt must give the honest answer to \"what do you have?\"",
        )
        _must(
            "never describe an opening" in _flat(prompt),
            f"{name} prompt must keep the hard rule against describing openings",
        )


def check_the_live_text_verification_flow_is_pinned() -> None:
    """Cards must wait for a genuine reply from the number we just texted."""
    for name, prompt in (("inbound", INBOUND), ("outbound", OUTBOUND)):
        _must(
            "start_text_verification" in prompt,
            f"{name} prompt lost the live text-verification tool",
        )
        # YES rather than the friendlier "hey"/"hello" this used to pin. The
        # mechanism still takes any non-STOP reply, but the word we ASK for has
        # to be one the 10DLC campaign declares as an opt-in keyword -- a
        # confirmation prompt naming a word on no keyword list is what carrier
        # vetting reads as an unverifiable call to action.
        _must(
            "reply yes to the actual text" in _flat(prompt),
            f"{name} prompt must ask for the verification reply",
        )
        # The phrase, not one exact sentence. The consent ask carries the
        # longer TCPA wording -- "an explicit, unambiguous yes is a yes;
        # hesitation, 'I guess', silence, or 'email is better' is a no" -- and
        # pinning a single sentence made restoring that wording look like a
        # regression when it was the opposite.
        _must(
            "explicit, unambiguous yes" in _flat(prompt),
            f"{name} prompt must not allow a vague answer to trigger a text",
        )
        _must(
            '"email is better"' in prompt and "re-ask or reframe" in _flat(prompt),
            f"{name} prompt must say what is NOT consent and forbid re-asking "
            "until a yes appears",
        )
        _must(
            "never say that anything else has been texted yet" in _flat(prompt),
            f"{name} prompt must not claim anything was already sent",
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
        or "{{recruiter_name}}" in OUTBOUND_PROMPT
        or "{{recruiter_intro}}" in OUTBOUND_PROMPT,
        "outbound prompt must keep its dynamic variables",
    )


def check_the_persona_is_introduced_once() -> None:
    """The agent is called Foray and works for Foray. Say it once.

    The rename made every "<name> at Foray" / "<name> with Foray" construction
    a stutter, and a caller hears the worst of them -- "I'm Foray, an AI
    recruiting assistant with Foray" -- as the very first sentence of the call.

    The fix is that the located name arrives pre-composed in
    `{{recruiter_intro}}`, from `messaging.prompts.locate`. So what this pins is
    that the prompt does not go back to writing the firm on after the slot,
    which is the shape that broke: the slot already carries it when it should.
    """
    for stutter in (
        "{{recruiter_intro}}, an ai recruiting assistant with foray",
        "{{recruiter_intro}} at foray",
        "{{recruiter_intro}} from foray",
        "{{recruiter_name}}, an ai recruiting assistant with foray",
        "{{recruiter_name}} at foray",
    ):
        _must(
            stutter not in OUTBOUND,
            f"outbound prompt names the firm after the persona slot ({stutter!r}); "
            "`recruiter_intro` already carries it, so this says Foray twice",
        )
    _must(
        "{{recruiter_intro}}" in OUTBOUND_PROMPT,
        "outbound prompt must introduce from the located slot, not a bare name",
    )


def main() -> int:
    checks = (
        check_inbound_does_not_ask_who_it_is_talking_to,
        check_inbound_branches_on_caller_known,
        check_every_supplied_variable_reaches_the_model,
        check_inbound_can_read_the_record_back,
        check_nothing_in_the_record_is_held_back_from_its_owner,
        check_the_identify_tool_is_gone,
        check_unknown_callers_still_get_a_clean_first_intake,
        check_recording_consent_survives_both_branches,
        check_every_slot_has_a_default,
        check_no_role_is_ever_described,
        check_the_live_text_verification_flow_is_pinned,
        check_outbound_is_unchanged_in_posture,
        check_the_persona_is_introduced_once,
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
