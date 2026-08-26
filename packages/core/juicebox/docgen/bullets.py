"""Bullets for a role the candidate never described, from what they did say.

Most people never fill in LinkedIn's description box. A profile fetch
therefore returns six real jobs — employer, title, dates, all true — and
nothing underneath any of them, and the resume we build from it is a list of
places somebody has been. That document does not survive a keyword screen and
does not read as finished.

The material to fix it usually exists and is simply in the wrong table. A
voice intake is fifteen minutes of the candidate describing this exact work; a
text intake is the same thing in shorter turns; ``highlights`` on the
preference row is stored *verbatim* precisely because it is phrasing a resume
does not have. This module moves those statements onto the page.

**It relocates; it does not compose.** Every drafted bullet arrives carrying
the span of evidence it came from, and ``_grounded`` throws away any bullet
whose quote is not in the evidence character for character, plus any bullet
carrying a number the quote does not. That check is the whole design: it is
mechanical, it runs on our side of the model, and it fails closed. A role the
candidate has never talked about gets no bullets at all — which is the same
answer ``docgen/answers.py`` gives to a form question the inputs do not
answer, for the same reason. A resume asserts things in a real person's name.
"""

from __future__ import annotations

import copy
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..intake.contact import candidate_speech
from ..llm.client import get_llm
from ..llm.prompts import (
    RESUME_BULLETS_SCHEMA,
    RESUME_BULLETS_SYSTEM,
    resume_bullets_prompt,
)
from ..models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    IntakeMessage,
    IntakeThread,
)

logger = logging.getLogger(__name__)

MAX_BULLETS_PER_ROLE = 3
MAX_WORDS_PER_BULLET = 30
# Shorter than this and a "quote" is a word or two that would match somewhere
# in any transcript, which is not provenance.
MIN_QUOTE_CHARS = 24


def _flatten(text: str) -> str:
    """Casefolded, whitespace- and punctuation-insensitive, for quote lookup.

    A transcript arrives with the provider's line breaks and smart quotes; a
    model asked to copy a span back reliably normalises both. Neither is a
    change of meaning, so neither should fail an honest bullet — while every
    *word* still has to be there in order.

    ``\\W`` rather than ``[^a-z0-9]``: the latter erases a non-Latin script
    entirely, which turns every quote from a call held in one into the empty
    string and silently switches drafting off for those candidates.
    """
    return re.sub(r"\W+", " ", str(text or "").casefold()).strip()


def candidate_evidence(db: Session, candidate: Candidate) -> str:
    """Everything this candidate has told us about their work, in their words.

    Their own statements only. A recruiter's note, our agent's side of the
    conversation and a vendor's profile blurb are all excluded: the first two
    are somebody else's words and the third is somebody else's guess, and none
    of the three is a thing the candidate can be held to in an interview.

    Excluding our own agent matters more here than anywhere else it is done,
    because downstream the *only* test of a bullet is whether its quote is
    somewhere in this string. Every sentence added here is a sentence a
    drafted bullet may be built on.
    """
    parts: list[str] = []

    preference = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
    )
    if preference is not None:
        highlights = [str(h).strip() for h in (preference.highlights or []) if str(h).strip()]
        if highlights:
            parts.append("What they said stood out:\n" + "\n".join(highlights))

    transcripts = db.scalars(
        select(IntakeCall.transcript)
        .where(IntakeCall.candidate_id == candidate.id, IntakeCall.transcript != "")
        .order_by(IntakeCall.created_at)
    ).all()
    for transcript in transcripts:
        # Their turns only. A transcript is a dialogue, and the agent's half of
        # it is full of quotable-looking sentences the candidate never said --
        # "so you led the platform rewrite?" answered with "yeah" would
        # otherwise pass the quote check as though they had described it.
        said = candidate_speech(str(transcript or "")).strip()
        if said:
            parts.append("From an intake call:\n" + said)

    replies = db.scalars(
        select(IntakeMessage.body)
        .join(IntakeThread, IntakeThread.id == IntakeMessage.thread_id)
        .where(
            IntakeThread.candidate_id == candidate.id,
            IntakeMessage.direction == "inbound",
            IntakeMessage.body != "",
        )
        .order_by(IntakeMessage.seq)
    ).all()
    said = [str(body).strip() for body in replies if str(body).strip()]
    if said:
        parts.append("What they texted us:\n" + "\n".join(said))

    return "\n\n".join(parts)


def remembered_bullets(db: Session, candidate: Candidate) -> dict[str, list[str]]:
    """Bullets already drafted for this candidate, keyed by employer.

    Consistency first, cost second. ``build_resume_json`` runs once per
    application, so without this a candidate who applies to twenty roles gets
    twenty separately drafted — and subtly different — accounts of the same
    job. Two employers reading two versions of one role is a worse failure
    than the twenty model calls it also saves.

    The profile resume is the store because it is already the one row per
    candidate that is kept current, and everything in it passed ``_grounded``
    when it was written.
    """
    # Local import: `docgen.profile` imports this module through
    # `docgen.resume`, and only the query predicate is wanted here.
    from .profile import profile_resume_of

    doc = profile_resume_of(db, candidate.id)
    structured = doc.structured if doc is not None else None
    if not isinstance(structured, dict):
        return {}

    out: dict[str, list[str]] = {}
    for job in structured.get("experience") or []:
        if not isinstance(job, dict):
            continue
        company = _flatten(job.get("company"))
        bullets = [str(b).strip() for b in (job.get("bullets") or []) if str(b).strip()]
        if company and bullets:
            out[company] = bullets
    return out


def _roles_needing_bullets(structured: dict) -> list[dict]:
    """Experience entries with an employer and nothing written underneath."""
    out: list[dict] = []
    for job in structured.get("experience") or []:
        if not isinstance(job, dict):
            continue
        if [b for b in (job.get("bullets") or []) if str(b).strip()]:
            continue
        if str(job.get("company") or "").strip():
            out.append(job)
    return out


def _roles_prompt(roles: list[dict]) -> str:
    return json.dumps(
        [
            {
                "company": job.get("company") or "",
                "title": job.get("title") or "",
                "start": job.get("start") or "",
                "end": job.get("end") or "",
                "location": job.get("location") or "",
            }
            for job in roles
        ],
        ensure_ascii=False,
        indent=2,
    )


_NUMBER = re.compile(r"\d[\d,.]*")


def _grounded(text: str, quote: str, flat_evidence: str) -> bool:
    """Is this bullet a relocation of something the candidate actually said?"""
    text, quote = str(text or "").strip(), str(quote or "").strip()
    if not text or len(quote) < MIN_QUOTE_CHARS:
        return False
    if len(text.split()) > MAX_WORDS_PER_BULLET:
        return False

    flat_quote = _flatten(quote)
    if not flat_quote or flat_quote not in flat_evidence:
        return False

    # Scale is what people lie about on a resume and what a reference check
    # catches, so a figure has to come from the quote and not from around it.
    quoted_numbers = {n.rstrip(".,") for n in _NUMBER.findall(quote)}
    return all(n.rstrip(".,") in quoted_numbers for n in _NUMBER.findall(text))


def _survivors(drafted: dict, evidence: str) -> dict[str, list[str]]:
    """The drafted bullets that passed the guard, keyed by employer."""
    flat_evidence = _flatten(evidence)
    by_company: dict[str, list[str]] = {}
    for role in drafted.get("roles") or []:
        if not isinstance(role, dict):
            continue
        company = _flatten(role.get("company"))
        if not company:
            continue
        kept = [
            str(bullet.get("text")).strip()
            for bullet in (role.get("bullets") or [])
            if isinstance(bullet, dict)
            and _grounded(bullet.get("text"), bullet.get("quote"), flat_evidence)
        ]
        if kept:
            by_company.setdefault(company, []).extend(kept)
    return by_company


def _apply(structured: dict, by_company: dict[str, list[str]]) -> tuple[dict, int]:
    """Write bullets onto a copy of the resume, one employer at a time."""
    if not by_company:
        return structured, 0

    available = {company: list(bullets) for company, bullets in by_company.items()}
    filled = copy.deepcopy(structured)
    added = 0
    for job in _roles_needing_bullets(filled):
        # `pop`, not `get`: a promotion is two bare rows at one employer, and
        # printing the same three bullets under both is worse than leaving the
        # second one bare — which is what this then does.
        bullets = available.pop(_flatten(job.get("company")), None)
        if not bullets:
            continue
        job["bullets"] = bullets[:MAX_BULLETS_PER_ROLE]
        added += len(job["bullets"])
    return filled, added


def fill_missing_bullets(
    structured: dict, *, evidence: str, remembered: dict[str, list[str]] | None = None
) -> tuple[dict, int]:
    """``(resume, bullets added)``. Returns the input unchanged when it can't.

    ``remembered`` is what was drafted for this candidate previously — see
    ``remembered_bullets``. It is applied first and a role it fills is not
    sent to the model again, so a candidate's account of one job is written
    once and then reused on every application.

    Unchanged is the common case and not a failure: no roles are bare, or the
    candidate has told us nothing yet, or there is no model configured. The
    resume that ships is the same truthful one either way.
    """
    if not _roles_needing_bullets(structured):
        return structured, 0

    structured, added = _apply(structured, remembered or {})

    roles = _roles_needing_bullets(structured)
    if not roles or not str(evidence or "").strip():
        return structured, added

    llm = get_llm()
    if not llm.available:
        return structured, added
    try:
        result = llm.json(
            system=RESUME_BULLETS_SYSTEM,
            prompt=resume_bullets_prompt(roles=_roles_prompt(roles), evidence=evidence),
            schema=RESUME_BULLETS_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - the bare resume ships instead
        logger.warning("resume bullet drafting failed, leaving the roles bare: %s", exc)
        return structured, added

    drafted = result.data or {}
    if not isinstance(drafted, dict):
        return structured, added
    filled, drafted_count = _apply(structured, _survivors(drafted, evidence))
    logger.info(
        "drafted %d grounded bullet(s) across %d bare role(s)", drafted_count, len(roles)
    )
    return filled, added + drafted_count
