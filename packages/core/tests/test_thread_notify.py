"""Proactive sends: every gate, with a frozen clock.

A notification is a message nobody asked for, so the tests here are mostly
about what does NOT go out: outside the window, over budget, past the
circuit breaker, into a stopped thread, or announcing nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from juicebox.applications.service import create_application
from juicebox.applications.state import transition
from juicebox.intake.schema import empty_record
from juicebox.jobs import KIND_THREAD_NOTIFY, Job
from juicebox.messaging.channels import (
    LINQ,
    DeliveryResult,
    FakeChannel,
    OutboundMessage,
    set_channel,
)
from juicebox.messaging.manager import MODE_MANAGING
from juicebox.messaging.notify import (
    EVENT_APPLICATION_SENT,
    EVENT_MATCHES_READY,
    EVENT_PACKAGE_READY,
    EVENT_PROFILE_READY,
    EVENT_STATUS_CHANGED,
    enqueue_notify,
    manager_thread_for,
    notify_candidate,
)
from juicebox.messaging.suppression import handle_stop
from juicebox.messaging.threads import open_thread, record_inbound, record_outbound
from juicebox.models import IntakeMessage, JobMatch, JobPosting, JobProposal, utcnow
from juicebox.worker import process_job

#: The card head joins title and company with an em dash. Spelled as an
#: escape because a literal one here has been mangled by tooling before.
EM_DASH = "—"

# Winter, so Eastern is EST: the unknown-zone window (11:00-21:00 ET) is
# 16:00Z-02:00Z. Inside, and outside.
IN_WINDOW = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)
OUT_OF_WINDOW = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)

STRONG = {
    "name": "title", "label": "Title", "kind": "title", "weight": 1.0,
    "status": "strong", "score": 0.95, "evidence": "Current title matches", "sources": [],
}


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    row = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-notify",
        entry_point="linq_inbound",
        from_address="+14155550142",
        consent_messaging=True,
        mode=MODE_MANAGING,
        contact_name="Ada Okonkwo",
        contact_email="ada-notify@example.com",
    )
    record = empty_record()
    record["spec"].update({"job_titles": ["ml engineer"], "locations": ["san francisco"]})
    row.record = record
    from juicebox.messaging import identity

    identity.link_candidate(session, thread=row)
    session.flush()
    return row


def _posting(session, org_id, *, company="Anthropic", suffix="1"):
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"notify-{company.lower()}-{suffix}",
        title="ML Engineer",
        company=company,
        company_normalized=company.lower(),
        jd_text="Build things.",
        status="open",
        # Pre-resolved to a supported vendor: the proposal gate only cards
        # roles an adapter can submit to, and resolving here would hit the
        # network. The stamp makes resolve_target answer from the row.
        ats_vendor="greenhouse",
        ats_resolved_at=utcnow(),
    )
    session.add(posting)
    session.flush()
    return posting


def _match(session, org_id, thread, posting, *, score=0.9):
    match = JobMatch(
        org_id=org_id,
        job_posting_id=posting.id,
        candidate_id=thread.candidate_id,
        score=score,
        criteria=[STRONG],
        status="new",
    )
    session.add(match)
    session.flush()
    return match


def _run(session, job, now=IN_WINDOW):
    job.status = "running"
    process_job(session, job, now=now)
    return job


class TestGates:
    def test_quiet_hours_defer_to_the_window(self, session, account, channel, thread):
        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="x")
        _run(session, job, now=OUT_OF_WINDOW)
        assert job.status == "queued"
        run_at = job.run_at if job.run_at.tzinfo else job.run_at.replace(tzinfo=UTC)
        assert run_at == datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
        assert channel.bodies == []

    def test_stopped_thread_gets_nothing(self, session, account, channel, thread):
        handle_stop(session, thread=thread)
        channel.sent.clear()
        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="x")
        _run(session, job)
        assert channel.bodies == []

    def test_intake_threads_take_no_notifications(self, session, account, channel, thread):
        thread.mode = "intake"
        session.flush()
        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="x")
        _run(session, job)
        assert channel.bodies == []

    def test_earlier_notifications_do_not_spend_a_budget(self, session, account, channel, thread):
        """The nudge budget counts nudges, not notifications. When it counted
        every proactive row, two early notifications silenced the third
        application-sent text on the thread forever."""
        for index in range(2):
            row = record_outbound(
                session,
                thread=thread,
                message=OutboundMessage(body=f"earlier notification {index}"),
                result=DeliveryResult(status="sent"),
                origin="proactive",
            )
            row.sent_at = IN_WINDOW - timedelta(hours=1 + index)
        session.flush()
        channel.sent.clear()

        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="x")
        _run(session, job)
        assert job.status == "succeeded"
        assert channel.bodies != []

    def test_circuit_breaker_pauses_the_org(self, session, account, channel, thread):
        from juicebox.messaging.threads import STATUS_STOPPED, close_thread

        for index in range(5):
            victim = open_thread(
                session,
                org_id=account["org_id"],
                channel=LINQ,
                provider="linq",
                provider_thread_id=f"chat-optout-{index}",
                from_address=f"+1415555030{index}",
                consent_messaging=True,
            )
            close_thread(session, thread=victim, status=STATUS_STOPPED, reason="opted out")
            victim.completed_at = IN_WINDOW - timedelta(hours=2)
        session.flush()
        channel.sent.clear()

        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="x")
        _run(session, job)
        assert channel.bodies == []

    def test_event_key_collapses_duplicates(self, session, account, channel, thread):
        first = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="s")
        second = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="s")
        assert first.id == second.id


class TestEvents:
    def test_profile_ready_sends_cards_when_there_are_any(
        self, session, account, channel, thread
    ):
        posting = _posting(session, account["org_id"])
        _match(session, account["org_id"], thread, posting)
        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="s")
        _run(session, job)

        # One text card per role; the last body is the follow-on prompt.
        body = "\n".join(channel.bodies)
        assert f"ML Engineer {EM_DASH} Anthropic" in body
        assert "[contact card]" not in body
        # Card lines only: the portal link is a JWT whose random bytes contain
        # "0.9" about 1.5% of the time (see test_messaging_manager._card_lines).
        card_lines = "\n".join(line for line in body.splitlines() if "/m/" not in line)
        assert "0.9" not in card_lines
        # Recorded as proactive: a message nobody asked for, on the audit trail.
        row = session.scalar(
            select(IntakeMessage)
            .where(IntakeMessage.thread_id == thread.id, IntakeMessage.origin == "proactive")
        )
        assert row is not None
        assert session.scalar(select(JobProposal)) is not None

    def test_profile_ready_falls_back_to_the_canned_line(
        self, session, account, channel, thread
    ):
        job = enqueue_notify(session, thread=thread, event=EVENT_PROFILE_READY, subject_id="s")
        _run(session, job)
        assert "scanning for roles" in channel.bodies[-1]

    def test_matches_ready_stays_silent_with_nothing_to_say(
        self, session, account, channel, thread
    ):
        """An unprompted 'nothing new' text spends attention on nothing."""
        job = enqueue_notify(session, thread=thread, event=EVENT_MATCHES_READY, subject_id="s")
        _run(session, job)
        assert channel.bodies == []

    def test_package_ready_links_to_the_portal(self, session, account, channel, thread):
        posting = _posting(session, account["org_id"], company="Figma", suffix="pkg")
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=thread.candidate_id,
            job_posting_id=posting.id,
        )
        session.flush()
        job = enqueue_notify(
            session, thread=thread, event=EVENT_PACKAGE_READY, subject_id=application.id
        )
        _run(session, job)
        ready = channel.bodies[0]
        assert "Figma" in ready and "/m/" in ready
        # Follow-up questions ride behind the ready message. Chat asks only
        # work authorization; other blanks stay off this text.
        if len(channel.bodies) > 1:
            assert "ready to review" in ready
            follow = channel.bodies[1].lower()
            assert "authoris" in follow or "sponsor" in follow
            assert "comp" not in follow

    def test_application_sent_reacts_like_on_the_consenting_inbound(
        self, session, account, channel, thread
    ):
        posting = _posting(session, account["org_id"], company="Notion", suffix="sent")
        inbound, _ = record_inbound(
            session, thread=thread, body="apply to 1", provider_native_id="in-apply"
        )
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=thread.candidate_id,
            job_posting_id=posting.id,
            consenting_message_id=inbound.id,
            consent_verbatim="apply to 1",
        )
        session.flush()
        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert any("Notion" in b for b in channel.bodies)
        assert ("in-apply", "like") in channel.reactions

    def test_application_sent_does_not_react_to_our_own_outbound(
        self, session, account, channel, thread
    ):
        posting = _posting(session, account["org_id"], company="Linear", suffix="out")
        outbound = record_outbound(
            session,
            thread=thread,
            message=OutboundMessage(body="a card"),
            result=DeliveryResult(status="sent", provider_native_id="out-card"),
        )
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=thread.candidate_id,
            job_posting_id=posting.id,
            consenting_message_id=outbound.id,
        )
        session.flush()
        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert any("Linear" in b for b in channel.bodies)
        assert channel.reactions == []

    def test_recruiter_transition_notifies_the_candidate(
        self, session, account, channel, thread
    ):
        posting = _posting(session, account["org_id"], company="Stripe", suffix="tr")
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=thread.candidate_id,
            job_posting_id=posting.id,
        )
        transition(session, application, to_status="prepared", actor="agent")
        application.approved_at = IN_WINDOW
        transition(session, application, to_status="submitted", actor="candidate")
        # A candidate's own action is not news to them.
        assert not session.scalars(
            select(Job).where(Job.kind == KIND_THREAD_NOTIFY)
        ).all()

        transition(session, application, to_status="interviewing", actor="recruiter")
        job = session.scalar(select(Job).where(Job.kind == KIND_THREAD_NOTIFY))
        assert job is not None
        _run(session, job)
        assert "interviewing" in channel.bodies[-1]


class TestThreadLookup:
    def test_only_manager_threads_receive(self, session, account, channel, thread):
        candidate_id = thread.candidate_id
        assert (
            manager_thread_for(
                session, org_id=account["org_id"], candidate_id=candidate_id
            ).id
            == thread.id
        )
        thread.mode = "intake"
        session.flush()
        assert (
            manager_thread_for(session, org_id=account["org_id"], candidate_id=candidate_id)
            is None
        )
        assert (
            notify_candidate(
                session,
                org_id=account["org_id"],
                candidate_id=candidate_id,
                event=EVENT_STATUS_CHANGED,
            )
            is None
        )


class TestConfirmationScreenshot:
    """The receipt PNG rides on the existing application.sent text."""

    def _application(self, session, account, thread, *, suffix="shot"):
        posting = _posting(session, account["org_id"], company="Notion", suffix=suffix)
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=thread.candidate_id,
            job_posting_id=posting.id,
        )
        session.flush()
        return application

    def _succeeded(self, session, account, application, artifacts):
        from juicebox.models import ApplicationSubmission

        row = ApplicationSubmission(
            org_id=account["org_id"],
            application_id=application.id,
            attempt=1,
            vendor="greenhouse",
            method="browser",
            status="succeeded",
            artifacts=list(artifacts),
        )
        session.add(row)
        session.flush()
        return row

    def _uploads(self, monkeypatch, *, refuse=False, boom=False):
        from juicebox.messaging import linq as linq_mod
        from juicebox.messaging.linq import LinqSendError

        calls = []

        def fake(*, filename, content_type, data):
            if boom:
                raise RuntimeError("network")
            if refuse:
                raise LinqSendError("upload refused")
            calls.append(
                {"filename": filename, "content_type": content_type, "size": len(data)}
            )
            return f"att_{len(calls)}"

        monkeypatch.setattr(linq_mod, "upload_attachment", fake)
        return calls

    def _png(self, application, label="confirmation"):
        from juicebox import storage

        relpath = storage.artifact_relpath(
            org_id=application.org_id,
            application_id=application.id,
            label=label,
        )
        storage.save_bytes(relpath, b"png-bytes")
        return relpath

    def test_imessage_attaches_the_confirmation_and_keeps_the_body(
        self, session, account, channel, thread, monkeypatch
    ):
        from juicebox.messaging.linq import SERVICE_IMESSAGE

        thread.last_service = SERVICE_IMESSAGE
        session.flush()
        application = self._application(session, account, thread, suffix="im")
        path = self._png(application)
        self._succeeded(session, account, application, [path])
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)

        assert uploads and uploads[0]["filename"] == "application-sent.png"
        assert len(channel.sent) == 1
        message = channel.sent[0][1]
        assert "Notion" in message.body
        assert "/m/" in message.body
        assert message.media
        assert message.media[0].attachment_id == "att_1"
        assert message.body  # words stay even when the image is attached

    def test_rcs_attaches_too(self, session, account, channel, thread, monkeypatch):
        from juicebox.messaging.linq import SERVICE_RCS

        thread.last_service = SERVICE_RCS
        session.flush()
        application = self._application(session, account, thread, suffix="rcs")
        path = self._png(application)
        self._succeeded(session, account, application, [path])
        self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert channel.sent[0][1].media

    def test_sms_stays_text_only(self, session, account, channel, thread, monkeypatch):
        from juicebox.messaging.linq import SERVICE_SMS

        thread.last_service = SERVICE_SMS
        session.flush()
        application = self._application(session, account, thread, suffix="sms")
        path = self._png(application)
        self._succeeded(session, account, application, [path])
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert "Notion" in channel.bodies[-1]
        assert channel.sent[0][1].media == ()
        assert uploads == []

    def test_empty_last_service_stays_text_only(
        self, session, account, channel, thread, monkeypatch
    ):
        thread.last_service = ""
        session.flush()
        application = self._application(session, account, thread, suffix="unk")
        path = self._png(application)
        self._succeeded(session, account, application, [path])
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert channel.sent[0][1].media == ()
        assert uploads == []

    def test_missing_artifact_is_text_only(
        self, session, account, channel, thread, monkeypatch
    ):
        from juicebox.messaging.linq import SERVICE_IMESSAGE

        thread.last_service = SERVICE_IMESSAGE
        session.flush()
        application = self._application(session, account, thread, suffix="miss")
        self._succeeded(session, account, application, ["org/applications/x/confirmation.png"])
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert "Notion" in channel.bodies[-1]
        assert channel.sent[0][1].media == ()
        assert uploads == []

    def test_upload_failure_is_text_only(
        self, session, account, channel, thread, monkeypatch
    ):
        from juicebox.messaging.linq import SERVICE_IMESSAGE

        thread.last_service = SERVICE_IMESSAGE
        session.flush()
        application = self._application(session, account, thread, suffix="up")
        path = self._png(application)
        self._succeeded(session, account, application, [path])
        self._uploads(monkeypatch, refuse=True)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert "Notion" in channel.bodies[-1]
        assert channel.sent[0][1].media == ()

    def test_unconfirmed_shot_is_not_sent(
        self, session, account, channel, thread, monkeypatch
    ):
        from juicebox.messaging.linq import SERVICE_IMESSAGE

        thread.last_service = SERVICE_IMESSAGE
        session.flush()
        application = self._application(session, account, thread, suffix="unc")
        path = self._png(application, label="unconfirmed")
        self._succeeded(session, account, application, [path])
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert channel.sent[0][1].media == ()
        assert uploads == []

    def test_failed_attempt_attaches_nothing(
        self, session, account, channel, thread, monkeypatch
    ):
        from juicebox.messaging.linq import SERVICE_IMESSAGE
        from juicebox.models import ApplicationSubmission

        thread.last_service = SERVICE_IMESSAGE
        session.flush()
        application = self._application(session, account, thread, suffix="fail")
        path = self._png(application)
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                attempt=1,
                vendor="greenhouse",
                method="browser",
                status="needs_human",
                artifacts=[path],
            )
        )
        session.flush()
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert channel.sent[0][1].media == ()
        assert uploads == []

    def test_before_submit_shot_is_not_sent(
        self, session, account, channel, thread, monkeypatch
    ):
        from juicebox.messaging.linq import SERVICE_IMESSAGE

        thread.last_service = SERVICE_IMESSAGE
        session.flush()
        application = self._application(session, account, thread, suffix="bef")
        path = self._png(application, label="before-submit")
        self._succeeded(
            session,
            account,
            application,
            [path, f"{application.org_id}/applications/{application.id}/unconfirmed.png"],
        )
        uploads = self._uploads(monkeypatch)

        job = enqueue_notify(
            session, thread=thread, event=EVENT_APPLICATION_SENT, subject_id=application.id
        )
        _run(session, job)
        assert channel.sent[0][1].media == ()
        assert uploads == []
