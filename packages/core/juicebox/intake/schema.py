"""What an intake call must collect, and when it is allowed to end.

Two tiers. ``required`` is the gate: a hiring company asks these about every
candidate, so a call that has not covered them has not done its job and must
keep going. ``enrichment`` is everything that makes a match better but that no
client will block on — asked only once the gate is closed, which is what lets a
cooperative call finish in two or three minutes instead of ten.

**The gate is computed from the record, never judged by the model.** An agent
asking itself "have I been talking long enough?" guesses; ``coverage()`` answers
from the fields actually filled. The agent gets it as a tool result mid-call, so
"wrap up" is a fact about the data rather than a feeling about the clock.

Every field carries the ``client_question`` it answers. That is not
documentation — it is the admission price. Intake exists to answer what a
company will ask, so a field no client would ask about is a field that makes
every call longer for nothing. Adding one here without a client question is the
mistake this structure exists to prevent.

Declining is an answer. A candidate who will not discuss comp has *resolved*
that field; if refusal did not close the gate, the agent would ask forever and
the call could never end. ``declined`` entries therefore count as covered and
are reported separately, so the record shows an empty field was asked for rather
than skipped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TIER_REQUIRED = "required"
TIER_ENRICHMENT = "enrichment"

# Bumped when the field set changes, so a stored record can be told apart from
# one captured under a different definition of "complete". Mirrors the
# spec_version convention on JobPosting.
SCHEMA_VERSION = "intake-v1"


@dataclass(frozen=True)
class IntakeField:
    """One thing the agent is trying to learn.

    ``paths`` is a tuple because some questions have more than one acceptable
    answer shape: a comp expectation is covered by a base number *or* a total,
    and demanding both would stall the gate on a candidate who only thinks in
    one of them.
    """

    key: str
    tier: str
    paths: tuple[str, ...]
    asks: str
    client_question: str

    def satisfied_by(self, record: Mapping[str, Any]) -> bool:
        return any(is_filled(read_path(record, path)) for path in self.paths)


# Ordered. `next_questions` hands these back in sequence, so this is also the
# default running order of the conversation: identity-shaping facts first,
# because a wrong answer there makes every later answer worth less.
FIELDS: tuple[IntakeField, ...] = (
    # --- the gate ---------------------------------------------------------
    IntakeField(
        key="spec.job_titles",
        tier=TIER_REQUIRED,
        paths=("spec.job_titles",),
        asks="Which roles they are actually going after.",
        client_question="Is this person applying for the job I have open?",
    ),
    IntakeField(
        key="spec.seniority",
        tier=TIER_REQUIRED,
        paths=("spec.seniority",),
        asks="What level they expect to come in at, IC or management.",
        client_question="Am I looking at a senior IC or someone who wants my manager slot?",
    ),
    IntakeField(
        key="spec.locations",
        tier=TIER_REQUIRED,
        paths=("spec.locations",),
        asks="Where they are and which places they would work in.",
        client_question="Can they work where this role sits?",
    ),
    IntakeField(
        key="spec.remote_ok",
        tier=TIER_REQUIRED,
        paths=("spec.remote_ok",),
        asks="Whether they will go onsite, hybrid, or remote only.",
        client_question="Will they come into our office, or is this remote-only?",
    ),
    IntakeField(
        key="spec.must_have_skills",
        tier=TIER_REQUIRED,
        paths=("spec.must_have_skills",),
        asks="The skills they want to be hired for and keep using.",
        client_question="Can they do the actual work?",
    ),
    IntakeField(
        key="employment_status",
        tier=TIER_REQUIRED,
        paths=("employment_status",),
        asks="Employed and browsing, employed and serious, or actively searching.",
        client_question="How available are they, and how hard will they be to close?",
    ),
    IntakeField(
        key="timeline",
        tier=TIER_REQUIRED,
        paths=("timeline",),
        asks="How soon they want to move.",
        client_question="Could they start inside my hiring window?",
    ),
    IntakeField(
        key="compensation",
        tier=TIER_REQUIRED,
        paths=("target_base", "target_total"),
        asks="Base or total compensation they are targeting.",
        client_question="What will it cost me to hire them, and is that in my band?",
    ),
    IntakeField(
        key="work_authorization",
        tier=TIER_REQUIRED,
        paths=("work_authorization",),
        asks="Their right to work where the role is, and whether sponsorship is needed.",
        client_question="Can I legally hire them without sponsoring a visa?",
    ),
    IntakeField(
        key="active_processes",
        tier=TIER_REQUIRED,
        paths=("active_processes",),
        asks="Other processes they are in and how far along. An explicit none counts.",
        client_question="Am I competing with anyone, and how fast do I have to move?",
    ),
    IntakeField(
        key="ok_to_share_with_clients",
        tier=TIER_REQUIRED,
        paths=("ok_to_share_with_clients",),
        asks="Permission to put them in front of companies at all.",
        client_question="(Ours, not theirs: may we represent this person?)",
    ),
    # --- enrichment -------------------------------------------------------
    IntakeField(
        key="highlights",
        tier=TIER_ENRICHMENT,
        paths=("highlights",),
        asks="Projects or work they are proudest of, in their own words.",
        client_question="Why should I spend an hour interviewing them?",
    ),
    IntakeField(
        key="spec.company_stages",
        tier=TIER_ENRICHMENT,
        paths=("spec.company_stages",),
        asks="Funding stages they would and would not join.",
        client_question="Would they actually join a company at our stage?",
    ),
    IntakeField(
        key="spec.company_sizes",
        tier=TIER_ENRICHMENT,
        paths=("spec.company_sizes",),
        asks="Headcount range they want.",
        client_question="Will they be happy at our size, or leave in a year?",
    ),
    IntakeField(
        key="spec.industries",
        tier=TIER_ENRICHMENT,
        paths=("spec.industries",),
        asks="Industries they want to work in.",
        client_question="Do they care about our space?",
    ),
    IntakeField(
        key="spec.nice_to_have_skills",
        tier=TIER_ENRICHMENT,
        paths=("spec.nice_to_have_skills",),
        asks="Skills they have but do not insist on using.",
        client_question="What else could they cover for us?",
    ),
    IntakeField(
        key="reason_for_looking",
        tier=TIER_ENRICHMENT,
        paths=("reason_for_looking",),
        asks="Why they are leaving, in their words.",
        client_question="Why are they leaving, and will the same thing happen here?",
    ),
    IntakeField(
        key="spec.excluded_companies",
        tier=TIER_ENRICHMENT,
        paths=("spec.excluded_companies",),
        asks="Companies they will not consider, and non-competes or conflicts.",
        client_question="Is there anything that rules us out before we start?",
    ),
    IntakeField(
        key="spec.target_companies",
        tier=TIER_ENRICHMENT,
        paths=("spec.target_companies",),
        asks="Companies they are already excited about.",
        client_question="Are we on their list, or a consolation prize?",
    ),
    IntakeField(
        key="spec.freeform_criteria",
        tier=TIER_ENRICHMENT,
        paths=("spec.freeform_criteria",),
        asks="Dealbreakers no filter can express, kept verbatim.",
        client_question="What would make them turn down an offer?",
    ),
    IntakeField(
        key="offers_in_hand",
        tier=TIER_ENRICHMENT,
        paths=("offers_in_hand",),
        asks="Live offers and their deadlines.",
        client_question="Is there a clock I am racing?",
    ),
    IntakeField(
        key="willing_to_relocate",
        tier=TIER_ENRICHMENT,
        paths=("willing_to_relocate",),
        asks="Whether they would move, and on what terms.",
        client_question="Would they relocate for the right role?",
    ),
    IntakeField(
        key="equity_importance",
        tier=TIER_ENRICHMENT,
        paths=("equity_importance",),
        asks="How much they weigh equity against cash.",
        client_question="Can I close them with equity instead of base?",
    ),
    IntakeField(
        key="urgency_driver",
        tier=TIER_ENRICHMENT,
        paths=("urgency_driver",),
        asks="What is making them move now.",
        client_question="Why now? Is this real or idle browsing?",
    ),
    IntakeField(
        key="confidential_items",
        tier=TIER_ENRICHMENT,
        paths=("confidential_items",),
        asks="Anything they do not want repeated to a company.",
        client_question="(Ours: what must never appear in a writeup?)",
    ),
)

FIELDS_BY_KEY: dict[str, IntakeField] = {f.key: f for f in FIELDS}
REQUIRED_KEYS: tuple[str, ...] = tuple(f.key for f in FIELDS if f.tier == TIER_REQUIRED)
ENRICHMENT_KEYS: tuple[str, ...] = tuple(f.key for f in FIELDS if f.tier == TIER_ENRICHMENT)

# The ParsedQuery fields intake is allowed to populate. The rest of
# ParsedQuery (schools, degrees, fields_of_study, min/max years, keywords,
# title_exclusions, search_text) describes a *company's* filter and has no
# candidate-stated equivalent — a candidate does not have an opinion about
# which school they attended, and inventing one would put noise into scoring.
SPEC_FIELDS: tuple[str, ...] = (
    "job_titles",
    "must_have_skills",
    "nice_to_have_skills",
    "seniority",
    "locations",
    "remote_ok",
    "industries",
    "company_stages",
    "company_sizes",
    "target_companies",
    "excluded_companies",
    "negative_keywords",
    "freeform_criteria",
)

EMPLOYMENT_STATUSES = (
    "employed_not_looking",
    "employed_open",
    "employed_searching",
    "unemployed_searching",
    "student",
)

TIMELINES = ("immediate", "under_1_month", "1_3_months", "3_6_months", "exploratory")

EQUITY_IMPORTANCE = ("irrelevant", "nice_to_have", "important", "critical")


def read_path(record: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted path, tolerating a missing ``spec`` sub-dict."""
    cursor: Any = record
    for part in path.split("."):
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(part)
    return cursor


def is_filled(value: Any) -> bool:
    """Has this been answered?

    ``False`` is an answer — "no, I will not go onsite" is exactly as
    informative as yes, and treating it as absent is the bug that makes
    ``Candidate.open_to_remote`` unable to distinguish "onsite only" from
    "never asked". Empty string, empty list and ``None`` are unanswered.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


@dataclass(frozen=True)
class Coverage:
    """What the call still owes us."""

    required_missing: tuple[str, ...]
    enrichment_missing: tuple[str, ...]
    declined: tuple[str, ...]
    covered: tuple[str, ...]

    @property
    def required_complete(self) -> bool:
        """True when the agent has permission to start wrapping up."""
        return not self.required_missing

    @property
    def completion_ratio(self) -> float:
        total = len(FIELDS)
        return len(self.covered) / total if total else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_complete": self.required_complete,
            "required_missing": list(self.required_missing),
            "enrichment_missing": list(self.enrichment_missing),
            "declined": list(self.declined),
            "covered": list(self.covered),
            "completion_ratio": round(self.completion_ratio, 3),
        }


def coverage(record: Mapping[str, Any]) -> Coverage:
    """Which fields are answered, refused, or still owed.

    A key in ``record["declined"]`` counts as covered: the candidate was asked
    and said no, which is a resolved field and must not keep the call alive.
    """
    declined = {
        key for key in (record.get("declined") or []) if isinstance(key, str) and key in FIELDS_BY_KEY
    }
    required_missing: list[str] = []
    enrichment_missing: list[str] = []
    covered: list[str] = []

    for spec in FIELDS:
        if spec.key in declined or spec.satisfied_by(record):
            covered.append(spec.key)
        elif spec.tier == TIER_REQUIRED:
            required_missing.append(spec.key)
        else:
            enrichment_missing.append(spec.key)

    return Coverage(
        required_missing=tuple(required_missing),
        enrichment_missing=tuple(enrichment_missing),
        declined=tuple(sorted(declined)),
        covered=tuple(covered),
    )


def next_questions(record: Mapping[str, Any], limit: int = 3) -> list[dict[str, str]]:
    """What to ask next, gate first.

    Returned to the agent mid-call, so an empty list for the required tier is
    the signal to start closing rather than to invent another question.
    """
    state = coverage(record)
    ordered = [*state.required_missing, *state.enrichment_missing]
    out: list[dict[str, str]] = []
    for key in ordered[: max(0, limit)]:
        spec = FIELDS_BY_KEY[key]
        out.append(
            {
                "field": spec.key,
                "tier": spec.tier,
                "asks": spec.asks,
                "because": spec.client_question,
            }
        )
    return out


def empty_record() -> dict[str, Any]:
    """A record with every key present and nothing answered.

    Present-but-empty rather than absent: a consumer should never have to guess
    whether a missing key means unasked or unsupported.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "spec": {name: _spec_default(name) for name in SPEC_FIELDS},
        "employment_status": "",
        "reason_for_looking": "",
        "timeline": "",
        "urgency_driver": "",
        "target_base": None,
        "target_total": None,
        "comp_currency": "",
        "comp_flexibility_notes": "",
        "equity_importance": "",
        "work_authorization": "",
        "willing_to_relocate": None,
        "active_processes": [],
        "offers_in_hand": [],
        "highlights": [],
        "confidential_items": [],
        "ok_to_share_with_clients": None,
        "declined": [],
    }


def _spec_default(name: str) -> Any:
    return None if name == "remote_ok" else []
