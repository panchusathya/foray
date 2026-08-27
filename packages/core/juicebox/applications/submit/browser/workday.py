"""Workday, driven through its own form.

Workday is the common case and the hardest one: you cannot apply without an
account, the account needs an email you can read, and the form is a wizard
whose steps a tenant configures. So the run has two legs.

**Leg one** creates the account and stops. Workday emails a verification link
to the address we registered, which is the candidate's alias, which is a
mailbox we own -- that is the whole reason the alias exists. The attempt parks
as ``needs_verification`` and the run's state goes on the attempt row.

**Leg two** starts when ``applications/inbox.py`` sees that mail, clicks the
link, and queues ``application.submit.verify``. It signs in, walks the wizard,
fills what the field map knows, and submits.

**A tenant we already registered for skips leg one entirely.** The service
hands the stored login over on the context, and the run goes straight to leg
two -- there is no second account, so there is no second verification mail to
wait for, and parking on one that will never arrive is how an application sat
in ``submitting`` until it aged out.

**What it will not do.** Every required field it cannot find, or cannot answer
from the package, ends the run as ``needs_human`` with the list. Workday
tenants add questions we have never seen; guessing at one is how you put a
sentence somebody did not write in front of an employer. The field map is
deliberately boring, and growing it is a code change with a reviewer.
"""

from __future__ import annotations

import logging
import secrets
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from ....models import AtsAccount
from ..base import METHOD_BROWSER, SubmissionContext, SubmissionResult
from . import driver
from .driver import BrowserUnavailable, FieldSpec, RunLog
from .forms import WORKDAY_ACCOUNT, WORKDAY_APPLICATION

logger = logging.getLogger(__name__)

#: Workday's own words, across the generations of the form a tenant may run.
_APPLY_BUTTONS = (
    'a[data-automation-id="adventureButton"]',
    'button[data-automation-id="applyManually"]',
    'a:has-text("Apply")',
    'button:has-text("Apply")',
)
_CREATE_ACCOUNT = (
    'button[data-automation-id="createAccountLink"]',
    'a:has-text("Create Account")',
)
_SIGN_IN = (
    'button[data-automation-id="signInSubmitButton"]',
    'button:has-text("Sign In")',
)
_SUBMIT = (
    'button[data-automation-id="bottom-navigation-next-button"]',
    'button[data-automation-id="wd-CommandButton_uic_okButton"]',
    'button:has-text("Submit")',
)


def _account_fields(email: str, password: str) -> list[FieldSpec]:
    return [
        FieldSpec(
            key="email",
            selectors=('input[data-automation-id="email"]', 'input[type="email"]'),
            value=email,
            required=True,
        ),
        FieldSpec(
            key="password",
            selectors=('input[data-automation-id="password"]',),
            value=password,
            required=True,
            secret=True,
        ),
        FieldSpec(
            key="verify_password",
            selectors=('input[data-automation-id="verifyPassword"]',),
            value=password,
            secret=True,
        ),
        FieldSpec(
            key="terms",
            selectors=('input[data-automation-id="createAccountCheckbox"]',),
            value="1",
            kind="check",
        ),
    ]


def _application_fields(ctx: SubmissionContext, resume_path: str) -> list[FieldSpec]:
    return [
        FieldSpec(
            key="first_name",
            selectors=('input[data-automation-id="legalNameSection_firstName"]',),
            value=ctx.first_name,
            required=True,
        ),
        FieldSpec(
            key="last_name",
            selectors=('input[data-automation-id="legalNameSection_lastName"]',),
            value=ctx.last_name,
            required=True,
        ),
        FieldSpec(
            key="phone",
            selectors=('input[data-automation-id="phone-number"]',),
            value=ctx.phone,
        ),
        FieldSpec(
            key="resume",
            selectors=(
                'input[data-automation-id="file-upload-input-ref"]',
                'input[type="file"]',
            ),
            value=resume_path,
            required=True,
            kind="file",
        ),
        FieldSpec(
            key="linkedin",
            selectors=('input[data-automation-id="linkedinQuestion"]',),
            value=ctx.linkedin_url,
        ),
    ]


class WorkdaySubmitter:
    """Two-leg Workday run. Parks on the verification mail in between."""

    vendor = "workday"
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
                    "workday needs a mailbox we can read: no alias is issued for "
                    "this candidate (APPLY_ALIAS_ADDRESS unset?)"
                ),
            )

        # Second role at a tenant we already registered for. No new account is
        # being made, so no verification mail is coming; parking on one would
        # leave the pursuit in `submitting` until it aged out. Leg two is the
        # whole of the work from here.
        if ctx.portal_password:
            return self.resume(
                ctx,
                {
                    "username": ctx.portal_username or ctx.from_address,
                    "password": ctx.portal_password,
                },
            )

        password = secrets.token_urlsafe(18)
        log = RunLog()
        try:
            with driver.session(log=log) as page:
                page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                driver.click(page, _APPLY_BUTTONS, log=log, what="apply")
                driver.click(page, _CREATE_ACCOUNT, log=log, what="create account")
                missing = driver.fill(page, _account_fields(ctx.from_address, password), log=log)
                shot = driver.snapshot(
                    page,
                    org_id=ctx.org_id,
                    application_id=ctx.application_id,
                    label="workday-account",
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
                # Nothing missing is not evidence we are registering. The
                # sign-in page carries an email box and a password box too, so
                # a run that never reached the Create Account form fills both,
                # reads as complete, and used to report an account created --
                # after typing a brand-new password into a sign-in form. The
                # confirm-password box and the terms checkbox exist only on the
                # registration form, so one of them is the evidence.
                if not {"verify_password", "terms"} & set(log.filled):
                    return SubmissionResult(
                        ok=False,
                        method=self.method,
                        needs_human=True,
                        error=(
                            f"workday {ctx.target.tenant} showed a sign-in form, not a "
                            "registration form, and we hold no account for this tenant"
                        ),
                        artifacts=artifacts,
                        verification=log.as_payload(),
                    )
                if not driver.click(page, _SIGN_IN, log=log, what="submit account"):
                    return SubmissionResult(
                        ok=False,
                        method=self.method,
                        needs_human=True,
                        error="the registration form has no submit control we recognise",
                        artifacts=artifacts,
                        verification=log.as_payload(),
                    )
        except BrowserUnavailable as exc:
            return SubmissionResult(ok=False, method=self.method, error=str(exc))

        # Parked: Workday has sent a verification mail to the alias. The
        # password rides along on the attempt row -- it is ours, generated for
        # this tenant, and leg two cannot sign in without it.
        return SubmissionResult(
            ok=False,
            method=self.method,
            needs_verification=True,
            confirmation_ref=f"workday {ctx.target.tenant}: awaiting email verification",
            artifacts=list(log.artifacts),
            verification={
                "stage": "account_created",
                "username": ctx.from_address,
                "password": password,
                "tenant": ctx.target.tenant,
                "run": log.as_payload(),
            },
        )

    def resume(self, ctx: SubmissionContext, verification: dict) -> SubmissionResult:
        """Leg two: sign in with the account we made and walk the wizard."""
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
                error="no stored credential for this workday account; cannot resume",
            )
        if ctx.resume is None:
            return SubmissionResult(
                ok=False, method=self.method, needs_human=True, error="no resume in the package"
            )

        log = RunLog()
        artifacts: list[str] = []
        with tempfile.TemporaryDirectory() as scratch:
            # Playwright uploads from a path; the bytes live in our store, so
            # they are staged here and gone when the run ends.
            resume_path = Path(scratch) / ctx.resume.filename
            resume_path.write_bytes(ctx.resume.content)
            try:
                with driver.session(log=log) as page:
                    page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                    driver.fill(
                        page,
                        [
                            FieldSpec(
                                key="email",
                                selectors=('input[data-automation-id="email"]',),
                                value=username,
                                required=True,
                            ),
                            FieldSpec(
                                key="password",
                                selectors=('input[data-automation-id="password"]',),
                                value=password,
                                required=True,
                                secret=True,
                            ),
                        ],
                        log=log,
                    )
                    driver.click(page, _SIGN_IN, log=log, what="sign in")

                    # Some tenants verify by emailing a code rather than a link.
                    # `applications/inbox.py` parked it on the attempt; this is
                    # the only place it is any use. Not required: a tenant that
                    # verified by link shows no code field at all.
                    otp = str(verification.get("otp") or "")
                    if otp:
                        driver.fill(
                            page,
                            [
                                FieldSpec(
                                    key="otp",
                                    selectors=WORKDAY_ACCOUNT.selectors_for("otp"),
                                    value=otp,
                                )
                            ],
                            log=log,
                        )
                        driver.click(page, _SIGN_IN, log=log, what="submit code")

                    missing = driver.fill(page, _application_fields(ctx, str(resume_path)), log=log)
                    before = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label="workday-before-submit",
                        log=log,
                    )
                    artifacts = [path for path in [before] if path]
                    if missing:
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=f"required fields we could not fill: {', '.join(missing)}",
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
                    if not driver.click(page, _SUBMIT, log=log, what="submit"):
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error="the form has no submit control we recognise",
                            artifacts=artifacts,
                            verification={"run": log.as_payload()},
                        )
                    # Same gate hosted_form uses: a click during an in-flight
                    # upload is swallowed, and the after-screenshot of the same
                    # filled form used to be recorded as submitted.
                    confirmed = driver.confirm_submission(
                        page,
                        landmark=WORKDAY_APPLICATION.landmark,
                        submit=_SUBMIT,
                        log=log,
                    )
                    after = driver.snapshot(
                        page,
                        org_id=ctx.org_id,
                        application_id=ctx.application_id,
                        label=f"workday-{'submitted' if confirmed else 'unconfirmed'}",
                        log=log,
                    )
                    artifacts += [path for path in [after] if path]
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
            confirmation_ref=f"workday {ctx.target.tenant}, {ctx.target.external_job_id}",
            artifacts=artifacts,
            verification={"run": log.as_payload()},
        )


def stored_account(
    db: Session, *, candidate_id: str, vendor: str, tenant: str
) -> tuple[str, str]:
    """``(username, password)`` for a portal account we already hold, or blanks.

    The write half has existed since the beginning and nothing ever read it,
    which is why the second role at one Workday tenant landed on the sign-in
    page with a freshly minted password and reported an account created.

    A blob that no longer decrypts reads as no account, same as
    ``submission._unseal``: registering again is recoverable, signing in with a
    credential we cannot read is not.
    """
    if not vendor:
        return "", ""
    row = (
        db.query(AtsAccount)
        .filter(
            AtsAccount.candidate_id == candidate_id,
            AtsAccount.vendor == vendor,
            AtsAccount.tenant == tenant,
        )
        .one_or_none()
    )
    if row is None or not row.token_blob:
        return "", ""
    if row.handed_over_at is not None:
        # The candidate took this account over (portal handover): whatever
        # password they set through the board's own reset is theirs, and the
        # one we generated is dead. Signing in with it would either fail or,
        # worse, work against their expectation of sole control.
        return "", ""
    from ....integrations.crypto import decrypt_json

    try:
        password = str(decrypt_json(row.token_blob).get("password") or "")
    except ValueError as exc:
        logger.warning("stored portal account %s could not be read: %s", row.id, exc)
        return "", ""
    return (row.username or ""), password


def remember_account(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    vendor: str,
    tenant: str,
    username: str,
    password: str,
) -> AtsAccount:
    """Store the portal login so the next role at this employer reuses it.

    The password is encrypted with the same helper that protects mailbox OAuth
    tokens; nothing here should ever be readable from a database dump.
    """
    from ....integrations.crypto import encrypt_json

    existing = (
        db.query(AtsAccount)
        .filter(
            AtsAccount.candidate_id == candidate_id,
            AtsAccount.vendor == vendor,
            AtsAccount.tenant == tenant,
        )
        .one_or_none()
    )
    if existing is None:
        existing = AtsAccount(
            org_id=org_id,
            candidate_id=candidate_id,
            vendor=vendor,
            tenant=tenant,
        )
        db.add(existing)
    existing.username = username
    existing.token_blob = encrypt_json({"password": password})
    db.flush()
    return existing
