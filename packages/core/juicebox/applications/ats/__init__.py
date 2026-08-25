"""ATS identification: which vendor is behind a posting, and where its form is.

The pure URL classifier is ``detect.detect_target``; ``resolve_target`` is the
one that touches the network and caches onto the posting row.
"""

from .detect import (
    TIER_API,
    TIER_BROWSER,
    TIER_EMAIL,
    TIER_HANDOFF,
    VENDOR_ASHBY,
    VENDOR_EMAIL,
    VENDOR_GREENHOUSE,
    VENDOR_LEVER,
    VENDOR_ORACLE,
    VENDOR_SMARTRECRUITERS,
    VENDOR_WORKDAY,
    ApplyTarget,
    apply_email,
    detect_target,
    is_aggregator,
    resolve_target,
    target_of,
)

__all__ = [
    "TIER_API",
    "TIER_BROWSER",
    "TIER_EMAIL",
    "TIER_HANDOFF",
    "VENDOR_ASHBY",
    "VENDOR_EMAIL",
    "VENDOR_GREENHOUSE",
    "VENDOR_LEVER",
    "VENDOR_ORACLE",
    "VENDOR_SMARTRECRUITERS",
    "VENDOR_WORKDAY",
    "ApplyTarget",
    "apply_email",
    "detect_target",
    "is_aggregator",
    "resolve_target",
    "target_of",
]
