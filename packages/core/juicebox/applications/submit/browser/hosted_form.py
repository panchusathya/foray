"""Applying through a vendor's hosted form, the way a candidate does.

This is the honest path for the open market. Greenhouse, Lever and Ashby all
publish an apply *API*, and all three require a key the **employer** issues from
their own account -- there is no key that spans boards, and we will never hold
one for a company we have no relationship with. The form on the hosted board is
the only door open to a candidate, so it is the one we use.

Which is easier than it sounds: unlike Workday, these are one page. No account,
no verification mail, no wizard. Navigate, fill, submit, screenshot.

One adapter class covers all three because the only thing that differs is the
field map (``forms.py``). A vendor whose form stops matching its map ends the
run as ``needs_human`` naming the fields it could not find -- never a partial
submission in somebody's name.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from ..base import METHOD_BROWSER, SubmissionContext, SubmissionResult
from . import driver
from .driver import BrowserUnavailable, FieldSpec, RunLog
from .forms import ASHBY, GREENHOUSE, LEVER, FormSpec

logger = logging.getLogger(__name__)


def _values(ctx: SubmissionContext, resume_path: str) -> dict[str, str]:
    """Logical field name -> what we would put in it.

    Only what the record actually holds. A blank stays blank: ``why_company``
    is left empty by ``docgen/answers`` on purpose, and a browser adapter is not
    the place that decides to invent one.
    """
    return {
        "full_name": ctx.full_name,
        "first_name": ctx.first_name,
        "last_name": ctx.last_name,
        "email": ctx.from_address,
        "phone": ctx.phone,
        "linkedin": ctx.linkedin_url,
        "comments": ctx.answer("why_company"),
        "resume": resume_path,
        "cover_letter": "",
    }


def specs_for(spec: FormSpec, values: dict[str, str]) -> list[FieldSpec]:
    return [
        FieldSpec(
            key=key,
            selectors=selectors,
            value=values.get(key, ""),
            required=key in spec.required,
            kind="file" if key in spec.files else "text",
        )
        for key, selectors in spec.fields.items()
    ]


class HostedFormSubmitter:
    """Fill and send one vendor's public application form."""

    method = METHOD_BROWSER

    def __init__(self, spec: FormSpec) -> None:
        self.spec = spec
        self.vendor = spec.vendor

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        ok, reason = driver.available()
        if not ok:
            return SubmissionResult(ok=False, method=self.method, error=reason)
        if ctx.resume is None:
            return SubmissionResult(
                ok=False, method=self.method, needs_human=True, error="no resume in the package"
            )
        if not ctx.from_address:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no return address to put on the application",
            )

        log = RunLog()
        artifacts: list[str] = []
        with tempfile.TemporaryDirectory() as scratch:
            # Playwright uploads from a path; the bytes live in our store, so
            # they are staged here and gone when the run ends.
            resume_path = Path(scratch) / ctx.resume.filename
            resume_path.write_bytes(ctx.resume.content)
            values = _values(ctx, str(resume_path))

            try:
                with driver.session(log=log) as page:
                    page.goto(ctx.target.apply_url, wait_until="domcontentloaded")

                    # Most boards render the posting with the form collapsed or
                    # on a second URL. Opening it is a navigation, not a send.
                    reached = driver.reach_form(
                        page,
                        landmark=self.spec.landmark,
                        open_form=self.spec.open_form,
                        log=log,
                    )

                    if not reached:
                        shot = driver.snapshot(
                            page,
                            org_id=ctx.org_id,
                            application_id=ctx.application_id,
                            label=f"{self.vendor}-no-form",
                            log=log,
                        )
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=(
                                "no application form on this page -- the posting may be "
                                "closed, or the board has changed shape (run ats-probe)"
                            ),
                            artifacts=[p for p in [shot] if p],
                            verification={"run": log.as_payload()},
                        )

                    missing = driver.fill(page, specs_for(self.spec, values), log=log)
                    before = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label=f"{self.vendor}-before-submit",
                        log=log,
                    )
                    artifacts = [p for p in [before] if p]
                    if missing:
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=f"required fields we could not fill: {', '.join(missing)}",
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
                    if not driver.click(page, self.spec.submit, log=log, what="submit"):
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error="the form has no submit control we recognise",
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
                    # The confirmation screen is the only evidence we have that
                    # the employer accepted it, so it is captured before the
                    # browser closes and the attempt is recorded.
                    page.wait_for_timeout(3000)
                    after = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label=f"{self.vendor}-submitted",
                        log=log,
                    )
                    artifacts += [p for p in [after] if p]
            except BrowserUnavailable as exc:
                return SubmissionResult(ok=False, method=self.method, error=str(exc))

        return SubmissionResult(
            ok=True,
            method=self.method,
            confirmation_ref=f"{self.vendor} {ctx.target.tenant}, {ctx.target.external_job_id}",
            artifacts=artifacts,
            verification={"run": log.as_payload()},
        )


class GreenhouseFormSubmitter(HostedFormSubmitter):
    def __init__(self) -> None:
        super().__init__(GREENHOUSE)


class LeverFormSubmitter(HostedFormSubmitter):
    def __init__(self) -> None:
        super().__init__(LEVER)


class AshbyFormSubmitter(HostedFormSubmitter):
    def __init__(self) -> None:
        super().__init__(ASHBY)
