"""The two submitters that never touch an employer.

``console`` is the default, for the same reason ``outreach/sender.py``'s
console provider is: an unconfigured deployment must not be one flag away from
sending real applications in somebody's name. ``memory`` is what tests assert
against.
"""

from __future__ import annotations

import logging
import uuid

from .base import METHOD_CONSOLE, METHOD_MEMORY, SubmissionContext, SubmissionResult

logger = logging.getLogger(__name__)


class ConsoleSubmitter:
    """Logs the application it would have sent, and claims success.

    Success rather than failure on purpose: the point is to exercise the whole
    path -- status machine, attempt row, notification -- on a dev box. Nothing
    left the machine, and ``ApplicationSubmission.method`` says ``console``, so
    a submitted row from this adapter is never mistaken for a real one.
    """

    vendor = "console"
    method = METHOD_CONSOLE

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        logger.info(
            "[console-apply] %s -> %s at %s via %s (%s)\n  from=%s resume=%s letter=%s answers=%d",
            ctx.full_name,
            ctx.title,
            ctx.company,
            ctx.target.vendor or "unknown ATS",
            ctx.target.apply_url,
            ctx.from_address,
            ctx.resume.filename if ctx.resume else "-",
            ctx.cover_letter.filename if ctx.cover_letter else "-",
            len(ctx.answers),
        )
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=f"console-{uuid.uuid4().hex[:12]}",
            confirmation_ref="logged, not sent",
        )


class MemorySubmitter:
    """Captures each context in a list. Tests read ``.sent``."""

    vendor = "memory"
    method = METHOD_MEMORY

    def __init__(self, *, result: SubmissionResult | None = None) -> None:
        self.sent: list[SubmissionContext] = []
        self._result = result

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        self.sent.append(ctx)
        if self._result is not None:
            return self._result
        return SubmissionResult(
            ok=True,
            method=self.method,
            external_id=f"memory-{len(self.sent)}",
            confirmation_ref="captured in memory",
        )

    def clear(self) -> None:
        self.sent.clear()
