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


def discovered_specs(ctx: SubmissionContext) -> list[FieldSpec]:
    """FieldSpecs for the questions this form itself added.

    Discovery (``applications/questions.py``) stored each one's selector and
    control type on the answer entry at package time; whatever the candidate
    wrote in the portal is the value. Entries without a selector are skipped --
    we cannot fill what we cannot address, and ``unanswerable_questions`` has
    already decided whether that blank is allowed to stop the run.
    """
    kinds = {"text": "text", "textarea": "text", "select": "select",
             "checkbox": "check", "radio": "radio", "file": "file"}
    specs: list[FieldSpec] = []
    for entry in ctx.answers:
        if entry.get("kind") not in ("custom", "attestation"):
            continue
        selector = str(entry.get("selector") or "")
        if not selector:
            continue
        field_type = str(entry.get("field_type") or "text")
        if field_type == "file":
            # A custom file upload ("portfolio deck") is nothing we hold.
            continue
        specs.append(
            FieldSpec(
                key=str(entry.get("key")),
                selectors=(selector,),
                value=str(entry.get("answer") or "").strip(),
                required=bool(entry.get("form_required")),
                kind=kinds.get(field_type, "text"),
            )
        )
    return specs


def unanswerable_questions(ctx: SubmissionContext) -> list[str]:
    """Labels of required discovered questions nobody has answered.

    Checked before the browser ever opens: "a required field we cannot answer
    from the record stops the run" extends to the form's own questions. Only
    *required* blanks stop it -- an optional attestation left blank IS an
    answer (declining to self-identify), and the candidate was told where
    those questions live before they approved. What never happens is anybody
    but the candidate writing one.
    """
    labels: list[str] = []
    for entry in ctx.answers:
        if entry.get("kind") not in ("custom", "attestation"):
            continue
        if str(entry.get("answer") or "").strip():
            continue
        if entry.get("form_required"):
            labels.append(str(entry.get("question") or entry.get("key")))
    return labels


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
        unanswered = unanswerable_questions(ctx)
        if unanswered:
            # Before the browser opens: the answer is not going to appear
            # mid-run, and a page view spent discovering that is wasted.
            named = "; ".join(unanswered[:3])
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=f"the form requires answers only the candidate can give: {named}",
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
                    # The form's own questions, by the selectors discovery
                    # stored. Answered in the portal, filled here verbatim.
                    missing += driver.fill(page, discovered_specs(ctx), log=log)
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
                    # "We clicked" is not "they accepted". The first live run
                    # recorded a success whose after-screenshot was the same
                    # filled form: the click landed while the resume upload was
                    # still in flight and the vendor swallowed it. Positive
                    # evidence -- a thank-you, or the form going away -- is now
                    # what separates submitted from needs_human.
                    confirmed = driver.confirm_submission(
                        page, landmark=self.spec.landmark, submit=self.spec.submit, log=log
                    )
                    after = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label=f"{self.vendor}-{'submitted' if confirmed else 'unconfirmed'}",
                        log=log,
                    )
                    artifacts += [p for p in [after] if p]
                    if not confirmed:
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=(
                                "clicked submit but the form never confirmed -- "
                                "the screenshots say what the page did"
                            ),
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
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
