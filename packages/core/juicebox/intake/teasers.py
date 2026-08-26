"""End-of-call job teasers: the roles worth naming before hangup, composed in code.

A finished intake used to close on "a recruiter will follow up", which spends
the one moment the candidate is most engaged saying nothing. This module turns
that moment into a pitch: two or three open roles that match what the caller
just said they wanted, spoken at a high level, with the full list offered by
text.

Everything speakable is composed here and read verbatim, the ``progress_line``
/ ``_on_file_summary`` rule: a company name, a pay figure, or a count that the
model invents is exactly the claim this feature cannot afford, so the agent is
handed finished sentences and forbidden to improvise any of the three.

**Selection reads only what the candidate asked for.** Postings are ranked with
``jobmatch.preference.preference_term`` against a preference built from the
live transcript — stated titles, locations, comp floor, exclusions — never with
the candidate-fact terms ``score_pair`` blends for recruiters. Two reasons, and
both are load-bearing:

* The intake channel evaluates nobody (``INTAKE_COMPLIANCE.md`` §5). A pitch
  built from preference fit is "this matches what you said you want"; one built
  from the match engine's candidate scoring would put an evaluation of the
  person into the call.
* It needs no ``JobMatch`` rows. A first-time caller is scored by a worker
  *after* the call, so anything reading the jobs book mid-call would be racing
  a queue it cannot see.

**What may be named is decided by the posting's provenance.** Open-market
postings (``exa`` / ``manual`` / ``csv``) are public listings and get the
company said out loud. A ``project`` posting is a client mandate, and the
prompt's "never name a client" rule stays true: the line says "one of our
client companies" instead. Pay is quoted only from the posting's own published
range (``parsed_spec``), only in USD, because reading a range the posting
states is pay transparency and inventing one is a promise.

**The delivery promise tracks what can actually send.** The close must not
promise a text no channel can deliver (the ``sms_enabled`` comment in
``config.py`` records that rule). The guidance mirrors the routing in
``messaging.voice_handoff``: a standing text thread first, then a new Linq
chat where Linq is enabled and the number is clean, and otherwise a recruiter
follow-up — with the handler parking a review flag whenever an opted-in caller
was pitched and the send still cannot happen.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    JobMatch,
    JobPosting,
    record_event,
)
from ..search.parser import ParsedQuery
from .extract import extract_intake
from .records import EXTENSION_FIELDS, merge_record
from .schema import coverage

logger = logging.getLogger(__name__)

# Sources whose company may be said on a call. A `project` posting is a client
# mandate; everything else is a public listing. Membership here is the policy,
# so a new source is unnamed until someone decides otherwise.
NAMEABLE_SOURCES = frozenset({"exa", "manual", "csv"})

# The gate has not closed, so there is nothing defensible to pitch yet. The
# agent hears this only if it calls the tool early; the prompt tells it not to.
GUIDANCE_NOT_READY = (
    "The intake is not finished, so there is nothing to preview yet. Keep "
    "following check_coverage and do not mention roles or a database."
)

_PITCH_GUIDANCE = (
    "Read each sentence in lines aloud, naturally and completely -- these are "
    "the only role, company, and pay details you may say, exactly as written, "
    "and they describe a match with what the candidate said they wanted, "
    "never how strong the candidate is. If more_line is present, say it "
    "after the lines. Then ask what they think about these opportunities. If "
    "they do not like them, ask what they would rather see, listen, and call "
    "preview_matches again with the updated conversation. If they do like them, "
    "ask whether they would like the details by text. Only after an explicit "
    "yes, call start_text_verification. Do exactly what its guidance says."
)


def empty_teasers(guidance: str) -> dict[str, Any]:
    """The no-pitch answer. The agent closes as it would have without this tool."""
    return {"matches": 0, "lines": [], "more_line": "", "guidance": guidance}


def close_normally() -> dict[str, Any]:
    """No pitch, ordinary close — for a call the endpoint cannot even place."""
    from .service import _wrap_up_guidance

    return empty_teasers(_wrap_up_guidance())


def teaser_snapshot(
    db: Session,
    *,
    call: IntakeCall,
    transcript: str,
    known: str = "",
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The wrap-up pitch, computed from what has actually been said.

    Mirrors ``coverage_snapshot``'s inputs on purpose: the same live extraction,
    the same ``prior`` merge, so the preference being pitched against is the
    record the gate just closed on. Degrades rather than fails — an error here
    is heard as an agent going quiet at the exact moment it promised to be
    interesting, so any failure returns the normal close instead of a 4xx.
    """
    from .service import _wrap_up_guidance

    fallback = _wrap_up_guidance()
    settings = get_settings()
    if not settings.intake_teasers_enabled:
        return empty_teasers(fallback)

    try:
        result = extract_intake(transcript, known=known, fast=True)
        record = merge_record(prior, result.record) if prior else result.record
        if not coverage(record, core_only=call.gate_mode == "core").required_complete:
            return empty_teasers(GUIDANCE_NOT_READY)

        ranked = rank_postings(
            db, org_id=call.org_id, record=record, candidate_id=call.candidate_id
        )
    except Exception:  # a silent agent is the worse failure
        logger.exception("teaser snapshot failed for call %s", call.id)
        return empty_teasers(fallback)

    if not ranked:
        return empty_teasers(fallback)

    top = ranked[: max(settings.intake_teaser_count, 1)]
    lines = [_teaser_line(posting) for posting, _score in top]
    offered = [
        {"posting_id": posting.id, "line": line, "score": round(score, 4)}
        for (posting, score), line in zip(top, lines, strict=True)
    ]
    call.matches_offered = offered
    record_event(
        db,
        org_id=call.org_id,
        type="intake.matches_offered",
        subject_type="intake_call",
        subject_id=call.id,
        payload={
            "candidate_id": call.candidate_id,
            "posting_ids": [entry["posting_id"] for entry in offered],
            "matched": len(ranked),
        },
    )
    return {
        "matches": len(ranked),
        "lines": lines,
        "more_line": _more_line(len(ranked) - len(top)),
        "guidance": _PITCH_GUIDANCE,
    }


def rank_postings(
    db: Session,
    *,
    org_id: str,
    record: dict[str, Any],
    candidate_id: str | None = None,
) -> list[tuple[JobPosting, float]]:
    """Open postings ranked by preference fit alone, best first.

    Vetoes (an excluded company, a stated ceiling under their floor) and
    anything under ``intake_teaser_min_score`` are dropped rather than ranked
    low: a weak match pitched with enthusiasm burns the trust the pitch exists
    to build. A posting the candidate's recruiter already dismissed for them,
    and the company they currently work at, are never pitched.
    """
    from ..jobmatch.preference import preference_term
    from ..jobmatch.referrals import is_incumbent

    preference = _transient_preference(org_id, record)
    candidate = db.get(Candidate, candidate_id) if candidate_id else None
    dismissed: set[str] = set()
    if candidate_id:
        dismissed = set(
            db.scalars(
                select(JobMatch.job_posting_id).where(
                    JobMatch.candidate_id == candidate_id,
                    JobMatch.status == "dismissed",
                )
            )
        )

    floor = get_settings().intake_teaser_min_score
    scored: list[tuple[JobPosting, float]] = []
    postings = db.scalars(
        select(JobPosting).where(JobPosting.org_id == org_id, JobPosting.status == "open")
    ).all()
    for posting in postings:
        if not (posting.title or "").strip():
            continue
        if posting.id in dismissed:
            continue
        if candidate is not None and is_incumbent(candidate, posting):
            continue
        spec = ParsedQuery.from_dict(posting.parsed_spec or {})
        score, criteria = preference_term(preference, spec, posting=posting)
        if score is None or score < floor:
            continue
        if any(item.get("veto") for item in criteria):
            continue
        scored.append((posting, score))
    # Deterministic under ties, so a retried tool call pitches the same roles.
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    return scored


def _transient_preference(org_id: str, record: dict[str, Any]) -> CandidatePreference:
    """A preference row shaped from the live record, never added to the session.

    ``preference_term`` reads ``spec`` plus the comp extension columns; giving
    it a real (unpersisted) row means the mid-call ranking and the post-call
    match engine read one vocabulary, and cannot drift.
    """
    preference = CandidatePreference(org_id=org_id)
    preference.spec = dict(record.get("spec") or {})
    for name in EXTENSION_FIELDS:
        setattr(preference, name, record.get(name))
    return preference


def _teaser_line(posting: JobPosting) -> str:
    """One role as one spoken sentence fragment.

    "a Senior M&A Analyst role at JPMorgan in San Francisco, paying upwards of
    $200,000" — title always, company only when the source allows naming it,
    location or remote when known, pay only as the posting itself states it.
    """
    title = (posting.title or "").strip()
    company = (posting.company or "").strip()
    location = (posting.location or "").strip()

    line = f"a {title} role"
    if company and posting.source in NAMEABLE_SOURCES:
        line += f" at {company}"
    elif posting.source == "project":
        line += " with one of our client companies"
    if location:
        line += f" in {location}"
    elif posting.open_to_remote:
        line += ", fully remote"
    comp = _comp_phrase(posting.parsed_spec or {})
    if comp:
        line += f", {comp}"
    return line


def _comp_phrase(parsed_spec: dict[str, Any]) -> str:
    """The posting's own published range, or nothing.

    Only what the posting states, and only in dollars: a non-USD figure read
    out as dollars misquotes the role, and converting needs a rate — the same
    refusal ``jobmatch.preference`` makes for the comp veto, for the same
    reason a wrong number here is worse than none.
    """
    currency = str(parsed_spec.get("compensation_currency") or "").strip().upper()
    if currency and currency != "USD":
        return ""
    low = _as_number(parsed_spec.get("min_compensation"))
    high = _as_number(parsed_spec.get("max_compensation"))
    if low and high:
        return f"paying {_dollars(low)} to {_dollars(high)}"
    if high:
        return f"paying up to {_dollars(high)}"
    if low:
        return f"paying upwards of {_dollars(low)}"
    return ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _dollars(value: float) -> str:
    return f"${round(value):,}"


def _more_line(extra: int) -> str:
    """The composed count of what was not pitched, like ``progress_line``.

    The prompt forbids the agent to quote a number of roles precisely so that
    the only counts a caller ever hears are computed ones. Capped in words
    above twenty: past that the number stops informing and starts selling.
    """
    if extra <= 0:
        return ""
    if extra == 1:
        return "There is one more in our database that fits what you described."
    if extra <= 20:
        return f"There are {extra} more in our database that fit what you described."
    return "There are more than twenty others in our database that fit what you described."
