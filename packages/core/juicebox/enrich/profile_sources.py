"""Two ways to learn about a candidate from a LinkedIn URL, in order.

**Scrape first.** The browser scraper (``enrich.linkedin_scrape``) costs
nothing per lookup and is enough for onboarding: a title, an employer, a
location, roughly how long they have been working. That is all the
conversation needs to confirm back and shorten the interview.

**Fall back to the paid provider** (``enrich.linkedin_profile``) when the
scrape fails — an auth wall, a CAPTCHA, no session cookie, Playwright not
installed. The provider costs money per lookup, which is exactly why it is
second and not first.

**And reach for the paid provider deliberately when we need employment
history.** The scrape does not produce one; a tailored resume cannot be
written without it. So a package build with no ``Experience`` rows pays for
the lookup rather than generating a resume with a blank career.

Both paths converge on the same two outputs: a ``LinkedInProfile`` for the
conversation to confirm, and (from the paid path only) a full
``Candidate`` + ``Experience`` + ``Education`` promotion.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import Candidate
from .linkedin_scrape import LinkedInProfile

logger = logging.getLogger(__name__)


def profile_from_paid_payload(parsed: dict, *, url: str) -> LinkedInProfile:
    """The paid provider's upsert payload, narrowed to what the flow shows.

    Deliberately lossy: this is only what ``confirm_message`` reads back and
    what ``apply_profile_to_record`` is allowed to guess. The full history
    goes to the CRM through ``promote``, not through here.
    """
    experiences = parsed.get("experiences") or []
    current = next(
        (e for e in experiences if isinstance(e, dict) and e.get("is_current")),
        experiences[0] if experiences and isinstance(experiences[0], dict) else {},
    )
    return LinkedInProfile(
        url=str(parsed.get("linkedin_url") or url),
        full_name=str(parsed.get("full_name") or ""),
        headline=str(parsed.get("headline") or ""),
        current_title=str(current.get("title") or ""),
        current_employer=str(current.get("company") or ""),
        location=str(parsed.get("location_city") or ""),
        experience_years=_years_from(experiences),
        raw={"source": "paid_provider"},
    )


def _years_from(experiences: list) -> float | None:
    """Earliest start year to now, in whole years. None when undatable.

    Summing per-role durations would double-count overlapping stints and
    inflate somebody's experience, which is a claim we would then put in
    front of an employer.
    """
    years: list[int] = []
    for item in experiences:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start_date") or "")[:4]
        if start.isdigit():
            years.append(int(start))
    if not years:
        return None
    from ..models import utcnow

    span = utcnow().year - min(years)
    return float(span) if 0 < span < 60 else None


def paid_lookup(
    db: Session,
    *,
    org_id: str,
    linkedin_url: str,
    candidate: Candidate | None = None,
    thread_id: str = "",
) -> tuple[LinkedInProfile | None, str]:
    """Pay for one profile and promote it. Returns ``(profile, error)``.

    Never raises: every caller is either mid-conversation or mid-package, and
    neither should die because a vendor is down. The negative-cache rules from
    the worker handler apply here too — a real miss is stamped, an outage is
    not, so a quota wall cannot mark somebody permanently unfetchable.
    """
    from .linkedin_profile import (
        ProfileNotFound,
        ProfileProviderUnavailable,
        get_profile_provider,
        map_profile,
    )
    from .promote import promote

    provider = get_profile_provider()
    if not provider.is_configured():
        return None, "no profile provider configured"
    try:
        raw = provider.fetch(linkedin_url)
    except ProfileNotFound:
        if candidate is not None:
            from ..models import utcnow

            candidate.profile_ingest_checked_at = utcnow()
            candidate.profile_ingest_error = "profile not found or not visible"
            db.flush()
        return None, "profile not found"
    except ProfileProviderUnavailable as exc:
        # Not cached: the vendor being down says nothing about this person.
        logger.warning("paid profile lookup unavailable for %s: %s", linkedin_url, exc)
        return None, f"provider unavailable: {exc}"
    except Exception as exc:
        logger.exception("paid profile lookup crashed for %s", linkedin_url)
        return None, f"provider error: {exc}"

    parsed = map_profile(raw, linkedin_url=linkedin_url)
    result = promote(db, org_id=org_id, parsed=parsed, candidate=candidate)
    promoted = db.get(Candidate, result.candidate_id) if result.candidate_id else None
    if promoted is not None:
        from ..models import utcnow

        promoted.profile_ingest_checked_at = utcnow()
        promoted.profile_ingest_error = ""
        db.flush()
    logger.info(
        "paid profile lookup filled %s (thread=%s, +%d roles, +%d schools)",
        linkedin_url,
        thread_id or "-",
        result.added_experiences,
        result.added_educations,
    )
    return profile_from_paid_payload(parsed, url=linkedin_url), ""
