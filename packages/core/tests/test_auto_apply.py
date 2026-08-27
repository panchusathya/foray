"""Auto-apply: what we send, where we send it, and what stops us.

The invariant this file exists to defend is the one from
``CANDIDATE_AGENT_PLAN`` §8, now that an agent can actually press submit:
**nothing is sent in a candidate's name without a recorded per-application yes,
and nothing is invented to fill a form.** The consent and budget tests below
are the load-bearing ones; the adapter tests only prove we speak each vendor's
dialect.

No network anywhere. Adapters take an injected client, the browser tier is
never launched, and the default submitter cannot reach an employer at all.
"""

from __future__ import annotations

import json

import httpx
import pytest

from juicebox.applications.alias import (
    address_for,
    candidate_for,
    ensure,
    parse,
    revoke,
    sending_identity,
)
from juicebox.applications.ats import detect
from juicebox.applications.service import create_application
from juicebox.applications.state import TransitionError, can_transition, transition
from juicebox.applications.submission import (
    NotSubmittable,
    attempts_used,
    auto_submit,
    first_submission_gate,
    request_submission,
    submit_application,
)
from juicebox.applications.submit import registry
from juicebox.applications.submit.base import (
    Attachment,
    SubmissionContext,
    SubmissionResult,
)
from juicebox.applications.submit.console import MemorySubmitter
from juicebox.jobs import KIND_APP_SUBMIT
from juicebox.models import (
    Application,
    ApplicationSubmission,
    Candidate,
    CandidatePreference,
    JobPosting,
    utcnow,
)
from juicebox.repository import upsert_candidates
from juicebox.security import alias_token, verify_alias_token


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    row = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
    row.emails = ["ada@example.com"]
    session.flush()
    return row


@pytest.fixture
def aliased(monkeypatch):
    """Aliasing on, as it is in any deployment that can clear a verification."""
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("APPLY_ALIAS_ADDRESS", "anna@goforay.io")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def memory_mode(monkeypatch):
    """The submitter that captures instead of sending."""
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("APPLY_SUBMITTER", "memory")
    reset_settings_cache()
    registry.reset_overrides()
    yield registry.memory_submitter()
    registry.reset_overrides()
    reset_settings_cache()


def _posting(session, org_id, *, company="Anthropic", url="", suffix="1") -> JobPosting:
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"posting-{company.lower()}-{suffix}",
        title="ML Engineer",
        company=company,
        company_normalized=company.lower(),
        company_domain=f"{company.lower()}.com",
        jd_text="Build things.",
        status="open",
        source_url=url or f"https://boards.greenhouse.io/{company.lower()}/jobs/40123{suffix}",
    )
    session.add(posting)
    session.flush()
    return posting


def _ready(session, org_id, candidate, posting, *, consent=True) -> Application:
    """An application with a package and a recorded yes, ready to submit."""
    application = create_application(
        db=session,
        org_id=org_id,
        candidate_id=candidate.id,
        job_posting_id=posting.id,
    )
    transition(session, application, to_status="prepared", actor="agent")
    if consent:
        application.approved_at = utcnow()
    session.flush()
    return application


class TestVendorDetection:
    """A URL is claimed for a vendor only when the shape actually says so."""

    @pytest.mark.parametrize(
        "url,vendor,tenant,job_id,tier",
        [
            (
                "https://boards.greenhouse.io/anthropic/jobs/4012345",
                "greenhouse",
                "anthropic",
                "4012345",
                detect.TIER_API,
            ),
            (
                "https://job-boards.greenhouse.io/stripe/jobs/6001",
                "greenhouse",
                "stripe",
                "6001",
                detect.TIER_API,
            ),
            (
                "https://boards.greenhouse.io/embed/job_app?token=4012345&for=anthropic",
                "greenhouse",
                "anthropic",
                "4012345",
                detect.TIER_API,
            ),
            (
                "https://jobs.lever.co/figma/1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                "lever",
                "figma",
                "1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                detect.TIER_API,
            ),
            (
                "https://jobs.ashbyhq.com/openai/9f8e7d6c-5b4a-3210-fedc-ba9876543210",
                "ashby",
                "openai",
                "9f8e7d6c-5b4a-3210-fedc-ba9876543210",
                detect.TIER_API,
            ),
            (
                "https://gs.wd5.myworkdayjobs.com/en-US/Goldman/job/NY/Eng_JR-0098765",
                "workday",
                "gs",
                "JR-0098765",
                detect.TIER_BROWSER,
            ),
            (
                "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en"
                "/sites/CX_1/job/141234",
                "oracle",
                "eeho/CX_1",
                "141234",
                detect.TIER_BROWSER,
            ),
            (
                "https://careers-acme.icims.com/jobs/12345/engineer/job",
                "icims",
                "careers-acme",
                "12345",
                detect.TIER_BROWSER,
            ),
            (
                "https://www.ycombinator.com/companies/deep24/jobs/upFGSQJ-founding-engineer",
                "yc",
                "deep24",
                "upFGSQJ-founding-engineer",
                detect.TIER_BROWSER,
            ),
            (
                "https://www.workatastartup.com/jobs/123456",
                "yc",
                "",
                "123456",
                detect.TIER_BROWSER,
            ),
        ],
    )
    def test_known_shapes(self, url, vendor, tenant, job_id, tier):
        target = detect.detect_target(url)
        assert (target.vendor, target.tenant, target.external_job_id, target.tier) == (
            vendor,
            tenant,
            job_id,
            tier,
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/jobs/view/4012345678/",
            "https://example.com/careers/engineer",
            # An oraclecloud host that is not a candidate-experience page: the
            # required locator is what stops us claiming a marketing page.
            "https://eeho.fa.us2.oraclecloud.com/about",
            # A YC company profile and a WaaS listing page: both in the book
            # today, neither is a posting anybody can apply to.
            "https://www.ycombinator.com/companies/nessie",
            "https://www.workatastartup.com/jobs/l/software-engineer",
            "",
            "not-a-url",
        ],
    )
    def test_unknown_is_a_handoff_not_a_guess(self, url):
        target = detect.detect_target(url)
        assert not target
        assert target.tier == detect.TIER_HANDOFF

    def test_aggregators_are_followed_not_claimed(self):
        assert detect.is_aggregator("https://www.linkedin.com/jobs/view/1/")
        assert detect.is_aggregator("https://uk.indeed.com/viewjob?jk=abc")
        assert not detect.is_aggregator("https://boards.greenhouse.io/x/jobs/1")

    def test_an_application_address_needs_the_copy_to_point_at_it(self):
        assert detect.apply_email("To apply, send your CV to jobs@acme.com") == "jobs@acme.com"
        assert detect.apply_email('<a href="mailto:hr@acme.com">') == "hr@acme.com"
        # A bare address in a JD is as likely to be a hiring manager's.
        assert detect.apply_email("Questions? Reach sarah@acme.com") == ""


class TestTargetResolution:
    def test_the_real_form_behind_an_aggregator_listing(self, session, account):
        """An Exa result is usually a listing. One hop, then we stop."""
        posting = _posting(
            session,
            account["org_id"],
            url="https://www.linkedin.com/jobs/view/4012345678/",
        )
        page = (
            '<html><head><link rel="canonical" href="https://www.linkedin.com/x"></head>'
            '<body><a href="https://boards.greenhouse.io/anthropic/jobs/4012345">Apply</a>'
            "</body></html>"
        )
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))
        )
        target = detect.resolve_target(session, posting, client=client)
        assert target.vendor == "greenhouse"
        assert target.external_job_id == "4012345"
        # Cached, so the next reader costs nothing.
        assert posting.ats_vendor == "greenhouse"
        assert posting.ats_resolved_at is not None
        assert detect.target_of(posting).vendor == "greenhouse"

    def test_a_403_is_not_evidence_there_is_no_ats(self, session, account):
        """Boards block datacentre IPs. That must read as unknown, not as no."""
        posting = _posting(session, account["org_id"], url="https://example.com/jobs/1")
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(403, text="nope"))
        )
        target = detect.resolve_target(session, posting, client=client)
        assert not target
        assert posting.ats_vendor == ""
        # And the apply link still points somewhere a human can use.
        assert posting.apply_url == "https://example.com/jobs/1"

    def test_an_application_address_in_the_jd_is_tier_one(self, session, account):
        posting = _posting(session, account["org_id"], url="")
        posting.source_url = ""
        posting.jd_text = "Great role. To apply, email your resume to careers@acme.com."
        target = detect.resolve_target(session, posting, allow_network=False)
        assert target.vendor == detect.VENDOR_EMAIL
        assert target.apply_url == "mailto:careers@acme.com"
        assert target.tier == detect.TIER_EMAIL


class TestTheConsentGateStillHolds:
    """The agent path gets no shortcut around the rule a human path obeys."""

    def test_submitting_requires_a_recorded_yes(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        application = _ready(session, account["org_id"], candidate, posting, consent=False)
        with pytest.raises(TransitionError, match="consent"):
            transition(session, application, to_status="submitting", actor="agent")

    def test_request_submission_refuses_without_consent(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        application = _ready(session, account["org_id"], candidate, posting, consent=False)
        with pytest.raises(Exception, match="consent"):
            request_submission(session, application)
        # And nothing was queued, so no worker can pick it up anyway.
        assert session.query(ApplicationSubmission).count() == 0

    def test_the_submission_budget_applies_to_the_agent_too(
        self, session, account, candidate, monkeypatch
    ):
        from juicebox.applications import limits

        monkeypatch.setattr(limits, "SUBMISSIONS_PER_WEEK", 0)
        posting = _posting(session, account["org_id"])
        application = _ready(session, account["org_id"], candidate, posting)
        with pytest.raises(TransitionError, match="budget"):
            transition(session, application, to_status="submitting", actor="agent")

    def test_the_machine_allows_the_agent_round_trip(self):
        assert can_transition("prepared", "submitting")
        assert can_transition("awaiting_consent", "submitting")
        assert can_transition("submitting", "submitted")
        # A failed attempt goes back to a package that still exists.
        assert can_transition("submitting", "prepared")
        # And nothing skips straight in from draft.
        assert not can_transition("draft", "submitting")


class TestEndToEndWithTheMemorySubmitter:
    def test_a_full_attempt(self, session, account, candidate, memory_mode, aliased):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)

        job = request_submission(session, application)
        assert job.kind == KIND_APP_SUBMIT
        result = submit_application(session, application)

        assert application.status == "submitted"
        assert application.submitted_by == "agent"
        assert application.submission_method == "memory"
        assert result["status"] == "succeeded"

        rows = session.query(ApplicationSubmission).all()
        assert len(rows) == 1
        assert rows[0].attempt == 1
        assert rows[0].vendor == "greenhouse"
        assert rows[0].finished_at is not None

        # The employer saw the alias, not the candidate's own address.
        sent = memory_mode.sent[-1]
        assert sent.from_address.startswith("anna+")
        assert sent.reply_to_address == "ada@example.com"

    def test_a_dry_run_never_texts_the_candidate(
        self, session, account, candidate, memory_mode, aliased, monkeypatch
    ):
        """A rehearsal must be invisible outside our own records.

        Console and memory return ok=True on purpose, so the status machine and
        the attempt row get exercised. The notification is the one part of that
        which reaches a real person, and "your application has been sent" is a
        lie when nothing was sent.
        """
        from juicebox.applications import submission as submission_mod

        notified = []
        monkeypatch.setattr(
            submission_mod,
            "_notify_sent",
            lambda db, application, attempt: notified.append(attempt.method)
            if attempt.method not in {"console", "memory"}
            else None,
        )

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        request_submission(session, application)
        submit_application(session, application)

        assert application.status == "submitted"
        assert application.submission_method == "memory"
        assert notified == []

    def test_a_real_send_still_notifies(self, session, account, candidate, monkeypatch):
        """The suppression keys off the method, so a real adapter is unaffected."""
        from juicebox.applications import submission as submission_mod
        from juicebox.messaging import notify as notify_mod
        from juicebox.models import ApplicationSubmission as _Attempt

        called = []
        monkeypatch.setattr(
            notify_mod, "notify_candidate", lambda db, **kw: called.append(kw["event"])
        )

        application = Application(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id="jp_x",
            status="submitted",
        )
        for method, expected in (("console", 0), ("memory", 0), ("greenhouse_api", 1)):
            called.clear()
            submission_mod._notify_sent(
                session,
                application,
                _Attempt(org_id=account["org_id"], id=f"sub_{method}", method=method, attempt=1),
            )
            assert len(called) == expected, f"{method} should notify {expected} time(s)"

    def test_a_second_attempt_is_refused_after_success(
        self, session, account, candidate, memory_mode, aliased
    ):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        request_submission(session, application)
        submit_application(session, application)

        # Re-running the same job is a no-op, not a second application.
        again = submit_application(session, application)
        assert again["skipped"] == "already submitted"
        assert session.query(ApplicationSubmission).count() == 1

    def test_the_double_submit_guard_is_in_the_database(
        self, session, account, candidate, memory_mode, aliased
    ):
        from sqlalchemy.exc import IntegrityError

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        request_submission(session, application)
        submit_application(session, application)

        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=99,
                vendor="greenhouse",
                method="memory",
                status="succeeded",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_a_failed_attempt_parks_the_package_rather_than_losing_it(
        self, session, account, candidate, aliased
    ):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        registry.set_submitter(
            "greenhouse",
            MemorySubmitter(
                result=SubmissionResult(
                    ok=False, method="memory", needs_human=True, error="form asked for a portfolio"
                )
            ),
        )
        try:
            submit_application(session, application)
        finally:
            registry.reset_overrides()

        assert application.status == "prepared"
        assert "portfolio" in application.status_reason
        row = session.query(ApplicationSubmission).one()
        assert row.status == "needs_human"
        # The recorded yes survives, so the retry does not ask again.
        assert application.approved_at is not None

    def test_a_parked_run_stays_in_flight(self, session, account, candidate, aliased):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        registry.set_submitter(
            "greenhouse",
            MemorySubmitter(
                result=SubmissionResult(
                    ok=False,
                    method="browser",
                    needs_verification=True,
                    verification={"stage": "account_created"},
                )
            ),
        )
        try:
            submit_application(session, application)
        finally:
            registry.reset_overrides()

        # Not back to prepared: something is genuinely in flight, and showing it
        # as ready would invite a second attempt at the same form.
        assert application.status == "submitting"
        row = session.query(ApplicationSubmission).one()
        assert row.status == "needs_verification"
        assert row.finished_at is None

    def test_the_retry_cap_hands_over_to_a_person(
        self, session, account, candidate, aliased, monkeypatch
    ):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("APPLY_MAX_ATTEMPTS", "1")
        reset_settings_cache()
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="greenhouse",
                method="memory",
                status="failed",
            )
        )
        session.flush()
        with pytest.raises(Exception, match="needs a person"):
            request_submission(session, application)
        reset_settings_cache()


class TestNothingAutomatedIsTheNormalAnswer:
    def test_an_unrecognised_ats_is_a_handoff(self, session, account, candidate, memory_mode):
        posting = _posting(session, account["org_id"], url="https://example.com/careers/1")
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        with pytest.raises(NotSubmittable, match="hand-off"):
            request_submission(session, application)

    def test_auto_submit_reports_instead_of_raising(self, session, account, candidate, memory_mode):
        """A portal tap must not 500 because Taleo exists."""
        posting = _posting(session, account["org_id"], url="https://example.com/careers/1")
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        # Give the candidate a prior submission so the first-run gate is not
        # what we are measuring here.
        other = _posting(session, account["org_id"], company="Stripe", suffix="2")
        prior = _ready(session, account["org_id"], candidate, other)
        transition(session, prior, to_status="submitted", actor="candidate")

        job, reason = auto_submit(session, application)
        assert job is None
        assert "hand-off" in reason

    def test_the_first_application_does_not_need_a_second_yes(
        self, session, account, candidate, memory_mode
    ):
        """Thumbs-up (or a portal apply-for-me tap) is the yes. A dashboard
        review of the package we just built used to be a second gate."""
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        assert application.approved_at is not None
        assert first_submission_gate(session, application) == ""
        job, reason = auto_submit(session, application)
        assert job is not None, reason

    def test_no_recorded_yes_still_refuses(self, session, account, candidate, memory_mode):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting, consent=False)
        assert first_submission_gate(session, application) == ""
        job, reason = auto_submit(session, application)
        assert job is None
        assert "consent" in reason

    def test_console_is_the_default_and_cannot_reach_an_employer(self):
        from juicebox.applications.submit.console import ConsoleSubmitter

        assert registry.mode() == "console"
        chosen = registry.get_submitter(detect.detect_target("https://jobs.lever.co/x/" + "a" * 24))
        assert isinstance(chosen, ConsoleSubmitter)


class TestAliasIdentity:
    def test_the_token_verifies_and_cannot_be_forged(self):
        token = alias_token("cand_abc")
        assert verify_alias_token("cand_abc", token)
        # Mail transport lowercases local parts; that must still verify.
        assert verify_alias_token("cand_abc", token.upper())
        assert not verify_alias_token("cand_abc", "0" * 18)
        assert not verify_alias_token("cand_other", token)

    def test_parsing_only_accepts_our_own_mailbox(self, aliased):
        address = address_for("cand_abc")
        assert address == f"anna+{alias_token('cand_abc')}@goforay.io"
        assert parse(address) == alias_token("cand_abc")
        assert parse(f"Anna <{address.upper()}>") == alias_token("cand_abc")
        assert parse("someone@else.com") == ""
        # Right domain, wrong mailbox.
        assert parse(f"bob+{alias_token('cand_abc')}@goforay.io") == ""

    def test_off_by_default_means_the_candidates_own_address(self, session, candidate):
        from_address, reply_to = sending_identity(session, candidate)
        assert from_address == reply_to == "ada@example.com"

    def test_lookup_verifies_the_token_against_the_row(self, session, candidate, aliased):
        row = ensure(session, candidate)
        assert row is not None
        found = candidate_for(session, row.address)
        assert found is not None and found.id == candidate.id

        revoke(session, candidate.id)
        assert candidate_for(session, row.address) is None

    def test_issuing_is_idempotent(self, session, candidate, aliased):
        first = ensure(session, candidate)
        second = ensure(session, candidate)
        assert first is not None and second is not None
        assert first.id == second.id


class TestInboundMail:
    """Employer mail is classified from its own words, timidly."""

    @pytest.mark.parametrize(
        "subject,body,kind",
        [
            (
                "Verify your account",
                "Click https://x.wd5.myworkdayjobs.com/verify/abc",
                "verification",
            ),
            ("Your code", "Your one-time passcode is 483920", "otp"),
            ("Code", "Your verification code: 1234", "otp"),
            ("Thanks", "Thank you for applying to Anthropic.", "acknowledged"),
            ("Update", "We have decided to pursue other candidates.", "rejected"),
            # A rejection that opens by thanking you is a rejection.
            (
                "Update",
                "Thank you for applying. Unfortunately, we will not be moving forward.",
                "rejected",
            ),
            ("Hello", "Do you have time to chat on Thursday?", "other"),
        ],
    )
    def test_classification(self, subject, body, kind):
        from juicebox.applications.inbox import classify

        assert classify(subject, body).kind == kind

    def test_an_acknowledgement_moves_the_matching_application(
        self, session, account, candidate, aliased
    ):
        from juicebox.applications.inbox import handle

        posting = _posting(session, account["org_id"])
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitted", actor="candidate")
        row = ensure(session, candidate)

        outcome = handle(
            session,
            org_id=account["org_id"],
            to_addresses=(row.address,),
            from_address="no-reply@anthropic.com",
            subject="We received your application",
            body="Thank you for applying to Anthropic.",
        )
        assert outcome["routed"] is True
        assert outcome["action"] == "moved_to_acknowledged"
        assert application.status == "acknowledged"

    def test_an_acknowledgement_during_submitting_still_lands(
        self, session, account, candidate, aliased
    ):
        """ATS mail often arrives before the browser run records submitted."""
        from juicebox.applications.inbox import handle

        posting = _posting(session, account["org_id"])
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitting", actor="candidate")
        row = ensure(session, candidate)

        outcome = handle(
            session,
            org_id=account["org_id"],
            to_addresses=(row.address,),
            from_address="no-reply@anthropic.com",
            subject="We received your application",
            body="Thank you for applying to Anthropic.",
        )
        assert outcome["routed"] is True
        assert outcome["action"] == "moved_to_acknowledged"
        assert application.status == "acknowledged"
        assert application.submitted_at is not None

    def test_an_unmatched_sender_moves_nothing(self, session, account, candidate, aliased):
        from juicebox.applications.inbox import handle

        for suffix, company in (("1", "Anthropic"), ("2", "Stripe")):
            posting = _posting(session, account["org_id"], company=company, suffix=suffix)
            application = _ready(session, account["org_id"], candidate, posting)
            transition(session, application, to_status="submitted", actor="candidate")
        row = ensure(session, candidate)

        outcome = handle(
            session,
            org_id=account["org_id"],
            to_addresses=(row.address,),
            from_address="careers@some-unrelated-vendor.io",
            subject="Update",
            body="We have decided to pursue other candidates.",
        )
        # Two open pursuits and a sender matching neither: guessing here would
        # tell somebody they were rejected from the wrong job.
        assert outcome["application_id"] == ""
        assert "action" not in outcome
        statuses = {row.status for row in session.query(Application).all()}
        assert statuses == {"submitted"}

    def test_mail_to_a_foreign_address_is_not_ours(self, session, account, aliased):
        from juicebox.applications.inbox import handle

        outcome = handle(
            session,
            org_id=account["org_id"],
            to_addresses=("recruiter@juicebox.example",),
            from_address="someone@acme.com",
            subject="hi",
            body="hello",
        )
        assert outcome["routed"] is False


class TestAdapters:
    """Each adapter speaks its vendor's dialect and refuses rather than guesses."""

    @staticmethod
    def _ctx(vendor: str, url: str) -> SubmissionContext:
        return SubmissionContext(
            application_id="app_1",
            org_id="org_1",
            candidate_id="cand_1",
            target=detect.detect_target(url),
            full_name="Ada Okonkwo",
            first_name="Ada",
            last_name="Okonkwo",
            from_address="anna+abc@goforay.io",
            reply_to_address="ada@example.com",
            phone="+15551234567",
            linkedin_url="https://linkedin.com/in/ada",
            resume=Attachment("ada-resume.pdf", "application/pdf", b"%PDF-1.4 resume"),
            cover_letter=Attachment("ada-letter.pdf", "application/pdf", b"%PDF-1.4 letter"),
            answers=[
                {"key": "salary_expectation", "question": "Comp?", "answer": "240000"},
                {"key": "why_company", "question": "Why us?", "answer": ""},
            ],
            company="Anthropic",
            title="ML Engineer",
            posting_url=url,
        )

    def _recording_client(self, response: httpx.Response):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content
            seen["headers"] = dict(request.headers)
            return response

        return httpx.Client(transport=httpx.MockTransport(handler)), seen

    def test_lever_posts_the_form(self):
        from juicebox.applications.submit.lever import LeverSubmitter

        url = "https://jobs.lever.co/figma/1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        client, seen = self._recording_client(httpx.Response(200, json={"data": {"id": "app-9"}}))
        result = LeverSubmitter(board_key="lever-key", client=client).submit(
            self._ctx("lever", url)
        )
        assert result.ok
        assert result.external_id == "app-9"
        assert "postings/figma/1a2b3c4d" in seen["url"]
        # The employer's key, and the headers Lever records on the application.
        assert "key=lever-key" in seen["url"]
        assert seen["headers"]["user-agent"].startswith("Foray/")
        # The alias is the address the employer gets, and the resume travelled.
        assert b"anna+abc@goforay.io" in seen["body"]
        assert b"%PDF-1.4 resume" in seen["body"]
        # A blank we chose not to invent stays out of the request entirely.
        assert b"comments" not in seen["body"]

    def test_ashby_sends_field_submissions(self):
        from juicebox.applications.submit.ashby import AshbySubmitter

        url = "https://jobs.ashbyhq.com/openai/9f8e7d6c-5b4a-3210-fedc-ba9876543210"
        client, seen = self._recording_client(
            httpx.Response(200, json={"success": True, "results": {"id": "sub-3"}})
        )
        result = AshbySubmitter(board_key="ashby-key", client=client).submit(
            self._ctx("ashby", url)
        )
        assert result.ok
        assert result.external_id == "sub-3"
        assert b"_systemfield_email" in seen["body"]

    def test_a_4xx_is_a_form_we_do_not_understand_not_a_retry(self):
        from juicebox.applications.submit.lever import LeverSubmitter

        url = "https://jobs.lever.co/figma/1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        client, _ = self._recording_client(httpx.Response(422, text="requires a portfolio url"))
        result = LeverSubmitter(board_key="k", client=client).submit(self._ctx("lever", url))
        assert not result.ok
        assert result.needs_human
        assert result.status == "needs_human"
        assert "portfolio" in result.error

    def test_a_5xx_is_the_vendors_problem_and_is_retried(self):
        from juicebox.applications.submit.base import SubmitError
        from juicebox.applications.submit.lever import LeverSubmitter

        url = "https://jobs.lever.co/figma/1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        client, _ = self._recording_client(httpx.Response(503, text="down"))
        with pytest.raises(SubmitError):
            LeverSubmitter(board_key="k", client=client).submit(self._ctx("lever", url))

    def test_an_api_adapter_reached_without_a_key_says_so(self):
        """The registry should never route here without one. If it did, say
        which board we lack a key for rather than posting blind and eating a
        401 that reads like the form rejected us."""
        from juicebox.applications.submit.greenhouse import GreenhouseSubmitter

        url = "https://boards.greenhouse.io/anthropic/jobs/4012345"
        result = GreenhouseSubmitter().submit(self._ctx("greenhouse", url))
        assert result.needs_human
        assert "anthropic" in result.error

    def test_ashby_two_hundred_with_success_false_is_not_a_submission(self):
        """The worst failure this module has available: telling a candidate we
        applied when the employer never recorded it. Ashby reports validation
        failures in the body, with a 200."""
        from juicebox.applications.submit.ashby import AshbySubmitter

        url = "https://jobs.ashbyhq.com/openai/9f8e7d6c-5b4a-3210-fedc-ba9876543210"
        client, _ = self._recording_client(
            httpx.Response(
                200, json={"success": False, "errors": ["Work authorization is required"]}
            )
        )
        result = AshbySubmitter(board_key="k", client=client).submit(self._ctx("ashby", url))
        assert not result.ok
        assert result.needs_human
        assert "Work authorization" in result.error

    def test_an_unreadable_ashby_body_is_a_refusal_not_a_success(self):
        from juicebox.applications.submit.ashby import AshbySubmitter

        url = "https://jobs.ashbyhq.com/openai/9f8e7d6c-5b4a-3210-fedc-ba9876543210"
        client, _ = self._recording_client(httpx.Response(200, text="<html>maintenance</html>"))
        result = AshbySubmitter(board_key="k", client=client).submit(self._ctx("ashby", url))
        assert not result.ok

    def test_the_email_adapter_names_the_role_and_attaches(self):
        from juicebox.applications.submit.email import EmailSubmitter, build_email
        from juicebox.outreach import sender

        ctx = self._ctx("email", "mailto:careers@acme.com")
        built = build_email(ctx)
        assert built.to == "careers@acme.com"
        assert "ML Engineer" in built.subject
        assert "240000" in built.text_body
        # why_company is blank by design and never printed as an empty label.
        assert "Why us?" not in built.text_body

        memory = sender.MemorySender()
        sender.set_sender(memory)
        try:
            result = EmailSubmitter().submit(ctx)
        finally:
            sender.set_sender(None)
        assert result.ok
        assert memory.outbox[0].from_email == "anna+abc@goforay.io"
        assert memory.attachments and memory.attachments[0]
        assert memory.attachments[0][0].filename == "ada-resume.pdf"

    def test_the_email_adapter_refuses_a_sender_that_cannot_attach(self):
        """A body-only send used to report success and mark the application
        submitted without the resume. Console/memory are dry runs; anything
        that actually delivers must carry the files or stop."""
        from juicebox.applications.submit.email import EmailSubmitter
        from juicebox.outreach import sender
        from juicebox.outreach.sender import SendResult

        class BodyOnly:
            name = "resend"

            def send(self, email):
                return SendResult(ok=True, provider_message_id="re_lie")

        ctx = self._ctx("email", "mailto:careers@acme.com")
        sender.set_sender(BodyOnly())
        try:
            result = EmailSubmitter().submit(ctx)
        finally:
            sender.set_sender(None)
        assert result.needs_human
        assert "cannot attach" in result.error

    def test_the_email_adapter_refuses_with_no_return_address(self):
        from juicebox.applications.submit.email import EmailSubmitter

        ctx = self._ctx("email", "mailto:careers@acme.com")
        ctx.from_address = ""
        result = EmailSubmitter().submit(ctx)
        assert result.needs_human
        assert "return address" in result.error

    def test_workday_will_not_run_without_a_mailbox_we_can_read(self):
        from juicebox.applications.submit.browser.driver import available
        from juicebox.applications.submit.browser.workday import WorkdaySubmitter

        url = "https://gs.wd5.myworkdayjobs.com/en-US/Goldman/job/NY/Eng_JR-0098765"
        ctx = self._ctx("workday", url)
        ctx.from_address = ""
        result = WorkdaySubmitter().submit(ctx)
        assert not result.ok
        installed, _ = available()
        # Either refusal is correct; both stop before touching the portal.
        assert ("alias" in result.error) if installed else ("playwright" in result.error)


class TestBrowserFieldFilling:
    """The field filler reports what it could not find; it never improvises."""

    class _Locator:
        def __init__(self, present=True, visible=True):
            self._present = present
            self._visible = visible
            self.filled = None

        @property
        def first(self):
            return self

        def count(self):
            return 1 if self._present else 0

        def is_visible(self):
            return self._visible

        def fill(self, value):
            self.filled = value

    class _Page:
        def __init__(self, known):
            self.known = known

        def locator(self, selector):
            return self.known.get(selector) or TestBrowserFieldFilling._Locator(present=False)

    def test_a_required_field_we_cannot_find_stops_the_run(self):
        from juicebox.applications.submit.browser.driver import FieldSpec, RunLog, fill

        found = self._Locator()
        page = self._Page({"#first": found})
        log = RunLog()
        missing = fill(
            page,
            [
                FieldSpec(key="first_name", selectors=("#first",), value="Ada", required=True),
                FieldSpec(key="portfolio", selectors=("#portfolio",), value="x", required=True),
                FieldSpec(key="optional", selectors=("#nope",), value="y"),
            ],
            log=log,
        )
        assert missing == ["portfolio"]
        assert found.filled == "Ada"
        assert log.filled == {"first_name": "Ada"}

    def test_a_blank_answer_is_left_blank_on_the_form(self):
        from juicebox.applications.submit.browser.driver import FieldSpec, RunLog, fill

        found = self._Locator()
        log = RunLog()
        missing = fill(
            self._Page({"#why": found}),
            [FieldSpec(key="why_company", selectors=("#why",), value="")],
            log=log,
        )
        assert missing == []
        assert found.filled is None

    def test_a_secret_reaches_the_form_and_never_the_run_log(self):
        """The log is persisted with the attempt, so a value recorded here is a
        value a database dump reads. "We filled the password box" is the whole
        of what diagnosis needs."""
        from juicebox.applications.submit.browser.driver import FieldSpec, RunLog, fill

        box = self._Locator()
        log = RunLog()
        fill(
            self._Page({"#pw": box}),
            [
                FieldSpec(
                    key="password",
                    selectors=("#pw",),
                    value="s3cret-generated",
                    required=True,
                    secret=True,
                )
            ],
            log=log,
        )
        assert box.filled == "s3cret-generated", "the portal still gets the real value"
        assert log.filled == {"password": "<redacted>"}
        assert "s3cret-generated" not in json.dumps(log.as_payload())

    def test_the_workday_account_form_declares_its_password_fields_secret(self):
        """Both of them: the confirm box carried the same plaintext copy."""
        from juicebox.applications.submit.browser.workday import _account_fields

        specs = {spec.key: spec for spec in _account_fields("anna+abc@goforay.io", "pw")}
        assert specs["password"].secret
        assert specs["verify_password"].secret
        assert not specs["email"].secret


class TestSyncKeepsAtsMailOutOfTheReplyPath:
    def test_alias_mail_never_reaches_the_recruiter_machinery(
        self, session, account, candidate, aliased, monkeypatch
    ):
        """An automated acknowledgement is not a candidate changing their mind."""
        from juicebox.integrations import sync
        from juicebox.integrations.providers import MailboxEvent
        from juicebox.models import MailboxConnection

        row = ensure(session, candidate)
        connection = MailboxConnection(
            org_id=account["org_id"], provider="microsoft", address="anna@goforay.io"
        )
        session.add(connection)
        session.flush()

        matched = []
        monkeypatch.setattr(sync, "_match_outbound", lambda *a, **k: matched.append(1) or None)
        summary = sync.SyncSummary()
        sync._handle_inbound_event(
            session,
            connection,
            MailboxEvent(
                native_id="msg-1",
                from_address="no-reply@anthropic.com",
                subject="Thanks",
                body="Thank you for applying to Anthropic.",
                to_addresses=(row.address,),
            ),
            summary=summary,
            now=utcnow(),
            address_index={},
        )
        assert summary.alias_routed == 1
        assert matched == []


class TestResolutionOnTheIngestPath:
    """Parsing every posting must not mean fetching every posting."""

    def test_a_recognisable_url_resolves_with_no_network(self, session, account):
        posting = _posting(session, account["org_id"])
        target = detect.resolve_target(session, posting, allow_network=False)
        assert target.vendor == "greenhouse"
        assert posting.ats_resolved_at is not None

    def test_an_unknown_url_stays_open_for_a_later_follow(self, session, account):
        """A URL-only miss is not a final answer, or the one moment a fetch is
        actually worth paying for would never happen."""
        posting = _posting(session, account["org_id"], url="https://example.com/careers/1")
        assert not detect.resolve_target(session, posting, allow_network=False)
        assert posting.ats_resolved_at is None
        # And the hand-off link is still populated.
        assert posting.apply_url == "https://example.com/careers/1"

    def test_the_application_link_is_the_form_not_the_listing(self, session, account, candidate):
        posting = _posting(
            session, account["org_id"], url="https://www.linkedin.com/jobs/view/4012345678/"
        )
        page = '<a href="https://boards.greenhouse.io/anthropic/jobs/4012345">Apply</a>'
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))
        )
        detect.resolve_target(session, posting, client=client)
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        assert application.submission_url == "https://boards.greenhouse.io/anthropic/jobs/4012345"


class TestTheThreadPath:
    """ "apply to 2" records the yes at creation, so the package build is where
    an automated attempt becomes possible."""

    def test_the_package_build_queues_an_attempt_and_sends_one_message(
        self, session, account, candidate, memory_mode, aliased
    ):
        from juicebox.jobs import KIND_APP_PACKAGE, enqueue
        from juicebox.models import Job
        from juicebox.worker import _handle_app_package

        # Work authorization is the one fact auto_submit still withholds on;
        # seed it so this test is measuring the queue, not the ask.
        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=candidate.id,
                spec={},
                work_authorization="US citizen, no sponsorship needed",
            )
        )
        session.flush()

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        application.approved_at = utcnow()
        session.flush()

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_APP_PACKAGE,
            idempotency_key="pkg-1",
            payload={"application_id": application.id},
        )
        result = _handle_app_package(session, job, utcnow())

        assert application.status == "prepared"
        assert result["submission"] == "queued"
        queued = [
            row
            for row in session.query(Job).all()
            if row.kind == KIND_APP_SUBMIT and row.payload.get("application_id") == application.id
        ]
        assert len(queued) == 1

    def test_missing_work_authorization_asks_instead_of_queueing(
        self, session, account, candidate, memory_mode
    ):
        from juicebox.jobs import KIND_APP_PACKAGE, enqueue
        from juicebox.worker import _handle_app_package

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        application.approved_at = utcnow()
        session.flush()
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_APP_PACKAGE,
            idempotency_key="pkg-auth",
            payload={"application_id": application.id},
        )
        result = _handle_app_package(session, job, utcnow())
        assert "waiting on required form answers" in result["submission"]
        assert "authorized" in result["submission"].lower() or "sponsor" in result["submission"].lower()

    def test_a_handoff_still_tells_the_candidate_their_package_is_ready(
        self, session, account, candidate, memory_mode
    ):
        from juicebox.jobs import KIND_APP_PACKAGE, enqueue
        from juicebox.worker import _handle_app_package

        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=candidate.id,
                spec={},
                work_authorization="US citizen, no sponsorship needed",
            )
        )
        session.flush()
        posting = _posting(session, account["org_id"], url="https://example.com/careers/1")
        detect.resolve_target(session, posting, allow_network=False)
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        application.approved_at = utcnow()
        session.flush()
        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_APP_PACKAGE,
            idempotency_key="pkg-2",
            payload={"application_id": application.id},
        )
        # The follow is allowed here (nobody resolved this one), so the fetch is
        # stubbed out rather than reaching the network from a test.
        from juicebox.applications.ats import detect as ats_detect

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(ats_detect, "follow", lambda *a, **k: ats_detect.ApplyTarget())
        try:
            result = _handle_app_package(session, job, utcnow())
        finally:
            monkeypatch.undo()
        assert "hand-off" in result["submission"]
        assert attempts_used(session, application) == 0


class TestParkedCredentialsAreNotReadableAtRest:
    """A parked Workday run carries the portal password we generated for it.

    It has to survive between the two legs, so it lives on the attempt row --
    which is exactly the place a database dump reads. Same treatment as mailbox
    OAuth tokens and `ats_accounts.token_blob`.
    """

    def test_the_password_is_encrypted_on_the_row_and_restored_for_the_adapter(
        self, session, account, candidate, aliased
    ):
        from juicebox.applications.submission import _seal, _unseal

        parked = {
            "stage": "account_created",
            "username": "anna+abc@goforay.io",
            "password": "hunter2-generated",
            "run": {"steps": ["browser opened"]},
        }
        sealed = _seal(parked)
        assert "password" not in sealed
        assert "hunter2-generated" not in json.dumps(sealed)
        # Everything that makes a parked run diagnosable stays readable.
        assert sealed["stage"] == "account_created"
        assert sealed["run"] == {"steps": ["browser opened"]}

        assert _unseal(sealed)["password"] == "hunter2-generated"

    def test_an_unreadable_blob_ends_the_run_instead_of_raising(self, caplog):
        """SECRET_KEY rotated under a parked attempt. The adapter then has no
        credential and reports needs_human, which a person can act on."""
        from juicebox.applications.submission import _unseal

        recovered = _unseal({"stage": "account_created", "secret_blob": "not-a-fernet-token"})
        assert "password" not in recovered
        assert recovered["stage"] == "account_created"

    def test_sealing_reaches_a_credential_the_run_log_nested(self):
        """`_seal` popped the top level only, so the copies the browser run log
        carried at run.filled.password went to the row in clear -- and on the
        branches with no top-level password there was nothing to encrypt at all,
        which left the credential existing *only* in plaintext."""
        from juicebox.applications.submission import _seal

        sealed = _seal(
            {
                "run": {
                    "steps": ["browser opened"],
                    "filled": {
                        "email": "anna+abc@goforay.io",
                        "password": "s3cret-generated",
                        "verify_password": "s3cret-generated",
                    },
                }
            }
        )
        assert "s3cret-generated" not in json.dumps(sealed)
        # And the rest of the log, which is what makes the attempt diagnosable.
        assert sealed["run"]["steps"] == ["browser opened"]
        assert sealed["run"]["filled"]["email"] == "anna+abc@goforay.io"

    def test_sealing_keeps_the_blob_it_just_wrote(self):
        """`secret_blob` is named like a secret and is the one that must live."""
        from juicebox.applications.submission import _seal, _unseal

        sealed = _seal({"password": "top-level", "run": {"filled": {"password": "nested"}}})
        assert _unseal(sealed)["password"] == "top-level"
        assert "nested" not in json.dumps(sealed)

    def test_a_parked_attempt_stores_no_plaintext_secret(
        self, session, account, candidate, aliased
    ):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        registry.set_submitter(
            "greenhouse",
            MemorySubmitter(
                result=SubmissionResult(
                    ok=False,
                    method="browser",
                    needs_verification=True,
                    verification={"stage": "account_created", "password": "s3cret-generated"},
                )
            ),
        )
        try:
            submit_application(session, application)
        finally:
            registry.reset_overrides()

        row = session.query(ApplicationSubmission).one()
        assert "s3cret-generated" not in json.dumps(row.verification)
        assert row.verification["stage"] == "account_created"

    def test_an_attempt_that_only_nested_the_credential_stores_none_of_it(
        self, session, account, candidate, aliased
    ):
        """The shape of workday's required-fields-missing branch: no top-level
        password to seal, so the only copy on the row was the run log's."""
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        registry.set_submitter(
            "greenhouse",
            MemorySubmitter(
                result=SubmissionResult(
                    ok=False,
                    method="browser",
                    needs_human=True,
                    error="could not fill required account fields: terms",
                    verification={
                        "steps": ["browser opened"],
                        "filled": {"password": "s3cret-generated"},
                        "missing": ["terms"],
                    },
                )
            ),
        )
        try:
            submit_application(session, application)
        finally:
            registry.reset_overrides()

        row = session.query(ApplicationSubmission).one()
        assert "s3cret-generated" not in json.dumps(row.verification)
        assert row.verification["missing"] == ["terms"]

    def test_the_recruiter_api_returns_the_run_log_and_nothing_else(
        self, session, account, candidate, client, aliased
    ):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="workday",
                method="browser",
                status="needs_verification",
                verification={
                    "stage": "account_created",
                    "secret_blob": "encrypted-blob-here",
                    "run": {"steps": ["browser opened"], "filled": {"first_name": "Ada"}},
                },
            )
        )
        session.commit()

        body = client.get(
            f"/v1/applications/{application.id}/submissions", headers=account["headers"]
        ).json()
        entry = body["submissions"][0]
        assert entry["run"]["filled"] == {"first_name": "Ada"}
        # The blob is never in the response at all, encrypted or not.
        assert "secret_blob" not in json.dumps(entry)


class TestApiVersusForm:
    """Holding the employer's key is what decides which door we use.

    Greenhouse, Lever and Ashby all authenticate their apply API with a key the
    employer issues from their own account. With one, the API is better. Without
    one -- the open market, most postings -- the candidate's door is the hosted
    form, and that needs no key at all.
    """

    @pytest.fixture(autouse=True)
    def live(self, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("APPLY_SUBMITTER", "live")
        reset_settings_cache()
        registry.reset_overrides()
        yield
        registry.reset_overrides()
        reset_settings_cache()

    def test_no_key_means_a_handoff_without_the_agent(self):
        target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
        chosen = registry.get_submitter(target)
        assert chosen is None

    def test_agent_configured_means_the_browser_agent(self, monkeypatch):
        from juicebox.applications.submit.browser.agent import BrowserAgentSubmitter
        from juicebox.applications.submit.browser.cloud import FakeCloudClient, set_cloud_client
        from juicebox.config import reset_settings_cache as _reset

        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu_test")
        monkeypatch.setenv("APPLY_BROWSER_AGENT", "true")
        monkeypatch.setenv("APPLY_BROWSER_AGENT_ACK", "true")
        _reset()
        set_cloud_client(FakeCloudClient())
        try:
            target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
            chosen = registry.get_submitter(target)
            assert isinstance(chosen, BrowserAgentSubmitter)
            assert chosen.method == "browser"
        finally:
            set_cloud_client(None)
            _reset()

    def test_a_stored_key_means_the_api(self):
        from juicebox.applications.submit.greenhouse import GreenhouseSubmitter

        target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
        chosen = registry.get_submitter(target, board_key="gh-key")
        assert isinstance(chosen, GreenhouseSubmitter)
        assert chosen.board_key == "gh-key"

    def test_a_key_for_one_board_does_not_leak_to_another(self, session, account, candidate):
        """The bug the old global GREENHOUSE_API_KEY had: one deployment key
        cannot post to a board it was not issued for."""
        from juicebox.applications.credentials import board_key, set_board_key

        set_board_key(
            session,
            org_id=account["org_id"],
            vendor="greenhouse",
            tenant="anthropic",
            key="anthropic-only",
        )
        assert (
            board_key(session, org_id=account["org_id"], vendor="greenhouse", tenant="anthropic")
            == "anthropic-only"
        )
        assert (
            board_key(session, org_id=account["org_id"], vendor="greenhouse", tenant="stripe") == ""
        )

    def test_a_key_is_encrypted_at_rest(self, session, account):
        from juicebox.applications.credentials import set_board_key
        from juicebox.models import AtsBoardCredential

        set_board_key(
            session,
            org_id=account["org_id"],
            vendor="lever",
            tenant="figma",
            key="super-secret-key",
        )
        row = session.query(AtsBoardCredential).one()
        assert "super-secret-key" not in row.token_blob

    def test_the_submission_service_picks_up_the_stored_key(
        self, session, account, candidate, aliased
    ):
        from juicebox.applications.credentials import set_board_key
        from juicebox.applications.submission import submitter_for
        from juicebox.applications.submit.greenhouse import GreenhouseSubmitter

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        target = detect.target_of(posting)

        chosen, key = submitter_for(session, application, posting, target)
        assert key == ""
        assert not isinstance(chosen, GreenhouseSubmitter)

        set_board_key(
            session,
            org_id=account["org_id"],
            vendor="greenhouse",
            tenant=target.tenant,
            key="board-key",
        )
        chosen, key = submitter_for(session, application, posting, target)
        assert key == "board-key"
        assert isinstance(chosen, GreenhouseSubmitter)


class TestTheGapsThatStrandedApplications:
    def test_a_crashed_attempt_can_be_picked_back_up(
        self, session, account, candidate, memory_mode, aliased
    ):
        """The worker sets `submitting` before the adapter runs. If it then dies,
        recover_stale_locks requeues the job -- and the handler used to skip it,
        leaving the application in `submitting` with no way out."""
        from juicebox.jobs import enqueue
        from juicebox.worker import _handle_app_submit

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitting", actor="agent")

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_APP_SUBMIT,
            idempotency_key="resubmit-1",
            payload={"application_id": application.id},
        )
        result = _handle_app_submit(session, job, utcnow())
        assert "skipped" not in result
        assert application.status == "submitted"

    def test_a_parked_attempt_that_never_gets_its_mail_ages_out(
        self, session, account, candidate, aliased
    ):
        from datetime import timedelta

        from juicebox.applications.submission import expire_parked

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitting", actor="agent")
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="workday",
                method="browser",
                status="needs_verification",
                started_at=utcnow() - timedelta(days=3),
            )
        )
        session.flush()

        assert expire_parked(session, org_id=account["org_id"]) == 1
        row = session.query(ApplicationSubmission).one()
        assert row.status == "needs_human"
        # And the application stops claiming to be in flight.
        assert application.status == "prepared"
        assert "verification mail" in application.status_reason

    def test_a_recent_parked_attempt_is_left_alone(self, session, account, candidate, aliased):
        from juicebox.applications.submission import expire_parked

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="workday",
                method="browser",
                status="needs_verification",
                started_at=utcnow(),
            )
        )
        session.flush()
        assert expire_parked(session, org_id=account["org_id"]) == 0

    def test_a_portal_account_is_remembered_for_the_next_role(
        self, session, account, candidate, aliased
    ):
        """Without this, the second role at the same employer registers a second
        account and the verification mail arrives all over again."""
        from juicebox.integrations.crypto import decrypt_json
        from juicebox.models import AtsAccount

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        registry.set_submitter(
            "greenhouse",
            MemorySubmitter(
                result=SubmissionResult(
                    ok=False,
                    method="browser",
                    needs_verification=True,
                    verification={
                        "stage": "account_created",
                        "username": "anna+abc@goforay.io",
                        "password": "generated-pw",
                    },
                )
            ),
        )
        try:
            submit_application(session, application)
        finally:
            registry.reset_overrides()

        stored = session.query(AtsAccount).one()
        assert stored.candidate_id == candidate.id
        assert stored.username == "anna+abc@goforay.io"
        # Encrypted, like every other credential this repo holds.
        assert "generated-pw" not in stored.token_blob
        assert decrypt_json(stored.token_blob)["password"] == "generated-pw"

    def test_ageing_out_keeps_the_evidence_the_ticket_is_made_of(
        self, session, account, candidate, aliased
    ):
        """`expire_parked` builds a result carrying only a method and an error,
        and `_finish` used to assign every field from it unconditionally -- so a
        parked Workday attempt lost its screenshot, its run log, its tenant and
        the sealed password for a portal account that still exists on the
        employer's side, and the needs_human ticket had nothing in it."""
        from datetime import timedelta

        from juicebox.applications.submission import expire_parked

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitting", actor="agent")
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="workday",
                method="browser",
                status="needs_verification",
                confirmation_ref="workday gs: awaiting email verification",
                artifacts=["orgs/o/apps/a/workday-account.png"],
                verification={
                    "stage": "account_created",
                    "tenant": "gs",
                    "secret_blob": "encrypted-blob-here",
                    "run": {"steps": ["browser opened"]},
                },
                started_at=utcnow() - timedelta(days=3),
            )
        )
        session.flush()

        assert expire_parked(session, org_id=account["org_id"]) == 1
        row = session.query(ApplicationSubmission).one()
        assert row.status == "needs_human"
        assert "verification mail" in row.error
        # Everything a person needs to close the account by hand survives.
        assert row.artifacts == ["orgs/o/apps/a/workday-account.png"]
        assert row.verification["tenant"] == "gs"
        assert row.verification["secret_blob"] == "encrypted-blob-here"
        assert "awaiting email verification" in row.confirmation_ref


class TestTheAttemptRowIsOnlyWrittenForARealAttempt:
    """Five refused retries used to spend the whole retry budget."""

    def test_a_refused_budget_writes_no_attempt_row(
        self, session, account, candidate, memory_mode, aliased, monkeypatch
    ):
        """The row went in before the `submitting` move, which can refuse. Each
        retry then left another `running` row nothing sweeps, `attempts_used`
        climbed to the cap, and the application was locked out with "this one
        needs a person" although no adapter ever ran."""
        from juicebox.applications import limits

        monkeypatch.setattr(limits, "SUBMISSIONS_PER_WEEK", 0)
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)

        for _ in range(3):
            with pytest.raises(TransitionError, match="budget"):
                submit_application(session, application)

        assert attempts_used(session, application) == 0
        assert session.query(ApplicationSubmission).count() == 0
        assert application.status == "prepared"

    def test_the_worker_path_honours_the_configured_cap(
        self, session, account, candidate, memory_mode, aliased, monkeypatch
    ):
        """APPLY_MAX_ATTEMPTS was checked only in `request_submission`, so the
        queue's own WORKER_MAX_ATTEMPTS -- a larger, unrelated number -- was the
        real ceiling on browser runs at an employer's form."""
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("APPLY_MAX_ATTEMPTS", "1")
        reset_settings_cache()
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitting", actor="agent")
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="greenhouse",
                method="memory",
                status="failed",
            )
        )
        session.flush()

        result = submit_application(session, application)
        assert "needs a person" in result["skipped"]
        # No second row, and the pursuit is parked where a person will see it
        # rather than left claiming to be in flight.
        assert session.query(ApplicationSubmission).count() == 1
        assert application.status == "prepared"
        assert "needs a person" in application.status_reason
        reset_settings_cache()


class TestTheBudgetCountsWhatIsAlreadyInFlight:
    """state.py:98 -- both gates apply on the way in *and* on the way out.

    Counting only `submitted_at` made everything in `submitting` invisible, so
    an admitted run could be refused at the end of a browser session that had
    already reached the employer.
    """

    def test_an_in_flight_application_spends_the_budget(
        self, session, account, candidate, aliased, monkeypatch
    ):
        from juicebox.applications import limits

        monkeypatch.setattr(limits, "SUBMISSIONS_PER_WEEK", 1)
        first = _ready(
            session,
            account["org_id"],
            candidate,
            _posting(session, account["org_id"], company="Acme", suffix="a"),
        )
        transition(session, first, to_status="submitting", actor="agent")

        second = _ready(
            session,
            account["org_id"],
            candidate,
            _posting(session, account["org_id"], company="Beta", suffix="b"),
        )
        decision = limits.may_submit(session, application=second)
        assert not decision
        assert decision.reason == limits.REASON_SUBMISSION_BUDGET

    def test_a_run_already_admitted_can_still_record_itself(
        self, session, account, candidate, aliased, monkeypatch
    ):
        """The other half: an application must not be refused its own in-flight
        row on the way out, or a submission the employer already has can never
        be written down."""
        from juicebox.applications import limits

        monkeypatch.setattr(limits, "SUBMISSIONS_PER_WEEK", 1)
        application = _ready(
            session,
            account["org_id"],
            candidate,
            _posting(session, account["org_id"], company="Acme", suffix="a"),
        )
        transition(session, application, to_status="submitting", actor="agent")
        assert limits.may_submit(session, application=application)
        transition(session, application, to_status="submitted", actor="agent")
        assert application.status == "submitted"


class TestWorkdayReusesTheAccountItAlreadyMade:
    """`AtsAccount` was written and read nowhere, so every role at one tenant
    minted a new password, typed it into the sign-in page, and reported an
    account created that no verification mail would ever follow."""

    _URL = "https://gs.wd5.myworkdayjobs.com/en-US/Goldman/job/NY/Eng_JR-0098765"

    def test_the_stored_login_reaches_the_adapter(self, session, account, candidate, aliased):
        from juicebox.applications.submission import build_context
        from juicebox.applications.submit.browser.workday import remember_account

        posting = _posting(session, account["org_id"], company="Goldman", url=self._URL, suffix="w")
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        target = detect.target_of(posting)
        remember_account(
            session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            vendor=target.vendor,
            tenant=target.tenant,
            username="anna+abc@goforay.io",
            password="stored-pw",
        )

        ctx = build_context(
            session, application, candidate=candidate, posting=posting, target=target
        )
        assert ctx.portal_username == "anna+abc@goforay.io"
        assert ctx.portal_password == "stored-pw"

    def test_a_greenhouse_run_asks_for_no_portal_account(
        self, session, account, candidate, aliased
    ):
        """Only the vendors that make you register have one to look up."""
        from juicebox.applications.submission import build_context

        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        ctx = build_context(
            session,
            application,
            candidate=candidate,
            posting=posting,
            target=detect.target_of(posting),
        )
        assert ctx.portal_username == "" and ctx.portal_password == ""

    def test_a_held_account_goes_straight_to_leg_two(self, monkeypatch):
        from juicebox.applications.submit.browser import workday as workday_mod

        monkeypatch.setattr(workday_mod.driver, "available", lambda: (True, ""))
        handed: dict = {}

        class _Reusing(workday_mod.WorkdaySubmitter):
            def resume(self, ctx, verification):
                handed.update(verification)
                return SubmissionResult(ok=True, method=self.method, confirmation_ref="done")

        ctx = TestAdapters._ctx("workday", self._URL)
        ctx.portal_username = "anna+abc@goforay.io"
        ctx.portal_password = "stored-pw"
        ctx.portal_account_verified = True  # a login we already registered
        result = _Reusing().submit(ctx)

        assert result.ok
        assert handed == {"username": "anna+abc@goforay.io", "password": "stored-pw"}
        # And nothing parks: there is no second account, so no second
        # verification mail is coming to un-park it.
        assert not result.needs_verification

    @staticmethod
    def _fake_browser(monkeypatch, *, fills: dict[str, str]):
        """A driver that lands on whatever page ``fills`` describes."""
        import contextlib

        from juicebox.applications.submit.browser import workday as workday_mod

        class _Page:
            def goto(self, url, **kwargs):
                return None

            def wait_for_timeout(self, ms):
                return None

        @contextlib.contextmanager
        def fake_session(*, log):
            yield _Page()

        def fake_fill(page, specs, *, log):
            for spec in specs:
                if spec.key in fills:
                    log.filled[spec.key] = fills[spec.key]
            return [spec.key for spec in specs if spec.required and spec.key not in fills]

        monkeypatch.setattr(workday_mod.driver, "available", lambda: (True, ""))
        monkeypatch.setattr(workday_mod.driver, "session", fake_session)
        monkeypatch.setattr(workday_mod.driver, "fill", fake_fill)
        monkeypatch.setattr(workday_mod.driver, "click", lambda *a, **k: True)
        monkeypatch.setattr(workday_mod.driver, "snapshot", lambda *a, **k: "")
        # `_settle` polls the real page for the SPA render; the fake page has
        # no clock, and the "page" here is whatever `fills` describes anyway.
        monkeypatch.setattr(workday_mod.driver, "wait_for_any", lambda *a, **k: "")
        return workday_mod

    def test_a_sign_in_page_is_not_reported_as_an_account_created(self, monkeypatch):
        """The second role at a tenant lands here. Both inputs exist, so nothing
        reads as missing -- and the run used to answer `needs_verification`,
        which left the pursuit showing as being sent right now until it aged
        out, with no mail ever coming."""
        workday_mod = self._fake_browser(
            monkeypatch, fills={"email": "anna+abc@goforay.io", "password": "<redacted>"}
        )
        ctx = TestAdapters._ctx("workday", self._URL)
        ctx.portal_password = "candidate-pick"  # vault credential, not yet registered
        result = workday_mod.WorkdaySubmitter().submit(ctx)

        assert not result.needs_verification
        assert result.needs_human
        assert "sign-in form" in result.error

    def test_the_registration_form_still_parks_for_its_mail(self, monkeypatch):
        """The confirm-password box is the evidence, and it is only on the
        registration form."""
        workday_mod = self._fake_browser(
            monkeypatch,
            fills={
                "email": "anna+abc@goforay.io",
                "password": "<redacted>",
                "verify_password": "<redacted>",
                "terms": "1",
            },
        )
        ctx = TestAdapters._ctx("workday", self._URL)
        ctx.portal_password = "candidate-pick"  # vault credential we register with
        result = workday_mod.WorkdaySubmitter().submit(ctx)

        assert result.needs_verification
        assert result.verification["stage"] == "account_created"


class TestEmployerMailIsMatchedToTheRightPursuit:
    """A vendor's domain is the same domain for every employer on it."""

    _ASHBY = "https://jobs.ashbyhq.com/{tenant}/9f8e7d6c-5b4a-3210-fedc-ba98765432{n}"

    def _at_ashby(self, session, account, candidate, company, n):
        posting = _posting(
            session,
            account["org_id"],
            company=company,
            url=self._ASHBY.format(tenant=company.lower(), n=n),
            suffix=n,
        )
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        transition(session, application, to_status="submitted", actor="candidate")
        return application

    def test_a_vendor_domain_cannot_choose_between_two_pursuits_behind_it(
        self, session, account, candidate, aliased
    ):
        """Acme's rejection from no-reply@ashbyhq.com matched Beta's apply_url,
        and the newest row won: Beta closed out, its pipeline stage moved, and
        the candidate told a company that never rejected them had."""
        from juicebox.applications.inbox import handle

        acme = self._at_ashby(session, account, candidate, "Acme", "10")
        beta = self._at_ashby(session, account, candidate, "Beta", "11")
        row = ensure(session, candidate)

        outcome = handle(
            session,
            org_id=account["org_id"],
            to_addresses=(row.address,),
            from_address="no-reply@ashbyhq.com",
            subject="Update on your application",
            body="We have decided to pursue other candidates.",
        )
        assert outcome["application_id"] == ""
        assert "action" not in outcome
        assert acme.status == "submitted"
        assert beta.status == "submitted"

    def test_the_employers_own_domain_still_moves_its_pursuit(
        self, session, account, candidate, aliased
    ):
        from juicebox.applications.inbox import handle

        acme = self._at_ashby(session, account, candidate, "Acme", "10")
        beta = self._at_ashby(session, account, candidate, "Beta", "11")
        row = ensure(session, candidate)

        handle(
            session,
            org_id=account["org_id"],
            to_addresses=(row.address,),
            from_address="no-reply@acme.com",
            subject="Update on your application",
            body="We have decided to pursue other candidates.",
        )
        assert acme.status == "rejected"
        assert beta.status == "submitted"

    def test_one_pursuit_at_that_vendor_is_still_unambiguous(
        self, session, account, candidate, aliased
    ):
        """The same rule the single-open-pursuit fallback already uses: a
        greenhouse pursuit is not a candidate for mail from ashbyhq.com."""
        from juicebox.applications.inbox import handle

        acme = self._at_ashby(session, account, candidate, "Acme", "10")
        elsewhere = _posting(session, account["org_id"], company="Stripe", suffix="12")
        detect.resolve_target(session, elsewhere, allow_network=False)
        other = _ready(session, account["org_id"], candidate, elsewhere)
        transition(session, other, to_status="submitted", actor="candidate")
        row = ensure(session, candidate)

        handle(
            session,
            org_id=account["org_id"],
            to_addresses=(row.address,),
            from_address="no-reply@ashbyhq.com",
            subject="Update on your application",
            body="We have decided to pursue other candidates.",
        )
        assert acme.status == "rejected"
        assert other.status == "submitted"


class TestTheFieldMapsAreCoherent:
    """The maps are the part most likely to be wrong, and `ats-probe` is what
    checks them against reality. These only check they are internally sane --
    that a required field has selectors to try, and that the adapter knows how
    to fill every field the map declares."""

    def test_every_required_field_has_selectors(self):
        from juicebox.applications.submit.browser.forms import PROBEABLE

        for vendor, spec in PROBEABLE.items():
            for key in spec.required:
                assert spec.selectors_for(key), f"{vendor}.{key} is required with no selectors"
            assert spec.submit, f"{vendor} has no submit control"
            assert spec.landmark, f"{vendor} has no landmark to prove we reached a form"

    def test_the_adapter_can_value_every_field_the_map_declares(self):
        from juicebox.applications.submit.browser.forms import WORKDAY_ACCOUNT, YC_ACCOUNT

        for spec in (WORKDAY_ACCOUNT, YC_ACCOUNT):
            assert spec.fields
            assert spec.submit
            for key in spec.required:
                assert spec.selectors_for(key)


class TestAccountGatedWaitsForVault:
    """Model B2: Workday/YC hold for a candidate-chosen vault password rather
    than self-registering behind an email loop we cannot read."""

    _WD = "https://acme.wd1.myworkdayjobs.com/en-US/acme/job/US/Analyst_R99"

    def test_no_credential_prompts_the_vault_and_does_not_submit(
        self, session, account, candidate, memory_mode, monkeypatch
    ):
        from juicebox.applications import submission as sub
        from juicebox.applications.ats import detect

        posting = _posting(session, account["org_id"], company="Acme", url=self._WD, suffix="wd")
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)

        prompted: list = []
        monkeypatch.setattr(
            sub, "prompt_vault_credential", lambda db, app, target: prompted.append(app.id)
        )
        job, reason = sub.auto_submit(session, application)

        assert job is None
        assert "vault credential" in reason
        assert prompted == [application.id]

    def test_a_stored_credential_lets_it_proceed(
        self, session, account, candidate, memory_mode
    ):
        from juicebox.applications import submission as sub
        from juicebox.applications.ats import detect
        from juicebox.applications.candidate_credentials import save_password

        posting = _posting(session, account["org_id"], company="Acme", url=self._WD, suffix="wd2")
        target = detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        save_password(
            session, org_id=account["org_id"], candidate_id=candidate.id,
            vendor=target.vendor, tenant=sub._account_tenant(target),
            username="anna+x@goforay.io", password="candidate-pick", origin="candidate_supplied",
            registered=False,
        )

        job, reason = sub.auto_submit(session, application)
        assert job is not None, reason
