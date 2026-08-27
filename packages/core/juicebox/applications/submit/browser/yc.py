"""Work at a Startup, driven the way a candidate uses it.

YC postings (ycombinator.com/companies/*/jobs/*, workatastartup.com/jobs/*)
all apply through one door: a Work at a Startup account on the unified YC
login. That makes this the second two-leg vendor after Workday, and the run
borrows its whole shape from ``browser/workday.py``:

**Leg one** registers the account and stops. YC mails a verification link to
the address we registered -- the candidate's alias, a mailbox we own -- and the
attempt parks as ``needs_verification`` with the credential on the attempt row.
``applications/inbox.py`` clicks the link when it lands and queues
``application.submit.verify``.

**Leg two** signs in and applies. A WaaS application is not a form wizard: the
job page's Apply control opens a note box ("tell them why you're a good fit")
and Send files it with the WaaS profile. The note carries the reviewed
``why_company`` draft verbatim -- drafting happens in ``docgen/answers`` with
the candidate's eyes on it, never here.

**One account spans every YC company.** The stored ``AtsAccount`` row is keyed
on the fixed tenant ``ACCOUNT_TENANT`` rather than the posting's company slug
-- scoped per company, the second YC role would register a fresh account and
park on a verification mail that reads as a duplicate signup.

**What it will not do.** Selectors here are unprobed starting points (see
``forms.py``'s doctrine); a control that stops matching ends the run as
``needs_human`` naming what it could not find, never a half-made application.
It also does not build out the WaaS *profile* (resume upload, work history)
in this version -- the send carries the account and the note, and a board
that requires a completed profile will refuse in front of the screenshots.
"""

from __future__ import annotations

import logging
import re
import secrets
import tempfile

from ..base import METHOD_BROWSER, SubmissionContext, SubmissionResult
from . import driver
from .driver import BrowserUnavailable, FieldSpec, RunLog
from .forms import YC_ACCOUNT, YC_APPLICATION

logger = logging.getLogger(__name__)

#: The one tenant every YC posting shares -- the login lives at
#: workatastartup.com, not at the employer. ``submission._account_tenant``
#: maps a YC target onto this before reading or writing ``AtsAccount``.
ACCOUNT_TENANT = "workatastartup"

_SIGN_IN = (
    "button[type='submit']",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
)


def _mint_username(ctx: SubmissionContext) -> str:
    """A YC username for the account we are about to register.

    The sign-up form requires one (live probe 2026-08-27); it is ours the
    same way the password is, so it is minted here and carried on the
    verification payload for leg two's sign-in.
    """
    stem = re.sub(r"[^a-z0-9]", "", f"{ctx.first_name}{ctx.last_name}".lower())
    return f"{(stem or 'candidate')[:16]}{secrets.token_hex(3)}"


def _account_fields(
    ctx: SubmissionContext, password: str, username: str
) -> list[FieldSpec]:
    return [
        FieldSpec(
            key="first_name",
            selectors=YC_ACCOUNT.selectors_for("first_name"),
            value=ctx.first_name,
        ),
        FieldSpec(
            key="last_name",
            selectors=YC_ACCOUNT.selectors_for("last_name"),
            value=ctx.last_name,
        ),
        FieldSpec(
            key="email",
            selectors=YC_ACCOUNT.selectors_for("email"),
            value=ctx.from_address,
            required=True,
        ),
        FieldSpec(
            key="username",
            selectors=YC_ACCOUNT.selectors_for("username"),
            value=username,
            required=True,
        ),
        FieldSpec(
            key="password",
            selectors=YC_ACCOUNT.selectors_for("password"),
            value=password,
            required=True,
            secret=True,
        ),
    ]


class YCSubmitter:
    """Two-leg Work at a Startup run. Parks on the verification mail between."""

    vendor = "yc"
    method = METHOD_BROWSER

    def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        ok, reason = driver.available()
        if not ok:
            return SubmissionResult(ok=False, method=self.method, error=reason)
        if not ctx.from_address:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error=(
                    "a YC account needs a mailbox we can read: no alias is issued "
                    "for this candidate (APPLY_ALIAS_ADDRESS unset?)"
                ),
            )

        # A verified account we already hold skips registration entirely -- no
        # new signup, no verification mail, so leg two is the whole of it.
        if ctx.portal_password and ctx.portal_account_verified:
            return self.resume(
                ctx,
                {
                    "username": ctx.portal_username or ctx.from_address,
                    "password": ctx.portal_password,
                },
            )

        # Model B2: register with the candidate's vault-chosen password. The
        # gate prompts the vault and holds, so no stored password means a path
        # that skipped it -- refuse rather than self-register in their name.
        if not ctx.portal_password:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no vault credential for workatastartup; awaiting the candidate's password",
            )
        password = ctx.portal_password
        username = ctx.portal_username or _mint_username(ctx)
        log = RunLog()
        try:
            with driver.session(log=log) as page:
                page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                # Logged out, Apply routes through the YC login; the sign-up
                # link is on that wall. Both clicks are navigation, not sends.
                driver.wait_for_any(page, YC_APPLICATION.open_form, timeout_ms=30000)
                driver.click(page, YC_APPLICATION.open_form, log=log, what="apply")
                driver.click(page, YC_ACCOUNT.open_form, log=log, what="sign up")
                # account.ycombinator.com is a SPA: domcontentloaded fires on
                # its shell, and the first live run filled nothing but the
                # (late-rendering) password box. Wait for the form's stable
                # ids before reading anything as absent.
                driver.wait_for_any(
                    page,
                    YC_ACCOUNT.selectors_for("username") + YC_ACCOUNT.selectors_for("email"),
                    timeout_ms=30000,
                )
                missing = driver.fill(page, _account_fields(ctx, password, username), log=log)
                shot = driver.snapshot(
                    page,
                    org_id=ctx.org_id,
                    application_id=ctx.application_id,
                    label="yc-account",
                    log=log,
                )
                artifacts = [path for path in [shot] if path]
                if missing:
                    return SubmissionResult(
                        ok=False,
                        method=self.method,
                        needs_human=True,
                        error=f"could not fill required account fields: {', '.join(missing)}",
                        artifacts=artifacts,
                        verification=log.as_payload(),
                    )
                # Same evidence rule as Workday: the sign-in form also has an
                # email box and a password box, so filling both is not proof
                # we are registering. The name fields exist only on sign-up,
                # so one of them is the evidence -- without it we just typed a
                # fresh password into a login form for an account we do not
                # hold.
                if not {"first_name", "last_name"} & set(log.filled):
                    return SubmissionResult(
                        ok=False,
                        method=self.method,
                        needs_human=True,
                        error=(
                            "workatastartup showed a sign-in form, not a sign-up "
                            "form, and we hold no account for this candidate"
                        ),
                        artifacts=artifacts,
                        verification=log.as_payload(),
                    )
                if not driver.click(page, YC_ACCOUNT.submit, log=log, what="submit account"):
                    return SubmissionResult(
                        ok=False,
                        method=self.method,
                        needs_human=True,
                        error="the sign-up form has no submit control we recognise",
                        artifacts=artifacts,
                        verification=log.as_payload(),
                    )
        except BrowserUnavailable as exc:
            return SubmissionResult(ok=False, method=self.method, error=str(exc))

        # Parked: YC has mailed a verification link to the alias. The password
        # rides on the attempt row -- ours, minted for this account, and leg
        # two cannot sign in without it.
        return SubmissionResult(
            ok=False,
            method=self.method,
            needs_verification=True,
            confirmation_ref="workatastartup: awaiting email verification",
            artifacts=list(log.artifacts),
            verification={
                "stage": "account_created",
                # The minted YC username, not the alias address: the login
                # form's identifier field is name='username' (#ycid-input),
                # and it is what leg two signs in with.
                "username": username,
                "email": ctx.from_address,
                "password": password,
                "tenant": ACCOUNT_TENANT,
                "run": log.as_payload(),
            },
        )

    def resume(self, ctx: SubmissionContext, verification: dict) -> SubmissionResult:
        """Leg two: sign in and file the application with the note."""
        ok, reason = driver.available()
        if not ok:
            return SubmissionResult(ok=False, method=self.method, error=reason)
        password = str(verification.get("password") or "")
        username = str(verification.get("username") or ctx.from_address)
        if not password:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no stored credential for this YC account; cannot resume",
            )

        log = RunLog()
        artifacts: list[str] = []
        # Scratch dir for parity with the other adapters; WaaS applies with
        # the profile rather than a per-application upload, so nothing is
        # staged today. The context manager stays so adding a profile-resume
        # step later does not re-plumb the run.
        with tempfile.TemporaryDirectory() as scratch:
            del scratch
            try:
                with driver.session(log=log) as page:
                    page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                    driver.click(page, YC_APPLICATION.open_form, log=log, what="apply")
                    # A logged-out session lands on the login wall here; a
                    # still-live session skips straight to the note. fill()
                    # skipping absent fields is what makes both paths one code
                    # path.
                    driver.fill(
                        page,
                        [
                            FieldSpec(
                                key="username",
                                # The login wall's identifier is #ycid-input
                                # (name='username'); the email selectors stay
                                # as the fallback for any older form shape.
                                selectors=YC_ACCOUNT.selectors_for("username")
                                + YC_ACCOUNT.selectors_for("email"),
                                value=username,
                            ),
                            FieldSpec(
                                key="password",
                                selectors=YC_ACCOUNT.selectors_for("password"),
                                value=password,
                                secret=True,
                            ),
                        ],
                        log=log,
                    )
                    if {"username", "email", "password"} & set(log.filled):
                        driver.click(page, _SIGN_IN, log=log, what="sign in")
                        # Signing in navigates away from the wall; the apply
                        # control has to be reached again on the job page.
                        page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                        driver.click(page, YC_APPLICATION.open_form, log=log, what="apply")

                    reached = driver.reach_form(
                        page,
                        landmark=YC_APPLICATION.landmark,
                        open_form=YC_APPLICATION.open_form,
                        log=log,
                    )
                    if not reached:
                        shot = driver.snapshot(
                            page,
                            org_id=ctx.org_id,
                            application_id=ctx.application_id,
                            label="yc-no-form",
                            log=log,
                        )
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=(
                                "no application note box on this page -- the posting "
                                "may be closed, or the board has changed shape "
                                "(run ats-probe)"
                            ),
                            artifacts=[p for p in [shot] if p],
                            verification={"run": log.as_payload()},
                        )

                    driver.fill(
                        page,
                        [
                            FieldSpec(
                                key="comments",
                                selectors=YC_APPLICATION.selectors_for("comments"),
                                value=ctx.answer("why_company"),
                            )
                        ],
                        log=log,
                    )
                    before = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label="yc-before-submit",
                        log=log,
                    )
                    artifacts = [p for p in [before] if p]
                    if not driver.click(page, YC_APPLICATION.submit, log=log, what="submit"):
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error="the application has no send control we recognise",
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
                    confirmed = driver.confirm_submission(
                        page,
                        landmark=YC_APPLICATION.landmark,
                        submit=YC_APPLICATION.submit,
                        log=log,
                    )
                    after = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label=f"yc-{'submitted' if confirmed else 'unconfirmed'}",
                        log=log,
                    )
                    artifacts += [p for p in [after] if p]
                    if not confirmed:
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=(
                                "clicked send but the page never confirmed -- "
                                "the screenshots say what it did"
                            ),
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
            except BrowserUnavailable as exc:
                return SubmissionResult(ok=False, method=self.method, error=str(exc))

        return SubmissionResult(
            ok=True,
            method=self.method,
            confirmation_ref=f"workatastartup {ctx.target.tenant}, {ctx.target.external_job_id}",
            artifacts=artifacts,
            verification={"run": log.as_payload()},
        )
