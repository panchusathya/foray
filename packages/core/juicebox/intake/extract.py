"""Turn an intake transcript into candidate preferences.

Two paths, same output shape. The model reads the conversation properly; the
heuristic exists so the stack still works with no ``ANTHROPIC_API_KEY``, like
every other model-backed step here.

The posture is copied from ``jobmatch.spec``, for the same reason: **an invented
preference is the expensive failure.** On the job side a hallucinated
requirement drops good candidates from a shortlist. On this side it is worse —
a preference the candidate never stated silently rules out roles they would have
taken, and nobody ever sees the roles that were filtered away. So both paths
would rather leave a field empty than fill it plausibly, and the heuristic path
declines several fields outright:

* ``must_have_skills`` stays empty. Skills mentioned in passing become
  ``nice_to_have_skills``; a regex cannot tell "I want to keep writing Rust"
  from "I did some Rust once", and the must-have list is what
  ``search.scoring.assess`` penalises hard misses against.
* ``ok_to_share_with_clients`` stays ``None``. Consent is never inferred from
  pattern matching — the one field where a wrong guess is a permission we did
  not have.
* ``highlights`` stays empty. Its value is the candidate's verbatim phrasing,
  which needs judgement about where the quote starts and stops.

Every heuristic extraction is flagged for review. A regex reading of a
conversation is a starting point for a human, not something to feed into
matching unseen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from ..enrich import taxonomy as tx
from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import (
    INTAKE_EXTRACT_SCHEMA,
    INTAKE_EXTRACT_SYSTEM,
    intake_extract_prompt,
)
from ..search.parser import ParsedQuery
from .contact import find_email, find_name, scan_transcript
from .schema import (
    EMPLOYMENT_STATUSES,
    EQUITY_IMPORTANCE,
    FIELDS_BY_KEY,
    SCHEMA_VERSION,
    SPEC_FIELDS,
    TIMELINES,
    Coverage,
    coverage,
    empty_record,
)

logger = logging.getLogger(__name__)

EXTRACTOR_LLM = "llm"
EXTRACTOR_HEURISTIC = "heuristic"
# The heuristic path reached because the model call *failed*, not because no
# model is configured. Both produce identical records, and conflating them is
# why nobody could answer "how often is the coverage gate running on regexes?"
# -- a deployment with no API key and a deployment whose extractor times out on
# every call looked the same from the outside. Anything that counts, reviews or
# alerts should treat this as a fault; `EXTRACTOR_HEURISTIC` is a choice.
EXTRACTOR_HEURISTIC_DEGRADED = "heuristic-degraded"

# Mirrors the cap in the prompt and in jobmatch.spec: a candidate with eleven
# non-negotiables has none.
MAX_MUST_HAVES = 5


@dataclass(frozen=True)
class ExtractionResult:
    record: dict[str, Any]
    extractor: str
    low_confidence: tuple[str, ...] = ()
    review_needed: bool = False
    review_reason: str = ""
    coverage: Coverage = field(default_factory=lambda: coverage(empty_record()))
    # Who said all this. Beside the record rather than inside it, for the reason
    # ``intake.contact`` gives: the record is normalised through ``ParsedQuery``
    # and a name has no counterpart on the job side. They never reach
    # ``coverage()`` either -- a caller who will not give a name has still
    # completed an intake, and gating the wrap-up on identity would keep a
    # finished call alive over a field the matcher does not read.
    contact_name: str = ""
    contact_email: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record,
            "extractor": self.extractor,
            "low_confidence": list(self.low_confidence),
            "review_needed": self.review_needed,
            "review_reason": self.review_reason,
            "coverage": self.coverage.to_dict(),
            "contact_name": self.contact_name,
            "contact_email": self.contact_email,
        }


def extract_intake(
    transcript: str,
    *,
    known: str = "",
    use_llm: bool | None = None,
    fast: bool = False,
    live_call: bool = False,
) -> ExtractionResult:
    """Extract preferences from ``transcript``, degrading to heuristics.

    ``use_llm=False`` forces the heuristic path, which is how the tests exercise
    it without unsetting the key.

    ``fast=True`` is for the mid-call coverage check, where a candidate is
    sitting in silence waiting for the agent's next question. It routes to
    ``json_fast`` — the fast model, a short timeout, no retries — because a
    slower and more careful reading of a half-finished conversation is the wrong
    trade: the gate only has to decide what to ask next, and it will be asked
    again on the next turn.
    """
    llm = get_llm()
    want_llm = llm.available if use_llm is None else use_llm

    if want_llm and llm.available:
        try:
            # A caller in silence gets the stricter budget; a text thread does
            # not. Resolved here rather than passed in, so callers say what kind
            # of wait it is and this module owns what that costs.
            budget = get_llm().voice_timeout if (fast and live_call) else None
            return _llm_extract(transcript, known=known, fast=fast, timeout=budget)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail an extraction
            logger.warning("llm intake extraction failed, using heuristics: %s", exc)
            degraded = _heuristic_extract(transcript)
            # Same record, different provenance. The caller decides what to do
            # about it; what matters here is that it is no longer silent.
            return replace(
                degraded,
                extractor=EXTRACTOR_HEURISTIC_DEGRADED,
                review_needed=True,
                review_reason=_join_reason(
                    degraded.review_reason,
                    "extractor unavailable, record read by heuristics",
                ),
            )

    return _heuristic_extract(transcript)


def _join_reason(existing: str, addition: str) -> str:
    existing = (existing or "").strip()
    return f"{existing}; {addition}" if existing else addition


# --------------------------------------------------------------------------
# model path
# --------------------------------------------------------------------------


# URLs and email addresses carry digits that are not money. A LinkedIn URL
# in the transcript must not count as the candidate having named a figure.
_NOT_MONEY = re.compile(r"https?://\S+|\S+@\S+", re.IGNORECASE)
_ANY_DIGIT = re.compile(r"\d")


def mentions_a_figure(transcript: str) -> bool:
    """Did anyone actually say a number, once links and addresses are out?"""
    return bool(_ANY_DIGIT.search(_NOT_MONEY.sub(" ", transcript or "")))


def _drop_uncited_comp(record: dict[str, Any], transcript: str) -> None:
    """Comp has to come from the conversation, not from the model.

    A live thread whose entire inbound content was a greeting, an email
    address, a LinkedIn URL and "strategic finance analyst roles" came out
    of extraction with target_base = 1,200,000. Nothing said it. That
    figure then vetoed every real posting on comp_floor, so the candidate
    was shown no roles at all and there was nothing on screen to explain
    why -- an invented number is worse than a missing one precisely because
    it acts like a real answer.

    The test is deliberately coarse: any digit anywhere, links and email
    addresses removed first. It cannot check that 150000 came from "150k"
    rather than "$1.5M", but it does stop a figure appearing out of a
    conversation that contains no numbers whatsoever. Spelled-out amounts
    ("two hundred thousand") are dropped too; losing a rare real answer is
    cheaper than keeping an invented one that silently filters every job.
    """
    if mentions_a_figure(transcript):
        return
    invented = {
        key: record.get(key)
        for key in ("target_base", "target_total")
        if record.get(key) is not None
    }
    if not invented:
        return
    logger.warning(
        "dropping comp the transcript never mentions: %s", invented
    )
    record["target_base"] = None
    record["target_total"] = None


def _llm_extract(
    transcript: str, *, known: str, fast: bool = False, timeout: float | None = None
) -> ExtractionResult:
    llm = get_llm()
    kwargs: dict[str, Any] = {}
    if fast and timeout is not None:
        # Only `json_fast` takes a budget; the careful path runs in a worker
        # where nobody is waiting and the main timeout is the right one.
        kwargs["timeout"] = timeout
    call = llm.json_fast if fast else llm.json
    result = call(
        system=INTAKE_EXTRACT_SYSTEM,
        prompt=intake_extract_prompt(transcript=transcript, known=known),
        schema=INTAKE_EXTRACT_SCHEMA,
        effort="low",
        max_tokens=8000,
        **kwargs,
    )
    if not result.data:
        raise LLMUnavailable("intake extractor returned no data")

    record = normalize_record(result.data)
    _drop_uncited_comp(record, transcript)
    low = _valid_keys(result.data.get("low_confidence"))
    # The model's own bucket was used because the quote did not map to one, so
    # nothing deterministic vouches for this value. Say so rather than let a
    # required field look as solid as the ones we derived.
    if (
        record["timeline"]
        and not _bucket_timeline(record["timeline_verbatim"])
        and "timeline" not in low
    ):
        low = (*low, "timeline")
    state = coverage(record)
    review, reason = _review_verdict(
        extractor=EXTRACTOR_LLM, low_confidence=low, state=state, record=record
    )
    name, email = _contact(result.data, transcript)
    return ExtractionResult(
        record=record,
        extractor=EXTRACTOR_LLM,
        low_confidence=low,
        review_needed=review,
        review_reason=reason,
        coverage=state,
        contact_name=name,
        contact_email=email,
    )


def _contact(payload: dict[str, Any], transcript: str) -> tuple[str, str]:
    """The caller's own name and address: the model's reading, then the scan.

    The model is asked because it can see whose turn is whose, which is exactly
    what a regex over a transcript cannot. It is not *trusted* though -- both
    values go through the same predicates the text channel uses, so "unknown",
    a sentence, or an invented address is dropped rather than written onto a
    person. This is the same posture as ``ok_to_share_verbatim``: a model's
    assertion is evidence, not an answer.

    The scan is the fallback rather than the primary because a spoken address
    is where transcription is least reliable, and the model has the whole turn
    to work from. Where both produce something the model wins; where the model
    is silent or unusable the scan still catches the common case.
    """
    payload = payload if isinstance(payload, dict) else {}

    stated_email = _text(payload.get("contact_email")).lower()
    email = find_email(stated_email)

    stated_name = _no_placeholder(_text(payload.get("contact_name")))
    # Through ``find_name`` rather than stored as-is: on a live call the model
    # answered this with "the caller did not give a name", which is a sentence,
    # not a name, and would have gone straight onto a CRM row.
    name = find_name(stated_name)

    if name and email:
        return name[:240], email[:255]
    scanned_name, scanned_email = scan_transcript(transcript)
    return (name or scanned_name)[:240], (email or scanned_email)[:255]


def normalize_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce raw extractor output into a stored record.

    Defensive about types even though the call is schema-constrained: a scripted
    ``FakeTransport`` response, a replayed old payload and a future schema
    revision all arrive through here, and a wrong type must not reach the
    database as one.
    """
    record = empty_record()
    payload = payload if isinstance(payload, dict) else {}

    record["spec"] = _normalize_spec(payload.get("spec"))

    record["employment_status"] = _enum(payload.get("employment_status"), EMPLOYMENT_STATUSES)
    record["equity_importance"] = _enum(payload.get("equity_importance"), EQUITY_IMPORTANCE)

    # Bucketed here, not by the model. Measured over five runs each on "two
    # weeks if the right thing came up": GLM 5.2 answered 1_3_months three
    # times, 3_6_months once and immediate once; gpt-oss-120b was no steadier.
    # Sorting a duration phrase into five buckets is arithmetic, and asking a
    # cheap model to do it makes a required field non-deterministic. The model
    # quotes what was said; the mapping is code, and the same patterns the
    # heuristic path already uses.
    record["timeline_verbatim"] = _text(payload.get("timeline_verbatim"))
    record["timeline"] = _bucket_timeline(record["timeline_verbatim"]) or _enum(
        payload.get("timeline"), TIMELINES
    )

    record["reason_for_looking"] = _text(payload.get("reason_for_looking"))
    record["urgency_driver"] = _text(payload.get("urgency_driver"))
    record["comp_flexibility_notes"] = _text(payload.get("comp_flexibility_notes"))
    # A placeholder is not an answer. In a real call the agent never asked about
    # work authorisation and the model wrote "unknown", which `is_filled` counts
    # as present -- so the gate closed on a required question nobody had asked,
    # and a client-blocking one at that ("can I hire them without a visa?").
    record["work_authorization"] = _no_placeholder(
        _text(payload.get("work_authorization")).lower()
    )
    record["comp_currency"] = _text(payload.get("comp_currency")).upper()[:8]

    record["target_base"] = _money(payload.get("target_base"))
    record["target_total"] = _money(payload.get("target_total"))

    record["willing_to_relocate"] = _tribool(payload.get("willing_to_relocate"))

    # Quote or it did not happen. In a real call the agent never asked for
    # permission to share and the model returned `true` anyway -- the third time
    # a cheap model has filled a field it was never given evidence for, and the
    # costliest one to get wrong. Requiring the exchange to be quotable turns
    # "the model asserted it" into "the transcript contains it", which is the
    # only version of consent worth storing.
    record["ok_to_share_verbatim"] = _text(payload.get("ok_to_share_verbatim"))
    claimed = _tribool(payload.get("ok_to_share_with_clients"))
    record["ok_to_share_with_clients"] = (
        claimed if (claimed is False or record["ok_to_share_verbatim"]) else None
    )

    record["active_processes"] = _dicts(
        payload.get("active_processes"), ("company", "stage", "expected_decision")
    )
    record["offers_in_hand"] = _dicts(
        payload.get("offers_in_hand"), ("company", "comp_summary", "deadline")
    )
    record["highlights"] = _highlights(payload.get("highlights"))
    record["confidential_items"] = _strings(payload.get("confidential_items"))
    record["declined"] = list(_valid_keys(payload.get("declined")))

    # A figure with no currency is unusable for comparison, and USD is the
    # wrong thing to assume silently. Say what was assumed instead.
    if (record["target_base"] or record["target_total"]) and not record["comp_currency"]:
        record["comp_currency"] = "UNKNOWN"

    return record


def _normalize_spec(raw: Any) -> dict[str, Any]:
    """Push the preference spec through the same taxonomy a JD goes through.

    Routed via ``ParsedQuery`` rather than normalising field by field so the two
    sides cannot drift: whatever ``normalized()`` does to a job's ``industries``
    it does to a candidate's, which is the whole premise of scoring one against
    the other.
    """
    raw = raw if isinstance(raw, dict) else {}
    parsed = ParsedQuery.from_dict({name: raw.get(name) for name in SPEC_FIELDS}).normalized()
    parsed.must_have_skills = parsed.must_have_skills[:MAX_MUST_HAVES]
    full = parsed.to_dict()
    return {name: full[name] for name in SPEC_FIELDS}


# --------------------------------------------------------------------------
# heuristic path
# --------------------------------------------------------------------------

_TIMELINE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("immediate", r"\b(right away|immediately|as soon as possible|asap|start tomorrow)\b"),
    ("under_1_month", r"\b(two weeks|2 weeks|couple of weeks|few weeks|within a month|next month|this month)\b"),
    ("1_3_months", r"\b(next quarter|couple of months|two months|three months|few months|2-3 months)\b"),
    ("3_6_months", r"\b(six months|6 months|end of the year|later this year|next year)\b"),
    # Conditional and non-committal answers are exploratory, not urgent. "If I
    # find the right role I might start soon, but if not I'm okay to stay" came
    # from a real call, and the model bucketed it as under_1_month -- which puts
    # a browsing candidate in front of a client who is hiring now.
    (
        "exploratory",
        r"\b(just looking|no rush|not in a hurry|keeping an eye|exploring|casually looking"
        r"|testing the waters|it depends|okay to stay|ok to stay|happy where i am"
        r"|not sure yet|no timeline|if i find the right|for the right (role|thing|one))\b",
    ),
)

# Ordered: the first match wins, so the least ambiguous statements come first.
_EMPLOYMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("unemployed_searching", r"\b(laid off|made redundant|unemployed|between roles|out of work|lost my job)\b"),
    ("student", r"\b(final year|graduating|new grad|still at university|phd student)\b"),
    ("employed_not_looking", r"\b(happy where i am|not really looking|not looking to move)\b"),
    ("employed_searching", r"\b(actively looking|actively searching|actively interviewing|in the market|need to leave)\b"),
    ("employed_open", r"\b(open to hearing|open to the right|open to conversations|not actively looking|passively)\b"),
)

_AUTH_PATTERNS: tuple[tuple[str, str], ...] = (
    ("sponsorship required", r"\b(need sponsorship|require sponsorship|need a visa|visa sponsorship|would need sponsoring)\b"),
    ("h-1b transfer needed", r"\b(h1b|h-1b)\b"),
    ("opt", r"\b(opt|stem opt)\b"),
    ("green card", r"\b(green card|permanent resident)\b"),
    ("us citizen", r"\b(us citizen|u\.s\. citizen|american citizen)\b"),
    ("citizen", r"\b(i am a citizen|i'm a citizen)\b"),
    ("right to work", r"\b(right to work|no visa (issues|problems)|work permit)\b"),
)

_REMOTE_TRUE = r"\b(fully remote|remote only|remote-only|only remote|work from home permanently|remote first)\b"
_REMOTE_FALSE = r"\b(in the office|onsite|on-site|in-office|hybrid|come into the office)\b"

# "fully remote isn't what I want" contains a remote-positive phrase and means
# the opposite. Without this, the single most consequential preference on the
# call gets recorded backwards.
_NEGATION = re.compile(
    r"\b(not|isn'?t|aren'?t|no|never|don'?t|doesn'?t|won'?t|wouldn'?t|rather not|"
    r"as long as it'?s not|other than)\b"
)
# A negation belongs to its clause, not to a radius of characters. "Hybrid is
# fine, fully remote isn't what I want" carries an assertion and a denial eleven
# characters apart, and any window wide enough to catch "remote isn't what I
# want" also swallows "hybrid is fine" and loses it.
_CLAUSE_SPLIT = re.compile(r"[,;.!?\n]|\bbut\b|\bthough\b|\bhowever\b|\bwhereas\b")

_RELOCATE_FALSE = r"\b(not willing to relocate|won'?t relocate|can'?t relocate|cannot relocate|no relocation|not moving)\b"
_RELOCATE_TRUE = r"\b(willing to relocate|happy to relocate|would relocate|open to relocating|open to moving)\b"

_NO_PROCESSES = r"\b(not interviewing anywhere|no other processes|nothing else going on|you'?re the first|not talking to anyone else)\b"

# A money amount only counts inside a sentence that is about pay. "we raised
# $200 million" is in every startup conversation and is not a comp expectation.
_COMP_CUE = re.compile(
    r"\b(base|salary|salaries|comp|compensation|package|total|all[- ]in|paid|pay|earning|make|target)\b",
    re.I,
)
_MONEY = re.compile(
    r"(?:[$£€]\s?)?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k\b|thousand\b|m\b|million\b)?",
    re.I,
)
_SENTENCE = re.compile(r"[.!?\n]+")

# Role nouns that make a captured phrase plausibly a job title. Without this
# every "looking for a change" becomes a job title.
_ROLE_NOUNS = (
    "engineer", "developer", "manager", "designer", "scientist", "analyst",
    "architect", "researcher", "lead", "director", "recruiter", "marketer",
    "salesperson", "consultant", "product manager", "pm", "sre", "devops",
)
_TITLE_CAPTURES = (
    r"looking for (?:a|an)\s+([a-z0-9+#/.\- ]{3,60}?)\s+(?:role|position|job|gig)",
    r"(?:want|hoping|trying) to (?:be|become|move into)\s+(?:a|an)?\s*([a-z0-9+#/.\- ]{3,60}?)\b",
    r"\b(?:as an?|for an?)\s+([a-z0-9+#/.\- ]{3,60}?)\s+(?:role|position)\b",
    r"\b([a-z0-9+#/.\- ]{3,60}?)\s+roles?\b",
)


def _heuristic_extract(transcript: str) -> ExtractionResult:
    text = (transcript or "").lower()
    record = empty_record()
    low: list[str] = []

    record["timeline"] = _first_match(text, _TIMELINE_PATTERNS)
    record["employment_status"] = _first_match(text, _EMPLOYMENT_PATTERNS)
    record["work_authorization"] = _first_match(text, _AUTH_PATTERNS)
    if record["work_authorization"] == "h-1b transfer needed":
        low.append("work_authorization")  # an H-1B mention is not always a transfer ask

    wants_remote = _asserted(text, _REMOTE_TRUE)
    wants_office = _asserted(text, _REMOTE_FALSE)
    if wants_remote and not wants_office:
        record["spec"]["remote_ok"] = True
    elif wants_office and not wants_remote:
        # "hybrid" lands here too, which loses the distinction between hybrid
        # and strictly onsite. Flagged rather than modelled.
        record["spec"]["remote_ok"] = False
        low.append("spec.remote_ok")
    elif wants_remote and wants_office:
        # They talked about both. A regex cannot rank them, and guessing wrong
        # here mis-files the preference that rules out the most roles.
        low.append("spec.remote_ok")

    if re.search(_RELOCATE_FALSE, text):
        record["willing_to_relocate"] = False
    elif re.search(_RELOCATE_TRUE, text):
        record["willing_to_relocate"] = True

    if re.search(_NO_PROCESSES, text):
        record["active_processes"] = [
            {"company": "none", "stage": "none", "expected_decision": ""}
        ]

    base, total, comp_low = _heuristic_comp(text)
    record["target_base"] = base
    record["target_total"] = total
    if base or total:
        record["comp_currency"] = "UNKNOWN"
    if comp_low:
        low.append("compensation")

    titles = _heuristic_titles(text)
    if titles:
        record["spec"]["job_titles"] = titles
        low.append("spec.job_titles")

    seniority = _heuristic_seniority(text, titles)
    if seniority:
        record["spec"]["seniority"] = seniority
        low.append("spec.seniority")

    locations = _heuristic_locations(text)
    if locations:
        # A transcript mentions where someone lives, where they used to work and
        # where they would move, and a keyword scan cannot tell those apart.
        record["spec"]["locations"] = locations
        low.append("spec.locations")

    skills = tx.extract_skills_from_text(text)
    if skills:
        record["spec"]["nice_to_have_skills"] = skills

    state = coverage(record)
    review, reason = _review_verdict(
        extractor=EXTRACTOR_HEURISTIC,
        low_confidence=tuple(low),
        state=state,
        record=record,
    )
    # No model, so the only reading available is the anchored transcript scan.
    # It is allowed to fill these two even though the heuristic path is barred
    # from ``must_have_skills``, ``highlights`` and consent, and the difference
    # is the failure mode: a wrong must-have silently filters out real roles and
    # nobody ever sees what was dropped, whereas a wrong name or address is
    # visible on the record the first time a recruiter looks at it, and the scan
    # only reads the answer to a question we asked.
    contact_name, contact_email = scan_transcript(transcript)
    return ExtractionResult(
        record=record,
        extractor=EXTRACTOR_HEURISTIC,
        low_confidence=tuple(low),
        review_needed=review,
        review_reason=reason,
        coverage=state,
        contact_name=contact_name,
        contact_email=contact_email,
    )


def _bucket_timeline(verbatim: str) -> str:
    """Map a quoted timing phrase onto a bucket, deterministically.

    Shared with the heuristic path on purpose: one definition of what "two
    weeks" means, exercised by both, so the model path and the no-model path
    cannot disagree about a required field.
    """
    return _first_match((verbatim or "").lower(), _TIMELINE_PATTERNS)


def _first_match(text: str, patterns: tuple[tuple[str, str], ...]) -> str:
    for value, pattern in patterns:
        if re.search(pattern, text):
            return value
    return ""


def _asserted(text: str, pattern: str) -> bool:
    """Does ``pattern`` appear somewhere it is not being negated?

    A phrase inside a negation is evidence *against* itself, so a transcript
    where every occurrence is negated counts as not having said it. The negated
    reading is not flipped into a positive for the opposite field — "not fully
    remote" does not establish that they want an office five days a week.
    """
    for clause in _CLAUSE_SPLIT.split(text):
        if re.search(pattern, clause) and not _NEGATION.search(clause):
            return True
    return False


def _heuristic_comp(text: str) -> tuple[float | None, float | None, bool]:
    """Money from sentences that are about money. Returns (base, total, unsure)."""
    for sentence in _SENTENCE.split(text):
        if not _COMP_CUE.search(sentence):
            continue
        amount = _largest_amount(sentence)
        if amount is None:
            continue
        if re.search(r"\bbase\b", sentence):
            return amount, None, False
        if re.search(r"\b(total|all[- ]in|package|tc)\b", sentence):
            return None, amount, False
        # A bare number in a pay sentence is usually base, but "usually" is
        # exactly what the review flag is for.
        return amount, None, True
    return None, None, False


def _largest_amount(sentence: str) -> float | None:
    """Biggest plausible annual figure in the sentence.

    Largest rather than first because "I'm on 150 but looking for 180" states
    the target second, and a pay sentence rarely contains a bigger unrelated
    number.
    """
    best: float | None = None
    for raw, suffix in _MONEY.findall(sentence):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        unit = (suffix or "").lower()
        if unit.startswith("k") or unit.startswith("thousand"):
            value *= 1_000
        elif unit.startswith("m") or unit.startswith("million"):
            value *= 1_000_000
        elif value < 1000:
            # "looking for 180" means 180k. Below 1000 with no unit is either
            # shorthand or not a salary at all.
            value *= 1_000
        if not 20_000 <= value <= 10_000_000:
            continue
        if best is None or value > best:
            best = value
    return best


def _heuristic_titles(text: str) -> list[str]:
    found: list[str] = []
    for pattern in _TITLE_CAPTURES:
        for phrase in re.findall(pattern, text):
            if not any(noun in phrase for noun in _ROLE_NOUNS):
                continue
            title = tx.normalize_title(phrase)
            if title and title not in found:
                found.append(title)
    return found[:3]


def _heuristic_seniority(text: str, titles: list[str]) -> list[str]:
    """What they said, or failing that what their target title implies.

    Stated wins outright rather than merging with the inferred value.
    ``infer_seniority_from_title`` answers "mid" for any title carrying no
    marker, so merging turned "a staff backend engineer role" into
    ``["mid", "staff"]`` — two levels, one of which the candidate never said, on
    a field clients filter hard against.
    """
    stated = [word for word in tx.SENIORITY_ORDER if re.search(rf"\b{re.escape(word)}\b", text)]
    if stated:
        return stated[:3]

    inferred: list[str] = []
    for title in titles:
        level = tx.infer_seniority_from_title(title)
        if level and level != "unknown" and level not in inferred:
            inferred.append(level)
    return inferred[:3]


# Place names that are also ordinary English. "let us know" and "tell us" are
# in every transcript, so "us" costs far more in false positives than the
# occasional "I want to work in the US" is worth — especially since a candidate
# saying that has almost always named a city too.
_LOCATION_STOPWORDS = frozenset({"us", "u.s.", "usa"})


def _heuristic_locations(text: str) -> list[str]:
    slug = tx.slugify_text(text)
    names = set(tx.METRO_MEMBERS) | set(tx.LOCATION_ALIASES)
    for members in tx.METRO_MEMBERS.values():
        names |= members
    found: list[str] = []
    # Longest first so "san francisco" is preferred over a bare "california"
    # inside it, and the four-item cap keeps the more specific hit.
    for name in sorted(names, key=len, reverse=True):
        if len(name) < 2 or name in _LOCATION_STOPWORDS:
            continue
        if re.search(rf"\b{re.escape(name)}\b", slug):
            normalized = tx.normalize_location(name)
            if normalized and normalized not in found:
                found.append(normalized)
    return found[:4]


# --------------------------------------------------------------------------
# review routing
# --------------------------------------------------------------------------


def _review_verdict(
    *,
    extractor: str,
    low_confidence: tuple[str, ...],
    state: Coverage,
    record: dict[str, Any],
) -> tuple[bool, str]:
    reasons: list[str] = []
    if extractor == EXTRACTOR_HEURISTIC:
        reasons.append("heuristic extraction: no model was available")
    if low_confidence:
        reasons.append(f"low confidence on {', '.join(sorted(low_confidence))}")
    if state.required_missing:
        reasons.append(f"required fields missing: {', '.join(state.required_missing)}")
    if record.get("ok_to_share_with_clients") is not True:
        # Not a data-quality problem — a permission we do not have. Anything
        # that reaches a client must pass a human first.
        reasons.append("no explicit permission to share with clients")
    elif extractor == EXTRACTOR_LLM:
        # A *claimed* permission is not a permission. Asked for a transcript
        # where sharing never came up, GLM 5.2 asserted `true` three times in
        # five and gpt-oss-120b twice — so the one field authorising us to put
        # a person in front of employers is also the one a model will invent.
        # It stays on the record as evidence and it still closes the coverage
        # gate, because the *call* did its job; what it must not do is clear
        # review on its own. A human or the candidate's own review page
        # confirms it, and `confirmed_at` is what makes it real.
        reasons.append("sharing permission read from the transcript, not yet confirmed")
    return bool(reasons), "; ".join(reasons)


def diff_records(a: dict[str, Any], b: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fields where two extractions of the same call disagree.

    The plan runs the agent's live extraction against a post-hoc pass over the
    transcript and compares them: agreement is the cheapest confidence signal
    available, and a disagreement is a better review trigger than either pass's
    own self-assessment.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, spec in FIELDS_BY_KEY.items():
        for path in spec.paths:
            left = _read(a, path)
            right = _read(b, path)
            if _comparable(left) != _comparable(right):
                out[path] = {"a": left, "b": right}
        del key
    return out


def _read(record: dict[str, Any], path: str) -> Any:
    cursor: Any = record
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _comparable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(sorted(str(v) for v in value))
    return value


# --------------------------------------------------------------------------
# coercion
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# Things a model writes into a free-text field instead of leaving it empty.
# Every one of these reads as "filled" to the coverage gate, which is how a
# required question stops being asked.
_PLACEHOLDERS = frozenset(
    {
        "-",
        "n/a",
        "na",
        "none",
        "none specified",
        "not asked",
        "not discussed",
        "not mentioned",
        "not provided",
        "not specified",
        "not stated",
        "null",
        "unclear",
        "undisclosed",
        "unknown",
        "unspecified",
    }
)


def _no_placeholder(value: str) -> str:
    return "" if value.strip().lower() in _PLACEHOLDERS else value


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    text = _text(value).lower()
    return text if text in allowed else ""


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0 < number <= 100_000_000 else None


def _tribool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _dicts(value: Any, keys: tuple[str, ...]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = {key: _text(item.get(key)) for key in keys}
        if any(row.values()):
            out.append(row)
    return out


def _highlights(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        detail = _text(item.get("detail"))
        if not detail:
            continue
        out.append(
            {
                "title": _text(item.get("title")),
                "detail": detail,
                "skills": tx.normalize_skills(_strings(item.get("skills"))),
            }
        )
    return out


def _valid_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text in FIELDS_BY_KEY and text not in out:
            out.append(text)
    return tuple(out)


def known_context(*, name: str = "", email: str = "", phone: str = "", extra: str = "") -> str:
    """Render pre-call form values for the prompt's ``already_known`` block."""
    lines = [
        f"name: {name}" if name else "",
        f"email: {email}" if email else "",
        f"phone: {phone}" if phone else "",
        extra.strip(),
    ]
    return "\n".join(line for line in lines if line)


__all__ = [
    "EXTRACTOR_HEURISTIC",
    "EXTRACTOR_LLM",
    "SCHEMA_VERSION",
    "ExtractionResult",
    "diff_records",
    "extract_intake",
    "known_context",
    "normalize_record",
]
