"""Apply by link: a job URL texted in comes back as a filled application.

The contract under test, end to end and per part:

* a URL in a message is an intent of its own — deterministic, keyless, and
  never a LinkedIn profile (those are identity);
* the page behind it becomes a ``JobPosting`` through one transport seam,
  with the real form questions captured when the ATS publishes them;
* the application is opened with the link message as its consent, the
  package answers the form's own questions under the provenance contract,
  and self-identification questions are structurally never answered;
* every dead end (bad link, duplicate, limits) texts the candidate a canned
  line instead of going silent.

Everything here runs with no API key and no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from juicebox.docgen.answers import build_form_answers, question_set
from juicebox.jobmatch import ingest_url
from juicebox.jobmatch.ingest_url import (
    FetchedPage,
    LinkUnreachable,
    ingest_posting_from_url,
)
from juicebox.jobs import KIND_APP_PACKAGE, KIND_APPLY_LINK, KIND_JOBS_PARSE, KIND_THREAD_NOTIFY
from juicebox.messaging import apply_link, cadence
from juicebox.messaging.apply_link import find_job_link
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import run_turn
from juicebox.messaging.intents import INTENT_APPLY_LINK, classify
from juicebox.messaging.manager import MODE_MANAGING
from juicebox.messaging.threads import open_thread, record_inbound
from juicebox.models import Application, CandidatePreference, Job
from juicebox.worker import process_job

GH_URL = "https://boards.greenhouse.io/acme/jobs/4021"
GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4021?questions=true"

GH_JOB = {
    "title": "Staff Software Engineer",
    "company_name": "Acme Robotics",
    "location": {"name": "San Francisco, CA"},
    "content": "&lt;p&gt;Build the control stack for warehouse robots. "
    "You will own services end to end, mentor engineers, and ship weekly. "
    "We value rigorous testing and short feedback loops across the fleet.&lt;/p&gt;",
    "questions": [
        {"label": "Resume", "required": True, "fields": [{"type": "input_file"}]},
        {
            "label": "Do you now or will you in the future require sponsorship?",
            "required": True,
            "fields": [{"type": "multi_value_single_select", "values": [
                {"label": "Yes"}, {"label": "No"},
            ]}],
        },
        {"label": "What draws you to robotics?", "required": False, "fields": [{"type": "textarea"}]},
        {"label": "Gender identity (voluntary self-identification)", "required": False,
         "fields": [{"type": "multi_value_single_select", "values": []}]},
    ],
}

GENERIC_URL = "https://careers.initech.com/openings/tps-lead"
GENERIC_HTML = (
    "<html><head><title>TPS Report Lead - Initech</title>"
    '<meta property="og:site_name" content="Initech Careers"/></head>'
    "<body><script>var x=1;</script><h1>TPS Report Lead</h1>"
    "<p>Initech seeks a lead to own the TPS reporting pipeline. You will "
    "coordinate cover sheets, drive adoption of the new memo format, and "
    "work closely with facilities. Five years of reporting experience "
    "required, and deep familiarity with staplers is a plus.</p></body></html>"
)

# Winter, inside the unknown-timezone send window (16:00Z to 02:00Z).
IN_WINDOW = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)


def fake_transport(pages: dict[str, dict | str]):
    """URL -> FetchedPage, JSON for dict values, HTML for strings."""

    def _fetch(url: str) -> FetchedPage:
        try:
            payload = pages[url]
        except KeyError:
            return FetchedPage(url=url, status=404, content_type="text/html", text="")
        if isinstance(payload, dict):
            return FetchedPage(
                url=url, status=200, content_type="application/json",
                text=json.dumps(payload),
            )
        return FetchedPage(url=url, status=200, content_type="text/html", text=payload)

    return _fetch


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-link",
        entry_point="linq_inbound",
        from_address="+14155550177",
        consent_messaging=True,
    )


@pytest.fixture
def managed(session, thread):
    """Past onboarding: named, linked, managing."""
    from juicebox.messaging import identity

    thread.contact_name = "Lin Okafor"
    thread.contact_email = "lin-link@example.com"
    thread.mode = MODE_MANAGING
    identity.link_candidate(session, thread=thread)
    session.flush()
    assert thread.candidate_id
    return thread


def _say(session, thread, body, *, native_id=""):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _apply_link_jobs(session, org_id):
    return session.scalars(
        select(Job).where(Job.org_id == org_id, Job.kind == KIND_APPLY_LINK)
    ).all()


class TestFindJobLink:
    def test_a_job_url_is_found_and_trailing_punctuation_dropped(self):
        assert find_job_link(f"what about this? {GH_URL}.") == GH_URL

    def test_a_linkedin_profile_is_identity_not_a_job(self):
        assert find_job_link("here you go https://www.linkedin.com/in/lin-okafor") == ""

    def test_a_linkedin_job_posting_still_counts(self):
        url = "https://www.linkedin.com/jobs/view/399001"
        assert find_job_link(url) == url

    def test_classify_puts_the_link_above_every_keyword(self):
        intent = classify(f"apply to this {GH_URL}", presented=[])
        assert intent.kind == INTENT_APPLY_LINK
        assert intent.url == GH_URL


class TestIngest:
    def test_greenhouse_lands_with_the_real_form_questions(self, session, account):
        posting, created = ingest_posting_from_url(
            session, org_id=account["org_id"], url=GH_URL,
            transport=fake_transport({GH_API: GH_JOB}),
        )
        assert created
        assert posting.source == "candidate"
        assert posting.company == "Acme Robotics"
        assert posting.title == "Staff Software Engineer"
        assert posting.location == "San Francisco, CA"
        assert "control stack" in posting.jd_text
        # The file upload never becomes a question; the demographic one is
        # carried but flagged; the sponsorship select keeps its options.
        questions = posting.form_questions
        assert [q["kind"] for q in questions] == ["file", "select", "text", "select"]
        sponsorship = questions[1]
        assert sponsorship["options"] == ["Yes", "No"]
        assert not sponsorship["self_identify"]
        assert questions[3]["self_identify"] is True
        parse_jobs = session.scalars(
            select(Job).where(Job.kind == KIND_JOBS_PARSE)
        ).all()
        assert any(
            (j.payload or {}).get("job_posting_id") == posting.id for j in parse_jobs
        )

    def test_the_same_link_twice_is_one_posting(self, session, account):
        transport = fake_transport({GH_API: GH_JOB})
        first, created_first = ingest_posting_from_url(
            session, org_id=account["org_id"], url=GH_URL, transport=transport
        )
        second, created_second = ingest_posting_from_url(
            session, org_id=account["org_id"], url=GH_URL + "?utm_source=sms",
            transport=transport,
        )
        assert created_first and not created_second
        assert first.id == second.id

    def test_a_generic_page_needs_no_adapter_and_no_model(self, session, account):
        posting, _ = ingest_posting_from_url(
            session, org_id=account["org_id"], url=GENERIC_URL,
            transport=fake_transport({GENERIC_URL: GENERIC_HTML}),
        )
        assert posting.title == "TPS Report Lead"
        assert posting.company == "Initech Careers"
        assert "cover sheets" in posting.jd_text
        assert "var x=1" not in posting.jd_text
        assert posting.form_questions == []

    def test_a_dead_link_refuses_with_a_candidate_safe_reason(self, session, account):
        with pytest.raises(LinkUnreachable):
            ingest_posting_from_url(
                session, org_id=account["org_id"],
                url="https://careers.initech.com/gone",
                transport=fake_transport({}),
            )

    def test_a_page_with_no_readable_description_refuses(self, session, account):
        with pytest.raises(LinkUnreachable):
            ingest_posting_from_url(
                session, org_id=account["org_id"], url=GENERIC_URL,
                transport=fake_transport({GENERIC_URL: "<html><body>404</body></html>"}),
            )

    def test_a_non_link_refuses_before_fetching(self, session, account):
        with pytest.raises(LinkUnreachable):
            ingest_posting_from_url(
                session, org_id=account["org_id"], url="ftp://example.com/job",
                transport=fake_transport({}),
            )


class TestAnswers:
    def _posting(self, session, org_id):
        posting, _ = ingest_posting_from_url(
            session, org_id=org_id, url=GH_URL,
            transport=fake_transport({GH_API: GH_JOB}),
        )
        return posting

    def test_the_forms_own_wording_replaces_the_standard_question(self, session, account):
        posting = self._posting(session, account["org_id"])
        questions = question_set(posting)
        keys = [q["key"] for q in questions]
        # The sponsorship select is the work_authorization question in the
        # employer's words -- same key, their wording.
        assert keys.count("work_authorization") == 1
        work_auth = next(q for q in questions if q["key"] == "work_authorization")
        assert "sponsorship" in work_auth["question"]
        assert work_auth["options"] == ["Yes", "No"]

    def test_custom_questions_stay_blank_rather_than_guessed(self, session, account):
        """No API key: the robotics question has no deterministic source, so
        it ships blank with provenance none -- never invented."""
        posting = self._posting(session, account["org_id"])
        answers, generated_by = build_form_answers(
            preference=None, resume_json={}, posting=posting
        )
        assert generated_by == "template"
        robotics = next(a for a in answers if "robotics" in a["question"])
        assert robotics["answer"] == ""
        assert robotics["provenance"] == "none"

    def test_a_self_identification_question_is_never_answered(self, session, account):
        posting = self._posting(session, account["org_id"])
        answers, _ = build_form_answers(preference=None, resume_json={}, posting=posting)
        gender = next(a for a in answers if a.get("self_identify"))
        assert gender["answer"] == ""
        assert gender["generated_by"] == "policy"

    def test_preference_values_still_fill_the_standard_keys(self, session, account):
        posting = self._posting(session, account["org_id"])
        preference = CandidatePreference(
            org_id=account["org_id"],
            work_authorization="US citizen, no sponsorship needed",
            timeline="under_1_month",
        )
        answers, _ = build_form_answers(
            preference=preference, resume_json={}, posting=posting
        )
        work_auth = next(a for a in answers if a["key"] == "work_authorization")
        assert work_auth["answer"] == "US citizen, no sponsorship needed"
        assert work_auth["provenance"] == "preference:work_authorization"
        # And it is reported under the employer's wording.
        assert "sponsorship" in work_auth["question"]


class TestManagerTurn:
    def test_a_link_is_acked_and_the_job_is_message_scoped(
        self, session, account, managed, channel
    ):
        result = _say(session, managed, f"can you do this one? {GH_URL}", native_id="l1")
        assert result.action == "applied"
        assert "ready to review" in channel.bodies[-1]
        jobs = _apply_link_jobs(session, account["org_id"])
        assert len(jobs) == 1
        message_id = jobs[0].payload["message_id"]
        assert jobs[0].idempotency_key == f"{KIND_APPLY_LINK}:{message_id}"
        assert jobs[0].payload["url"] == GH_URL

    def test_no_identity_means_one_ask_and_a_stash_not_a_job(
        self, session, account, thread, channel
    ):
        thread.mode = MODE_MANAGING
        session.flush()
        result = _say(session, thread, GH_URL, native_id="l2")
        assert result.action == "asked"
        assert "got the link" in channel.bodies[-1].lower()
        assert apply_link.pending(thread)["url"] == GH_URL
        assert _apply_link_jobs(session, account["org_id"]) == []

    def test_the_identity_reply_spends_the_stash(self, session, account, thread, channel):
        thread.mode = MODE_MANAGING
        session.flush()
        _say(session, thread, GH_URL, native_id="l3")
        _say(session, thread, "Lin Okafor, lin-spend@example.com", native_id="l4")
        assert thread.candidate_id
        jobs = _apply_link_jobs(session, account["org_id"])
        assert len(jobs) == 1
        assert jobs[0].payload["url"] == GH_URL

    def test_a_repeat_link_is_told_its_already_in_flight(
        self, session, account, managed, channel, monkeypatch
    ):
        monkeypatch.setattr(ingest_url, "_http_fetch", fake_transport({GH_API: GH_JOB}))
        _say(session, managed, GH_URL, native_id="l5")
        job = _apply_link_jobs(session, account["org_id"])[0]
        process_job(session, job)
        assert session.scalar(select(Application)) is not None

        _say(session, managed, f"one more time {GH_URL}", native_id="l6")
        assert "already in flight" in channel.bodies[-1].lower()
        # Still exactly one apply-link job: the repeat never enqueued.
        assert len(_apply_link_jobs(session, account["org_id"])) == 1


class TestOnboardingStash:
    def test_a_strangers_first_message_can_be_the_link(self, session, thread, channel):
        _say(session, thread, f"hey can you apply me to this {GH_URL}", native_id="s1")
        assert apply_link.pending(thread)["url"] == GH_URL
        # Onboarding still runs: the greeting went out, not an application.
        assert thread.mode != MODE_MANAGING

    def test_handover_spends_the_stash(self, session, account, managed):
        record = dict(managed.record or {})
        record[apply_link.APPLY_META_KEY] = {"url": GH_URL, "message_id": "msg-x"}
        managed.record = record
        session.flush()
        from juicebox.messaging.apply_link import ensure_enqueued

        job = ensure_enqueued(session, managed)
        assert job is not None
        assert job.idempotency_key == f"{KIND_APPLY_LINK}:msg-x"


class TestWorker:
    def _send_link(self, session, managed, *, native_id="w1"):
        _say(session, managed, GH_URL, native_id=native_id)
        return _apply_link_jobs(session, managed.org_id)[0]

    def test_the_link_message_is_the_consent_on_the_application(
        self, session, account, managed, channel, monkeypatch
    ):
        monkeypatch.setattr(ingest_url, "_http_fetch", fake_transport({GH_API: GH_JOB}))
        job = self._send_link(session, managed)
        process_job(session, job)
        application = session.scalar(select(Application))
        assert application is not None
        assert application.source == "link"
        assert application.consenting_message_id == job.payload["message_id"]
        assert GH_URL in (application.consent_verbatim or "")
        # The package build is queued; its notify closes the loop.
        package_jobs = session.scalars(
            select(Job).where(Job.kind == KIND_APP_PACKAGE)
        ).all()
        assert any(
            (j.payload or {}).get("application_id") == application.id
            for j in package_jobs
        )
        assert apply_link.pending(managed) == {}

    def test_an_unreachable_link_texts_the_problem_line(
        self, session, account, managed, channel, monkeypatch
    ):
        monkeypatch.setattr(ingest_url, "_http_fetch", fake_transport({}))
        monkeypatch.setattr(cadence, "utcnow", lambda: IN_WINDOW)
        job = self._send_link(session, managed, native_id="w2")
        process_job(session, job)
        notify_jobs = session.scalars(
            select(Job).where(Job.kind == KIND_THREAD_NOTIFY)
        ).all()
        events = [(j.payload or {}).get("event") for j in notify_jobs]
        assert "link.problem" in events
        assert session.scalar(select(Application)) is None
        assert apply_link.pending(managed) == {}

    def test_a_duplicate_from_the_worker_side_notifies_not_errors(
        self, session, account, managed, channel, monkeypatch
    ):
        monkeypatch.setattr(ingest_url, "_http_fetch", fake_transport({GH_API: GH_JOB}))
        first = self._send_link(session, managed, native_id="w3")
        process_job(session, first)
        # A second job for the same posting (different message) refuses as a
        # duplicate and says so, rather than failing and retrying.
        record_inbound(session, thread=managed, body=GH_URL, provider_native_id="w4")
        from juicebox.messaging.threads import messages_of

        last = messages_of(session, managed)[-1]
        apply_link.stash(managed, url=GH_URL, message_id=last.id)
        job = apply_link.ensure_enqueued(session, managed)
        process_job(session, job)
        notify_jobs = session.scalars(
            select(Job).where(Job.kind == KIND_THREAD_NOTIFY)
        ).all()
        events = [(j.payload or {}).get("event") for j in notify_jobs]
        assert "link.duplicate" in events

    def test_the_problem_notify_sends_the_canned_line(
        self, session, account, managed, channel, monkeypatch
    ):
        monkeypatch.setattr(ingest_url, "_http_fetch", fake_transport({}))
        monkeypatch.setattr(cadence, "utcnow", lambda: IN_WINDOW)
        job = self._send_link(session, managed, native_id="w5")
        process_job(session, job)
        notify_job = session.scalar(
            select(Job).where(Job.kind == KIND_THREAD_NOTIFY)
        )
        assert notify_job is not None
        # handle_notify judges cadence on the worker's `now`, not on
        # cadence.utcnow — pin it inside the send window.
        process_job(session, notify_job, now=IN_WINDOW)
        assert "couldn't open the job link" in channel.bodies[-1].lower()
