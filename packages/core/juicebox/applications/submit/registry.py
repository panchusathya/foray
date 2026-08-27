"""Which submitter handles which vendor, and the two rules that decide.

``get_submitter`` answers for a target; ``None`` means we have no adapter and
the application stays a Tier-0 hand-off. That is a normal answer, not an
error: recognising Taleo does not mean we can submit to Taleo.

**Rule two: a board key decides API versus form.** Greenhouse, Lever and Ashby
each publish an apply API and each authenticates with a key the *employer*
issues from their own account. Holding one is the relationship, so when
``board_key`` is present the API adapter runs -- structured errors, a real
application id back. When it is absent, which is the open market and therefore
most postings, the candidate's own door is the hosted form and the browser
adapter drives it. Neither is a fallback for the other; they are two different
situations.

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
#: handwritten for a reason, and a derived spec must never run against them:
#: their logged-out page is a registration form, and "filling" it types a
#: fresh password into a sign-in wall.
ACCOUNT_GATED_VENDORS = (
    detect.VENDOR_WORKDAY,
    detect.VENDOR_ORACLE,
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


def _browser_submitter(vendor: str) -> Submitter | None:
    """Playwright is an optional extra, so an install without it still runs
    every other tier rather than failing to import."""
    try:
        from .browser.hosted_form import (
            AshbyFormSubmitter,
            GreenhouseFormSubmitter,
            LeverFormSubmitter,
            SmartRecruitersFormSubmitter,
            WorkableFormSubmitter,
        )
        from .browser.workday import WorkdaySubmitter
        from .browser.yc import YCSubmitter
    except ImportError as exc:
        logger.debug("browser submitters unavailable: %s", exc)
        return None
    built = {
        detect.VENDOR_GREENHOUSE: GreenhouseFormSubmitter,
        detect.VENDOR_LEVER: LeverFormSubmitter,
        detect.VENDOR_ASHBY: AshbyFormSubmitter,
        detect.VENDOR_WORKDAY: WorkdaySubmitter,
        detect.VENDOR_YC: YCSubmitter,
        detect.VENDOR_SMARTRECRUITERS: SmartRecruitersFormSubmitter,
        detect.VENDOR_WORKABLE: WorkableFormSubmitter,
    }.get(vendor)
    return built() if built is not None else None


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


def _derived_submitter(spec) -> Submitter | None:
    """A hosted-form run over a spec derived from the page itself.

    Same optional-Playwright contract as ``_browser_submitter``: an install
    without the browser extra still runs every other tier.
    """
    try:
        from .browser.hosted_form import HostedFormSubmitter
    except ImportError as exc:
        logger.debug("derived submitter unavailable: %s", exc)
        return None
    return HostedFormSubmitter(spec)


def get_submitter(
    target: detect.ApplyTarget, *, board_key: str = "", derived_spec=None
) -> Submitter | None:
    """The adapter for this target, or ``None`` for a Tier-0 hand-off.

    ``board_key`` is the employer's own API key for this board, when we hold
    one -- ``applications/credentials.board_key`` is where it comes from and
    empty is the ordinary case.

    ``derived_spec`` is the posting's own derived ``FormSpec``, when one is
    cached (``applications/formspec.usable_spec``). It is strictly the
    fallback: every handwritten adapter, keyed API and the email path win
    first, and an account-gated vendor never takes one.
    """
    if not target.vendor and derived_spec is None:
        return None
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
    submitter = _browser_submitter(target.vendor) if target.vendor else None
    if (
        submitter is None
        and derived_spec is not None
        and target.vendor not in ACCOUNT_GATED_VENDORS
    ):
        submitter = _derived_submitter(derived_spec)
    if submitter is None:
        logger.info(
            "no live submitter for %s; staying a hand-off", target.vendor or "this board"
        )
    return submitter


def submittable(target: detect.ApplyTarget, *, board_key: str = "") -> bool:
    """Whether an automated attempt is possible at all, for copy that must not
    promise a submission we cannot make."""
    return get_submitter(target, board_key=board_key) is not None


#: Vendors a live deployment can actually submit to, browser tier included.
#: Kept as data rather than derived from ``_browser_submitter`` so the answer
#: does not depend on whether Playwright happens to be installed in *this*
#: process: the question "can Foray apply here" is about the product, and the
#: web container asking it has no Chromium while the worker that submits does.
BROWSER_VENDORS = (
    detect.VENDOR_GREENHOUSE,
    detect.VENDOR_LEVER,
    detect.VENDOR_ASHBY,
    detect.VENDOR_WORKDAY,
    detect.VENDOR_YC,
    detect.VENDOR_SMARTRECRUITERS,
    detect.VENDOR_WORKABLE,
)


def can_submit_vendor(vendor: str) -> bool:
    """Whether *some* adapter exists for this vendor, regardless of mode.

    ``submittable`` answers "would a submit attempt run right now", and under
    ``console``/``memory`` that is true for any vendor because the double
    accepts everything. This answers the question the proposal gate needs --
    "should we promise to apply to this role" -- which must not change when an
    operator flips APPLY_SUBMITTER to rehearse.
    """
    return bool(vendor) and (vendor == detect.VENDOR_EMAIL or vendor in BROWSER_VENDORS)


def can_apply(posting, target: detect.ApplyTarget) -> bool:
    """``can_submit_vendor``, plus the posting's own derived spec.

    The posting-aware sibling the carding gate needs: an unknown board with a
    usable derived spec is applicable, and one without is not. Same
    mode-independence contract as ``can_submit_vendor`` -- flipping
    ``APPLY_SUBMITTER`` to rehearse changes neither answer.
    """
    if can_submit_vendor(target.vendor):
        return True
    if target.vendor:
        # Recognised but unsupported (iCIMS, Taleo, Oracle): a wizard or an
        # account wall, and a derived page-1 spec would card a promise the
        # one-page submitter cannot keep.
        return False
    from ..formspec import usable_spec

    return usable_spec(posting) is not None
