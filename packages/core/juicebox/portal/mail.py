"""Who candidate-facing portal mail comes from.

The outreach sender is a named recruiter writing to a candidate. A sign-in
code is the system talking, and putting that in a person's personal mailbox
is the wrong return address twice over: replies land somewhere nobody reads,
and a candidate who has never met that recruiter gets an unexplained email
from them.

One function rather than two settings read at the call site, because the
address and the display name have to move together. Pairing a configured
portal address with the outreach display name would put one person's name on
another person's mailbox -- which is worse than either default alone.
"""

from __future__ import annotations

from ..config import get_settings


def portal_sender() -> tuple[str, str]:
    """``(from_email, from_name)`` for portal mail.

    Falls back to the outreach identity as a pair when ``PORTAL_FROM_EMAIL``
    is unset, so a deployment that never configures one keeps sending.
    """
    settings = get_settings()
    if settings.portal_from_email:
        return settings.portal_from_email, settings.portal_from_name
    return settings.outreach_from_email, settings.outreach_from_name
