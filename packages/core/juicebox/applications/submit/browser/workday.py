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
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from ....models import AtsAccount
from ..base import METHOD_BROWSER, SubmissionContext, SubmissionResult
from . import driver
from .driver import BrowserUnavailable, FieldSpec, RunLog
from .forms import WORKDAY_ACCOUNT

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
#: The "Start Your Application" chooser that Apply opens on most tenants —
#: Autofill with Resume / Apply Manually / Use My Last Application / LinkedIn.
#: Manual is our route: the field map is the source of truth, not a resume
#: parse the tenant runs. (Live screenshot, Intapp 2026-08-27: the run
#: clicked Apply, this chooser opened, and "create account" matched nothing.)
_APPLY_MANUALLY = (
    'button[data-automation-id="applyManually"]',
    'a[data-automation-id="applyManually"]',
    'button:has-text("Apply Manually")',
    'a:has-text("Apply Manually")',
)
#: The federated sign-on step some tenants run before any account form —
#: Google / LinkedIn / "Sign in with email" (live screenshot, Intapp
#: 2026-08-27). Email is our route: the address is the application's alias,
#: and OAuth in the candidate's name is not ours to click. The email/password
#: inputs (and the Create Account link) render only after this.
_EMAIL_ROUTE = (
    # Workday ships this id capitalized (SignInWithEmailButton, live DOM dump,
    # Intapp 2026-08-27); the lowercase variant stays for tenants that differ.
    'button[data-automation-id="SignInWithEmailButton"]',
    'button[data-automation-id="signInWithEmailButton"]',
    'button:has-text("Sign in with email")',
    'a:has-text("Sign in with email")',
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

#: Workday's consent overlay, which paints over the first render and can
#: swallow the Apply click.
_COOKIE_BUTTONS = (
    'button[data-automation-id="legalNoticeAcceptButton"]',
    'button:has-text("Accept Cookies")',
)

#: The registration/sign-in form's own fields, for waiting on route changes.
_ACCOUNT_EMAIL = ('input[data-automation-id="email"]', 'input[type="email"]')

#: A posting we already have a draft on offers "Continue Application" where an
#: untouched one offers "Apply" (live, Intapp 2026-08-27 — Candidate Home
#: showed Active (1)). Every retry after the first partial fill lands here, so
#: without this the run clicks a button that is not on the page any more.
_CONTINUE_APPLICATION = (
    'a:has-text("Continue Application")',
    'button:has-text("Continue Application")',
    'a[data-automation-id="adventureButton"]:has-text("Continue")',
)

#: Controls that exist only inside the signed-in wizard. Presence of any is
#: the confirmation that sign-in landed; a generic input is not (the login
#: page has those too).
_WIZARD_MARKERS = (
    '[data-automation-id="applyFlowPage"]',
    'button[data-automation-id="pageFooterNextButton"]',
    '[data-automation-id="progressBar"]',
    'button[data-automation-id="bottom-navigation-next-button"]',
    'input[data-automation-id="legalNameSection_firstName"]',
    'input[data-automation-id="file-upload-input-ref"]',
    '[data-automation-id="fileUploadDropZone"]',
)


def _settle(page, *, log: RunLog, expect: tuple[str, ...], timeout_ms: int = 45000) -> str:
    """Wait for the SPA to actually render, then clear the cookie overlay.

    ``domcontentloaded`` fires on Workday's empty shell; the page itself
    arrives seconds later — measured at ~9s through a residential proxy — and
    the first live run clicked into the blank, filled nothing, and
    screenshotted a white page. Every navigation or client-side route change
    settles here before anything is clicked or filled.
    """
    hit = driver.wait_for_any(page, expect, timeout_ms=timeout_ms)
    if driver.click(page, _COOKIE_BUTTONS, log=log, what="cookie banner"):
        page.wait_for_timeout(300)
    return hit


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
            kind="password",
            secret=True,
        ),
        FieldSpec(
            key="verify_password",
            selectors=('input[data-automation-id="verifyPassword"]',),
            value=password,
            kind="password",
            secret=True,
        ),
        FieldSpec(
            key="terms",
            selectors=('input[data-automation-id="createAccountCheckbox"]',),
            value="1",
            kind="check",
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

        # A verified account we already hold: no new account, no verification
        # mail, so leg two (sign in and apply) is the whole of the work.
        if ctx.portal_password and ctx.portal_account_verified:
            return self.resume(
                ctx,
                {
                    "username": ctx.portal_username or ctx.from_address,
                    "password": ctx.portal_password,
                },
            )

        # Model B2: register with the candidate's vault-chosen password, never
        # a minted one. The gate in `auto_submit` prompts the vault and holds
        # the pursuit, so a run with no stored password is a path that skipped
        # it — refuse rather than self-register in the candidate's name.
        if not ctx.portal_password:
            return SubmissionResult(
                ok=False,
                method=self.method,
                needs_human=True,
                error="no vault credential for this workday tenant; awaiting the candidate's password",
            )
        password = ctx.portal_password
        log = RunLog()
        try:
            with driver.session(log=log) as page:
                page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                _settle(page, log=log, expect=_APPLY_BUTTONS)
                driver.click(page, _APPLY_BUTTONS, log=log, what="apply")
                # Apply opens the route chooser on most tenants, a sign-in
                # dialog on some, and the registration form on a few — wait
                # for whichever shape arrives, take the manual route when the
                # chooser is what showed, and wait again for the dialog.
                _settle(
                    page,
                    log=log,
                    expect=_APPLY_MANUALLY + _EMAIL_ROUTE + _CREATE_ACCOUNT + _ACCOUNT_EMAIL,
                    timeout_ms=30000,
                )
                if driver.click(page, _APPLY_MANUALLY, log=log, what="apply manually"):
                    _settle(
                        page,
                        log=log,
                        expect=_EMAIL_ROUTE + _CREATE_ACCOUNT + _ACCOUNT_EMAIL,
                        timeout_ms=30000,
                    )
                if driver.click(page, _EMAIL_ROUTE, log=log, what="sign in with email"):
                    _settle(page, log=log, expect=_CREATE_ACCOUNT + _ACCOUNT_EMAIL, timeout_ms=30000)
                driver.click(page, _CREATE_ACCOUNT, log=log, what="create account")
                driver.wait_for_any(page, _ACCOUNT_EMAIL, timeout_ms=20000)
                # The account is registered under the candidate's own email
                # (ctx.portal_username, set from the vault credential) so the
                # verification link reaches them -- not the alias, whose inbox
                # we cannot read. The alias still fronts the application.
                account_email = ctx.portal_username or ctx.from_address
                missing = driver.fill(page, _account_fields(account_email, password), log=log)
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

                # "Clicked submit" is not "account created". A blank/invalid
                # password submitted silently, the form stayed put, and the
                # run still parked needs_verification for a mail that was never
                # sent (live, Intapp 2026-08-27). Registration succeeds only
                # when the account form leaves the screen -- the email box is
                # gone, or a wizard step / check-your-mail notice replaced it.
                # Still on it means Workday rejected the form.
                page.wait_for_timeout(3000)
                after = driver.snapshot(
                    page,
                    org_id=ctx.org_id,
                    application_id=ctx.application_id,
                    label="workday-registered",
                    log=log,
                )
                artifacts += [p for p in [after] if p]
                still_on_form = driver.wait_for_any(
                    page, ('input[data-automation-id="password"]',), timeout_ms=1500
                )
                if still_on_form:
                    return SubmissionResult(
                        ok=False,
                        method=self.method,
                        needs_human=True,
                        error=(
                            "workday rejected the account form (still showing after submit) -- "
                            "likely a password that fails the tenant's complexity rule"
                        ),
                        artifacts=artifacts,
                        verification=log.as_payload(),
                    )
        except BrowserUnavailable as exc:
            return SubmissionResult(ok=False, method=self.method, error=str(exc))

        # Parked: Workday emailed a verification link to the candidate's own
        # inbox (the account email), which they click. The password rides the
        # attempt row so leg two can sign in once they have.
        return SubmissionResult(
            ok=False,
            method=self.method,
            needs_verification=True,
            confirmation_ref=f"workday {ctx.target.tenant}: awaiting email verification",
            artifacts=list(log.artifacts),
            verification={
                "stage": "account_created",
                "username": ctx.portal_username or ctx.from_address,
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
        username = str(verification.get("username") or ctx.portal_username or ctx.from_address)
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
                    _settle(
                        page,
                        log=log,
                        expect=_CONTINUE_APPLICATION + _APPLY_BUTTONS + _ACCOUNT_EMAIL,
                    )
                    # A warm profile may arrive already signed in, where the
                    # posting offers Continue Application instead of Apply.
                    if not driver.click(
                        page, _CONTINUE_APPLICATION, log=log, what="continue application"
                    ):
                        driver.click(page, _APPLY_BUTTONS, log=log, what="apply")
                    _settle(
                        page,
                        log=log,
                        expect=_APPLY_MANUALLY + _ACCOUNT_EMAIL + _WIZARD_MARKERS,
                        timeout_ms=30000,
                    )
                    # Already inside the wizard (a warm profile whose session is
                    # still good, or a Continue Application that went straight
                    # in): there is no credential to type, so signing in again
                    # is not just wasted -- the sign-in block would find no
                    # email box and fail a run that is already authenticated.
                    already_in = driver.wait_for_visible(page, _WIZARD_MARKERS, timeout_ms=0)
                    if already_in:
                        log.step("already signed in; wizard is open")
                    if not already_in and driver.click(
                        page, _APPLY_MANUALLY, log=log, what="apply manually"
                    ):
                        _settle(page, log=log, expect=_EMAIL_ROUTE + _ACCOUNT_EMAIL, timeout_ms=30000)
                    missing: list[str] = []
                    if not already_in:
                        # wait_ms: the settle above returns on PRESENCE, and
                        # Workday paints the chooser a beat after it renders the
                        # nodes -- an instant click missed the visible-only check
                        # and the whole sign-in fell through to clicking the
                        # header's Sign In (live, Intapp 2026-08-27).
                        driver.click(
                            page, _EMAIL_ROUTE, log=log, what="sign in with email", wait_ms=15000
                        )
                        driver.wait_for_visible(page, _ACCOUNT_EMAIL, timeout_ms=30000)
                        missing = driver.fill(
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
                    if missing:
                        # Without both boxes there is nothing to sign in WITH,
                        # and the text-fallback Sign In click lands on the
                        # header's own Sign In link -- the run then hands the
                        # agent an unauthenticated page it rightly refuses
                        # (live, Intapp 2026-08-27). Stop here, with the miss.
                        shot = driver.snapshot(
                            page,
                            org_id=ctx.org_id,
                            application_id=ctx.application_id,
                            label="workday-no-signin-form",
                            log=log,
                        )
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=(
                                "sign-in form never rendered: missing "
                                + ", ".join(missing)
                            ),
                            artifacts=[p for p in [shot] if p],
                            verification={"run": log.as_payload()},
                        )
                    if not already_in:
                        driver.click(page, _SIGN_IN, log=log, what="sign in")
                        # Sign-in is a navigation. Wait for the password field to
                        # actually leave before handing off, or the agent observes
                        # the still-present login form and refuses it.
                        try:
                            page.locator('input[data-automation-id="password"]').first.wait_for(
                                state="detached", timeout=30000
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.step(
                                f"password field still present after sign in: {str(exc)[:80]}"
                            )

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

                    # Signed in. Workday's application is a multi-step wizard
                    # (My Information -> My Experience -> Questions -> Voluntary
                    # Disclosures -> Self Identify -> Review -> Submit), each
                    # step its own form behind a Save-and-Continue. Walking
                    # multi-step forms is the agent's job -- only the account
                    # sign-in above had to be deterministic, because an LLM
                    # must never type a credential. Hand it the authenticated
                    # page; the agent reads the approved answers off ctx.
                    from .agent import loop as agent_loop
                    from .agent import policy as agent_policy
                    from .agent.loop import LoopResult

                    # Wait for a WIZARD marker, never a generic input: the
                    # login page has its own email/password inputs, so a
                    # generic "input" match fired the handoff before sign-in
                    # navigated and the agent saw a login page and refused it
                    # (live, 2026-08-27). These controls exist only past the
                    # sign-in, so their presence is the confirmation of it.
                    # VISIBLE, not merely present: the "Something went wrong"
                    # shell still carries hidden wizard scaffolding from the
                    # half-rendered SPA, so a presence check passed, this
                    # recovery never ran, and the agent got the broken page
                    # anyway (live, Intapp 2026-08-27).
                    wizard = driver.wait_for_visible(page, _WIZARD_MARKERS, timeout_ms=45000)
                    if not wizard:
                        # Authenticating can land on an error shell still titled
                        # "Sign In" ("Something went wrong", no controls), and a
                        # posting we already drafted offers Continue Application
                        # rather than Apply. Reloading the posting as a signed-in
                        # user clears both (live, Intapp 2026-08-27). Deterministic
                        # recovery, so the agent never sees the broken shell.
                        log.step("no wizard after sign in; reloading the posting")
                        page.goto(ctx.target.apply_url, wait_until="domcontentloaded")
                        _settle(
                            page,
                            log=log,
                            expect=_CONTINUE_APPLICATION + _APPLY_BUTTONS,
                            timeout_ms=30000,
                        )
                        if not driver.click(
                            page,
                            _CONTINUE_APPLICATION,
                            log=log,
                            what="continue application",
                            wait_ms=15000,
                        ):
                            driver.click(
                                page, _APPLY_BUTTONS, log=log, what="apply", wait_ms=10000
                            )
                            driver.click(
                                page,
                                _APPLY_MANUALLY,
                                log=log,
                                what="apply manually",
                                wait_ms=15000,
                            )
                        wizard = driver.wait_for_visible(page, _WIZARD_MARKERS, timeout_ms=45000)
                    if not wizard:
                        # No wizard marker means sign-in did not land -- handing
                        # the page over anyway burned an attempt on an agent
                        # that observed a login page and stopped (live, Intapp
                        # 2026-08-27). Fail here, with the page as evidence.
                        shot = driver.snapshot(
                            page,
                            org_id=ctx.org_id,
                            application_id=ctx.application_id,
                            label="workday-no-wizard",
                            log=log,
                        )
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=True,
                            error=(
                                "signed-in wizard never appeared after sign in "
                                "(no next-button / name / upload marker in 45s)"
                            ),
                            artifacts=[p for p in [shot] if p],
                            verification={"run": log.as_payload()},
                        )
                    walked = LoopResult()
                    agent_loop.walk(
                        page,
                        ctx=ctx,
                        allow_submit=True,
                        log=log,
                        result=walked,
                        start_origin=agent_policy.origin_of(ctx.target.apply_url),
                        resume_path=str(resume_path),
                    )
                    artifacts = list(walked.artifacts)
                    if not walked.ok:
                        return SubmissionResult(
                            ok=False,
                            method=self.method,
                            needs_human=walked.needs_human,
                            error=walked.error or "workday wizard did not complete",
                            artifacts=artifacts,
                            verification={"run": log.as_payload(), "agent": walked.steps},
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
    from ...candidate_credentials import read_password

    return read_password(db, candidate_id=candidate_id, vendor=vendor, tenant=tenant)


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
    from ...candidate_credentials import save_password

    return save_password(
        db,
        org_id=org_id,
        candidate_id=candidate_id,
        vendor=vendor,
        tenant=tenant,
        username=username,
        password=password,
        origin="generated",
    )
