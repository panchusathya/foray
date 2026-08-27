"""Public-form submitter: one governed browser-agent run per attempt."""

from __future__ import annotations

from ...base import METHOD_BROWSER, SubmissionContext, SubmissionResult
from .. import cloud as browser_cloud
from . import loop, planner
from .loop import LoopResult


class BrowserAgentSubmitter:
    """Fill a public application form through the typed-action planner.

    ``vendor`` is the detected board when we have one, or ``public`` for an
    unrecognised URL. Method is always ``browser`` so the consent machine and
    the attempt row stay on the same path Workday already uses.
    """

    method = METHOD_BROWSER

    def __init__(self, vendor: str = "public") -> None:
        self.vendor = vendor or "public"

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        if not browser_cloud.agent_configured():
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="browser agent is not configured",
            )
        if not planner.available():
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no vision model configured for the apply planner",
            )
        try:
            result = loop.run(ctx, allow_submit=True)
        except browser_cloud.ProfileQuota as exc:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=str(exc)[:400],
            )
        return _to_result(result)

    def resume(self, ctx: SubmissionContext, verification: dict) -> SubmissionResult:
        """Inspect a session that already requested final_submit. Never click."""
        result = loop.recover(ctx, verification)
        return _to_result(result)


def _to_result(result: LoopResult) -> SubmissionResult:
    from . import trace

    verification = trace.payload(
        session_id=result.session_id,
        final_submit_requested_at=result.final_submit_requested_at,
        steps=result.steps,
        artifacts=result.artifacts,
    )
    return SubmissionResult(
        ok=result.ok,
        method=METHOD_BROWSER,
        needs_human=result.needs_human,
        error=(result.error or "")[:600],
        artifacts=list(result.artifacts),
        verification={"run": verification},
        confirmation_ref="browser-agent" if result.ok else "",
    )
