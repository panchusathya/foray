"""Which submitter handles which vendor, and the one rule about defaults.

``get_submitter`` answers for a target; ``None`` means we have no adapter and
the application stays a Tier-0 hand-off. That is a normal answer, not an
error: recognising Taleo does not mean we can submit to Taleo.

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


def _live_submitters() -> dict[str, Submitter]:
    """Built per call so a settings change (or a test) is picked up, and so an
    optional dependency for one vendor cannot break the import of the rest."""
    from .ashby import AshbySubmitter
    from .email import EmailSubmitter
    from .greenhouse import GreenhouseSubmitter
    from .lever import LeverSubmitter

    built: dict[str, Submitter] = {
        detect.VENDOR_EMAIL: EmailSubmitter(),
        detect.VENDOR_GREENHOUSE: GreenhouseSubmitter(),
        detect.VENDOR_LEVER: LeverSubmitter(),
        detect.VENDOR_ASHBY: AshbySubmitter(),
    }
    try:
        from .browser.workday import WorkdaySubmitter
    except ImportError as exc:  # playwright is an optional extra
        logger.debug("workday submitter unavailable: %s", exc)
    else:
        built[detect.VENDOR_WORKDAY] = WorkdaySubmitter()
    return built


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


def get_submitter(target: detect.ApplyTarget) -> Submitter | None:
    """The adapter for this target, or ``None`` for a Tier-0 hand-off."""
    if not target.vendor:
        return None
    if target.vendor in _overrides:
        return _overrides[target.vendor]

    current = mode()
    if current == MODE_MEMORY:
        return memory_submitter()
    if current == MODE_CONSOLE:
        return ConsoleSubmitter()
    submitter = _live_submitters().get(target.vendor)
    if submitter is None:
        logger.info("no live submitter for %s; staying a hand-off", target.vendor)
    return submitter


def submittable(target: detect.ApplyTarget) -> bool:
    """Whether an automated attempt is possible at all, for copy that must not
    promise a submission we cannot make."""
    return get_submitter(target) is not None
