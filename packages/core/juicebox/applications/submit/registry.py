"""Which submitter handles which vendor, and the two rules that decide.

``get_submitter`` answers for a target; ``None`` means we have no adapter and
the application stays a Tier-0 hand-off. That is a normal answer, not an
error: recognising Taleo does not mean we can submit to Taleo.

**Rule two: a board key decides API versus form.** Greenhouse, Lever and Ashby
each publish an apply API and each authenticates with a key the *employer*
issues from their own account. Holding one is the relationship, so when
``board_key`` is present the API adapter runs -- structured errors, a real
application id back. When it is absent, which is the open market and therefore
most postings, the candidate's own door is the hosted form. A configured
browser agent drives that form; without the agent the posting is a hand-off.
Neither is a fallback for the other; they are two different situations.

**The global default is never a real sender.** ``APPLY_SUBMITTER`` gates the
whole feature: ``console`` (the default) exercises the path without touching an
employer, ``memory`` is the test double, and ``live`` is the only value that
lets a vendor adapter run. A deployment that has not made that choice
explicitly does not submit applications in anybody's name.
"""

from __future__ import annotations

import logging

from ...config import get_settings
from ..ats import detect
from .base import Submitter
from .console import ConsoleSubmitter, MemorySubmitter

logger = logging.getLogger(__name__)

MODE_CONSOLE = "console"
MODE_MEMORY = "memory"
MODE_LIVE = "live"

_overrides: dict[str, Submitter] = {}
_memory: MemorySubmitter | None = None


#: Vendors whose apply API we speak, when the employer has given us a key.
KEYED_API_VENDORS = (
    detect.VENDOR_GREENHOUSE,
    detect.VENDOR_LEVER,
    detect.VENDOR_ASHBY,
)

#: Vendors that gate applying behind an account. Their two-leg flows are
#: handwritten for a reason, and the public-form agent must never run against
#: them: their logged-out page is a registration form, and "filling" it types a
#: fresh password into a sign-in wall.
ACCOUNT_GATED_VENDORS = (
    detect.VENDOR_WORKDAY,
    detect.VENDOR_ORACLE,
    detect.VENDOR_YC,
)

#: Two-leg adapters we actually ship. Oracle is account-gated but has no
#: submitter yet, so it stays a hand-off.
TWO_LEG_VENDORS = (
    detect.VENDOR_WORKDAY,
    detect.VENDOR_YC,
)


def _api_submitter(vendor: str, board_key: str) -> Submitter | None:
    from .ashby import AshbySubmitter
    from .greenhouse import GreenhouseSubmitter
    from .lever import LeverSubmitter

    built = {
        detect.VENDOR_GREENHOUSE: GreenhouseSubmitter,
        detect.VENDOR_LEVER: LeverSubmitter,
        detect.VENDOR_ASHBY: AshbySubmitter,
    }.get(vendor)
    return built(board_key=board_key) if built is not None else None


def _two_leg_submitter(vendor: str) -> Submitter | None:
    """Playwright is an optional extra, so an install without it still runs
    every other tier rather than failing to import."""
    try:
        from .browser.workday import WorkdaySubmitter
        from .browser.yc import YCSubmitter
    except ImportError as exc:
        logger.debug("two-leg browser submitters unavailable: %s", exc)
        return None
    built = {
        detect.VENDOR_WORKDAY: WorkdaySubmitter,
        detect.VENDOR_YC: YCSubmitter,
    }.get(vendor)
    return built() if built is not None else None


def _agent_submitter(vendor: str) -> Submitter | None:
    try:
        from .browser.agent import BrowserAgentSubmitter
        from .browser.cloud import agent_configured
    except ImportError as exc:
        logger.debug("browser agent unavailable: %s", exc)
        return None
    if not agent_configured():
        return None
    return BrowserAgentSubmitter(vendor=vendor or "public")


def memory_submitter() -> MemorySubmitter:
    """The process-wide memory double, so a test can read what was sent."""
    global _memory
    if _memory is None:
        _memory = MemorySubmitter()
    return _memory


def set_submitter(vendor: str, submitter: Submitter | None) -> None:
    """Tests and dry runs pin one vendor; ``None`` removes the pin."""
    if submitter is None:
        _overrides.pop(vendor, None)
    else:
        _overrides[vendor] = submitter


def reset_overrides() -> None:
    _overrides.clear()
    global _memory
    _memory = None


def mode() -> str:
    configured = (get_settings().apply_submitter or MODE_CONSOLE).strip().lower()
    return configured if configured in {MODE_CONSOLE, MODE_MEMORY, MODE_LIVE} else MODE_CONSOLE


def get_submitter(
    target: detect.ApplyTarget, *, board_key: str = "", derived_spec=None
) -> Submitter | None:
    """The adapter for this target, or ``None`` for a Tier-0 hand-off.

    ``board_key`` is the employer's own API key for this board, when we hold
    one -- ``applications/credentials.board_key`` is where it comes from and
    empty is the ordinary case.

    ``derived_spec`` is accepted and ignored. Public-form maps and page-1
    derivation as a fill path are gone; the argument stays so callers that
    still pass a cached spec do not break while they are unwound.
    """
    del derived_spec
    if target.vendor in _overrides:
        return _overrides[target.vendor]

    current = mode()
    if current == MODE_MEMORY:
        return memory_submitter()
    if current == MODE_CONSOLE:
        return ConsoleSubmitter()

    if target.vendor == detect.VENDOR_EMAIL:
        from .email import EmailSubmitter

        return EmailSubmitter()
    if board_key and target.vendor in KEYED_API_VENDORS:
        return _api_submitter(target.vendor, board_key)
    if target.vendor in TWO_LEG_VENDORS:
        return _two_leg_submitter(target.vendor)
    if target.vendor in ACCOUNT_GATED_VENDORS:
        logger.info("no live submitter for account-gated %s; staying a hand-off", target.vendor)
        return None
    submitter = _agent_submitter(target.vendor)
    if submitter is None:
        logger.info(
            "no live submitter for %s; staying a hand-off", target.vendor or "this board"
        )
    return submitter


def submittable(target: detect.ApplyTarget, *, board_key: str = "") -> bool:
    """Whether an automated attempt is possible at all, for copy that must not
    promise a submission we cannot make."""
    return get_submitter(target, board_key=board_key) is not None


def can_submit_vendor(vendor: str) -> bool:
    """Whether *some* adapter exists for this vendor, regardless of mode.

    ``submittable`` answers "would a submit attempt run right now", and under
    ``console``/``memory`` that is true for any vendor because the double
    accepts everything. This answers the question the proposal gate needs --
    "should we promise to apply to this role" -- which must not change when an
    operator flips APPLY_SUBMITTER to rehearse.

    A configured browser agent makes any non-account-gated public board
    eligible, including an unrecognised vendor. Workday and YC stay on their
    handwritten two-leg adapters. Without the agent, public forms are a
    hand-off (a stored board key still selects the API adapter at submit time,
    which this gate cannot see).
    """
    if vendor == detect.VENDOR_EMAIL:
        return True
    if vendor in TWO_LEG_VENDORS:
        return True
    if vendor in ACCOUNT_GATED_VENDORS:
        return False
    try:
        from .browser.cloud import agent_configured
    except ImportError:
        return False
    return agent_configured()
