"""Where connector fetches become intake facts — and the rules for which
facts they are allowed to become.

Two rules carry the module:

* **The resume is chosen, not collected.** ``pick_resume`` ranks the hits
  (their own attachment beats a received one, a filename saying resume/cv
  beats one that does not, newer beats older), downloads at most
  ``MAX_FETCH_ATTEMPTS`` of them, and keeps the first that sniffs as a real
  PDF/DOCX. Losing candidates' bytes are discarded on the spot — one document
  is the whole take.

* **Haves are not wants.** GitHub languages are skills the person
  demonstrably HAS; ``spec.must_have_skills`` is what they *insist the next
  role uses*, and ``jobmatch.preference`` scores it against a posting's
  requirements. Writing haves into wants makes every posting that omits one
  of their hobbies score worse — the same class of bug AGENTS.md records for
  the heuristic extractor. Connector skills therefore go onto the
  ``Candidate.skills`` haystack (blanks only, matching ``enrich.promote``'s
  posture), never into the spec.
"""

from __future__ import annotations

import re

from ..docgen.parse import sniff_resume_type
from ..enrich.linkedin_scrape import LinkedInProfile, is_ic_title
from .base import CandidateConnector, ConnectorProfile, ResumeHit

#: Attachments larger than this are skipped unfetched — a 40MB "resume" is a
#: portfolio, and the parse pipeline gains nothing from swallowing it.
MAX_RESUME_BYTES = 10 * 1024 * 1024
#: How many ranked hits we will download before giving up on the resume path.
MAX_FETCH_ATTEMPTS = 3

_RESUME_NAME = re.compile(r"(resume|\bcv\b)", re.IGNORECASE)
_RESUME_EXT = re.compile(r"\.(pdf|docx?)$", re.IGNORECASE)


def rank_hits(hits: list[ResumeHit]) -> list[ResumeHit]:
    """Best candidate first. Stable, so a provider's own ordering (usually
    newest-first) breaks the remaining ties."""

    def key(h: ResumeHit) -> tuple:
        named = bool(_RESUME_NAME.search(h.filename or ""))
        stamp = h.occurred_at.timestamp() if h.occurred_at else 0.0
        return (not h.sent_by_owner, not named, -stamp)

    plausible = [h for h in hits if _RESUME_EXT.search(h.filename or "")]
    return sorted(plausible, key=key)


def pick_resume(
    db, connector: CandidateConnector, connection, hits: list[ResumeHit]
) -> tuple[bytes, ResumeHit] | None:
    """Download the best-ranked hits until one sniffs as a real resume file.

    Returns ``(bytes, hit)`` or None. A failed download or a mis-sniffed file
    moves to the next hit rather than failing the index — the caller's
    fallback (profile-only, then gap-fill) is always available.
    """
    attempts = 0
    for hit in rank_hits(hits):
        if attempts >= MAX_FETCH_ATTEMPTS:
            break
        if hit.size_bytes and hit.size_bytes > MAX_RESUME_BYTES:
            continue
        attempts += 1
        try:
            data = connector.fetch_resume(db, connection, hit)
        except Exception:  # noqa: BLE001 - the next hit is the retry
            continue
        if not data or len(data) > MAX_RESUME_BYTES:
            continue
        if sniff_resume_type(data):
            return data, hit
    return None


def to_linkedin_profile(profile: ConnectorProfile) -> LinkedInProfile:
    """The connector's derived facts, in the shape the settle machinery speaks.

    ``apply_profile_to_record`` and ``confirm_message`` both take a
    ``LinkedInProfile``; emitting one means the connect path reuses the scrape
    path's merge and copy without either knowing the difference.
    """
    title = (profile.current_title or "").strip()
    return LinkedInProfile(
        url=profile.linkedin_url or "",
        full_name=(profile.full_name or "").strip(),
        headline=(profile.headline or "")[:300],
        current_title=title,
        current_employer=(profile.current_employer or "").strip(),
        location=(profile.location or "").strip(),
        experience_years=profile.experience_years,
        is_ic=is_ic_title(title),
    )


def scrape_payload(profile: ConnectorProfile, *, provider: str) -> dict:
    """What goes into the ``_linkedin.scrape`` flow-meta slot.

    The ``LinkedInProfile.to_dict()`` shape the confirm step reads, plus the
    connector extras — extra keys are fine, missing ones are not.
    """
    payload = to_linkedin_profile(profile).to_dict()
    payload["skills"] = list(profile.skills)
    payload["github_url"] = profile.github_url
    payload["provider"] = provider
    return payload


def apply_skills_to_candidate(candidate, skills: tuple[str, ...] | list[str]) -> bool:
    """Fill ``Candidate.skills`` from connector signals — blanks only.

    The same posture as ``enrich.promote``: skills feed the retrieval haystack
    (search/index.py), so a connector may seed an empty field but never rewrite
    one a human or a resume already populated. Returns True when it wrote.
    """
    cleaned = [str(s).strip() for s in skills if str(s).strip()]
    if not cleaned or (candidate.skills or []):
        return False
    candidate.skills = cleaned[:12]
    return True
