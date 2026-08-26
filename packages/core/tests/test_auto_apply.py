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
from juicebox.models import Application, ApplicationSubmission, Candidate, JobPosting, utcnow
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

    def test_the_first_application_waits_for_a_person(
        self, session, account, candidate, memory_mode
    ):
        posting = _posting(session, account["org_id"])
        detect.resolve_target(session, posting, allow_network=False)
        application = _ready(session, account["org_id"], candidate, posting)
        assert "first application" in first_submission_gate(session, application)
        job, reason = auto_submit(session, application)
        assert job is None
        assert "first application" in reason
        # Explicitly requested, it still goes -- the gate is on the automatic
        # path, not on the candidate's own decision.
        assert request_submission(session, application) is not None

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

        # A prior submission, so the first-application review gate is not what
        # this test is measuring.
        earlier = _posting(session, account["org_id"], company="Stripe", suffix="9")
        prior = _ready(session, account["org_id"], candidate, earlier)
        transition(session, prior, to_status="submitted", actor="candidate")

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

    def test_a_handoff_still_tells_the_candidate_their_package_is_ready(
        self, session, account, candidate, memory_mode
    ):
        from juicebox.jobs import KIND_APP_PACKAGE, enqueue
        from juicebox.worker import _handle_app_package

        earlier = _posting(session, account["org_id"], company="Stripe", suffix="9")
        prior = _ready(session, account["org_id"], candidate, earlier)
        transition(session, prior, to_status="submitted", actor="candidate")

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

    def test_no_key_means_the_hosted_form(self):
        from juicebox.applications.submit.browser.hosted_form import HostedFormSubmitter

        target = detect.detect_target("https://boards.greenhouse.io/anthropic/jobs/4012345")
        chosen = registry.get_submitter(target)
        assert isinstance(chosen, HostedFormSubmitter)
        assert chosen.method == "browser"

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
        from juicebox.applications.submit.browser.forms import ASHBY, GREENHOUSE, LEVER
        from juicebox.applications.submit.browser.hosted_form import _values, specs_for

        ctx = TestAdapters._ctx("greenhouse", "https://boards.greenhouse.io/x/jobs/1")
        values = _values(ctx, "/tmp/resume.pdf")
        for spec in (GREENHOUSE, LEVER, ASHBY):
            built = specs_for(spec, values)
            assert {row.key for row in built} == set(spec.fields)
            # Every required field resolves to something we actually hold.
            for row in built:
                if row.required:
                    assert row.value, f"{spec.vendor}.{row.key} is required but we have no value"
