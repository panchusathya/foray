"""LinkedIn URL detection and canonicalisation.

Kept off the Playwright scraper so identity matching and the CRM lookup can
import this without pulling a browser stack. The regex is the same shape as
Foray's ``find_linkedin``: a ``linkedin.com/in/...`` URL, optional scheme and
www, nothing else.
"""

from __future__ import annotations

import re

# Bare host is accepted: people paste "linkedin.com/in/ada" into iMessage.
_LINKEDIN = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/([A-Za-z0-9_%.-]+)",
    re.IGNORECASE,
)


def linkedin_slug(value: str) -> str:
    """The ``/in/{slug}`` token, or empty when ``value`` is not a profile URL."""
    text = (value or "").strip()
    if not text:
        return ""
    match = _LINKEDIN.search(text)
    if not match:
        return ""
    return match.group(1).rstrip("._-").lower()


def canonical_linkedin_url(value: str) -> str:
    """``https://www.linkedin.com/in/{slug}``, or empty."""
    slug = linkedin_slug(value)
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def find_linkedin(text: str) -> str:
    """First LinkedIn profile URL in ``text``, canonicalised, or empty."""
    return canonical_linkedin_url(text or "")
