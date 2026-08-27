"""Answering an application's questions over text, and the drift it used to
cause.

The reported sequence, verbatim in shape: the candidate applied to a founding
engineer role, the form wanted comp expectations and work authorisation, they
answered both, and the agent said "noted, thanks. anything else you want me
looking for?" -- then read the following "no" as declining a job search and
replied that it would go and look for roles.

Two separate faults, and the conversational one was the smaller:

* The reply named no role and described nothing that had happened, because the
  answer was classified as a generic profile answer with no idea an application
  was waiting on it.
* ``refill_from_preference`` -- the function that folds an answer into the
  application that asked -- was only ever called when the portal served that
  application. On the text path it never ran, so the form stayed blank and
  answering achieved nothing at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from test_messaging_manager import STRONG, _managed, _posting, _say

from juicebox.applications import followups
from juicebox.applications.service import create_application
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.threads import open_thread
from juicebox.models import Application, CandidatePreference, JobMatch


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
        provider_thread_id="chat-answers",
        entry_point="linq_inbound",
        from_address="+15134333227",
        consent_messaging=True,
    )


def _asked_application(session, account, thread, *, company="Sourcebot", keys=None):
    """An application that has asked over text and is waiting, as
    ``ask_for_missing`` leaves one."""
    keys = keys or ["salary_expectation", "work_authorization"]
    posting = _posting(
        session, account["org_id"], company=company, title="Founding Engineer", suffix="ans"
    )
    session.add(
        JobMatch(
            org_id=account["org_id"],
            job_posting_id=posting.id,
            candidate_id=thread.candidate_id,
            score=0.9,
            criteria=[STRONG],
            status="new",
        )
    )
    session.flush()
    application = create_application(
        session,
        org_id=account["org_id"],
        candidate_id=thread.candidate_id,
        job_posting_id=posting.id,
        source="candidate",
    )
    application.status = "prepared"
    # The state ask_for_missing leaves behind: asked, and still blank.
    application.form_answers = [
        {"key": key, "question": followups.ASKABLE[key], "answer": "", "asked": True}
        for key in keys
    ]
    session.flush()
    return application, posting


def _managed_owing_answers(session, thread):
    """A managed thread whose candidate has NOT given comp or work auth.

    `_managed` seeds `_core_complete_record`, which fills `target_base`,
    `work_authorization` and `timeline` -- so every question the form could ask
    is already answerable and none of these tests would exercise anything.
    Clearing them is what a text-onboarded candidate actually looks like now:
    onboarding stops after the profile confirm and no longer collects comp,
    notice period or work auth in the abstract.
    """
    thread = _managed(session, thread)
    record = dict(thread.record or {})
    record["target_base"] = None
    record["target_total"] = None
    record["work_authorization"] = ""
    record["timeline"] = ""
    thread.record = record
    session.flush()
    # `_capture` wrote the preference from the record before we cleared it.
    preference = session.scalar(
        select(CandidatePreference).where(
            CandidatePreference.candidate_id == thread.candidate_id
        )
    )
    if preference is not None:
        preference.target_base = None
        preference.target_total = None
        preference.work_authorization = ""
        preference.timeline = ""
        session.flush()
    return thread


def _extracted(session, thread, *, comp=None, work_auth=None):
    """Simulate the extractor having understood the candidate's reply.

    Written onto `thread.record`, not straight onto the preference, because
    that is the direction production runs in: `_reread` updates the record and
    `_capture` persists it. Writing the preference directly would be undone by
    `_capture` on the very next turn -- which is how the first draft of these
    tests quietly passed against answers nobody had given.
    """
    record = dict(thread.record or {})
    if comp is not None:
        record["target_base"] = float(comp)
        record["comp_currency"] = "USD"
    if work_auth is not None:
        record["work_authorization"] = work_auth
    thread.record = record
    session.flush()


class TestDetection:
    def test_an_asked_and_blank_application_is_waiting(
        self, session, account, channel, thread
    ):
        thread = _managed_owing_answers(session, thread)
        application, _ = _asked_application(session, account, thread)
        found = followups.awaiting_text_answers(
            session, org_id=account["org_id"], candidate_id=thread.candidate_id
        )
        assert found is not None and found.id == application.id

    def test_an_application_that_never_asked_is_not_waiting(
        self, session, account, channel, thread
    ):
        """The guard that stops this becoming a catch-all.

        A package that filled everything from the preference asked nothing, so
        a blank on it must not capture the next unrelated thing the candidate
        says.
        """
        thread = _managed_owing_answers(session, thread)
        application, _ = _asked_application(session, account, thread)
        application.form_answers = [
            {"key": "salary_expectation", "answer": "", "asked": False}
        ]
        session.flush()
        assert (
            followups.awaiting_text_answers(
                session, org_id=account["org_id"], candidate_id=thread.candidate_id
            )
            is None
        )

    def test_a_fully_answered_application_is_not_waiting(
        self, session, account, channel, thread
    ):
        thread = _managed_owing_answers(session, thread)
        application, _ = _asked_application(session, account, thread)
        application.form_answers = [
            {"key": "salary_expectation", "answer": "$200k", "asked": True},
            {"key": "work_authorization", "answer": "Authorised", "asked": True},
        ]
        session.flush()
        assert (
            followups.awaiting_text_answers(
                session, org_id=account["org_id"], candidate_id=thread.candidate_id
            )
            is None
        )


class TestTheReportedSequence:
    def test_an_answer_is_acknowledged_against_the_role(
        self, session, account, channel, thread
    ):
        """The fix for the reply half. It must name the role, and it must not
        offer to go and find more work."""
        thread = _managed_owing_answers(session, thread)
        _asked_application(session, account, thread)
        # The preference the extractor writes from "200k, no sponsorship needed".
        _extracted(session, thread, comp=200000, work_auth="green card")

        _say(session, thread, "200k works, and no sponsorship needed", native_id="a-1")

        reply = channel.bodies[-1]
        assert "Sourcebot" in reply
        assert "Founding Engineer" in reply
        # The exact drift that was reported.
        assert "anything else you want me looking for" not in reply.lower()
        assert "go find roles" not in reply.lower()
        assert "look for roles" not in reply.lower()

    def test_the_answers_reach_the_application(
        self, session, account, channel, thread
    ):
        """The fix for the half that mattered more: the form was still blank.

        Before this, `refill_from_preference` ran only when the portal served
        the application, so answering over text moved nothing.
        """
        thread = _managed_owing_answers(session, thread)
        application, _ = _asked_application(session, account, thread)
        _extracted(session, thread, comp=200000, work_auth="green card")

        _say(session, thread, "200k works, and no sponsorship needed", native_id="a-2")

        session.refresh(application)
        answers = {
            item["key"]: item.get("answer") or "" for item in application.form_answers
        }
        assert answers["salary_expectation"].strip()
        assert answers["work_authorization"].strip()
        # `asked_blanks`, not `blank_answers`: the latter reads a key that was
        # never on this form as blank, so it would never report complete.
        assert followups.asked_blanks(application) == []

    def test_a_partial_answer_asks_only_for_what_is_left(
        self, session, account, channel, thread
    ):
        thread = _managed_owing_answers(session, thread)
        _asked_application(session, account, thread)
        _extracted(session, thread, comp=200000)

        _say(session, thread, "200k works", native_id="a-3")

        reply = channel.bodies[-1]
        assert "sponsorship" in reply.lower() or "authorised" in reply.lower()
        assert "comp expectations" not in reply.lower()
        assert "Sourcebot" in reply

    def test_an_unparseable_answer_says_so_instead_of_noted_thanks(
        self, session, account, channel, thread
    ):
        """"noted, thanks" for something we did not understand is the worst of
        the four cases: it claims an answer landed, so the candidate has no
        idea anything is still outstanding."""
        thread = _managed_owing_answers(session, thread)
        _asked_application(session, account, thread)

        _say(session, thread, "hmm depends on the equity tbh", native_id="a-4")

        reply = channel.bodies[-1].lower()
        assert "noted" not in reply
        assert "didn't catch" in reply or "didn t catch" in reply
        assert "sourcebot" in reply

    def test_a_bare_no_is_not_read_as_declining_a_job_search(
        self, session, account, channel, thread
    ):
        """The second half of the reported drift.

        With an application waiting, "no" is a reply to its question -- not a
        decision to stop looking for work. It previously hit INTENT_DECLINE and
        answered "I'll go look for roles and text you what I find."
        """
        thread = _managed_owing_answers(session, thread)
        _asked_application(session, account, thread)

        _say(session, thread, "no", native_id="a-5")

        reply = channel.bodies[-1].lower()
        assert "look for roles" not in reply
        assert "sourcebot" in reply

    def test_with_nothing_waiting_the_ordinary_branches_still_run(
        self, session, account, channel, thread
    ):
        """The regression guard. This must not swallow the conversation
        whenever a candidate has an application on file."""
        thread = _managed_owing_answers(session, thread)
        _say(session, thread, "no thanks", native_id="a-6")
        reply = channel.bodies[-1].lower()
        # MANAGER_ACK_DECLINE / MANAGER_STANDING_BY, as before.
        assert "roles" in reply or "keep an eye out" in reply


class TestAnsweringAdvancesTheApplication:
    def test_completing_the_answers_retries_the_submission(
        self, session, account, channel, thread, monkeypatch
    ):
        """Answering has to *do* something.

        The first attempt ran at package time, before these answers existed,
        and a required blank refuses the run before the browser opens. Without
        a retry here the candidate answers the question and nothing happens --
        which is what the reported sequence actually felt like.
        """
        calls: list[str] = []

        thread = _managed_owing_answers(session, thread)
        application, _ = _asked_application(session, account, thread)
        _extracted(session, thread, comp=200000, work_auth="green card")

        import juicebox.applications.submission as submission_mod

        def fake_auto_submit(db, app):
            calls.append(app.id)
            return None, "console"

        monkeypatch.setattr(submission_mod, "auto_submit", fake_auto_submit)
        _say(session, thread, "200k, and i'm authorised", native_id="a-7")
        assert calls == [application.id]

    def test_a_partial_answer_does_not_try_to_submit(
        self, session, account, channel, thread, monkeypatch
    ):
        """Half an answer is not a complete form, and a submission attempt
        costs an attempt off a capped budget."""
        calls: list[str] = []
        import juicebox.applications.submission as submission_mod

        thread = _managed_owing_answers(session, thread)
        _asked_application(session, account, thread)
        _extracted(session, thread, comp=200000)

        monkeypatch.setattr(
            submission_mod,
            "auto_submit",
            lambda db, app: (calls.append(app.id), (None, "x"))[1],
        )
        _say(session, thread, "200k works", native_id="a-8")
        assert calls == []


class TestGateBlockedCompletionIsHonest:
    def test_a_gated_send_asks_for_the_approve_tap_instead_of_promising(
        self, session, account, thread, channel, monkeypatch
    ):
        """The gate refused the send: "i'll take it from here" followed by
        silence is a lie. Live thread, 2026-08-27: the candidate answered
        "just fire the app looks good" and nothing fired, with no next step
        named. The completion message must carry the approve link instead."""
        import juicebox.applications.submission as submission_mod

        thread = _managed_owing_answers(session, thread)
        _asked_application(session, account, thread, keys=["salary_expectation"])
        _extracted(session, thread, comp=150000, work_auth="us citizen")
        monkeypatch.setattr(
            submission_mod,
            "auto_submit",
            lambda db, app: (None, "first application for this candidate: a person reviews it before it goes"),
        )

        _say(session, thread, "150k works", native_id="gate-1")

        text = channel.bodies[-1]
        assert "take it from here" not in text
        assert "approve" in text.lower()
        assert "/m/" in text, "the approve message must carry the portal link"


class TestAcknowledgementWording:
    def _posting_obj(self, session, account):
        return _posting(
            session,
            account["org_id"],
            company="Sourcebot",
            title="Founding Engineer",
            suffix="w",
        )

    def test_complete_and_nothing_handed_over(self, session, account):
        posting = self._posting_obj(session, account)
        text = followups.answer_acknowledgement(
            Application(), posting, {"filled": ["salary_expectation"], "missing": [], "handover": False}
        )
        assert "Founding Engineer" in text and "Sourcebot" in text
        assert "take it from here" in text

    def test_complete_but_the_portal_still_needs_them(self, session, account):
        posting = self._posting_obj(session, account)
        text = followups.answer_acknowledgement(
            Application(), posting, {"filled": ["salary_expectation"], "missing": [], "handover": True}
        )
        assert "app" in text
        assert "take it from here" not in text

    def test_no_posting_still_reads_as_english(self):
        text = followups.answer_acknowledgement(
            Application(), None, {"filled": ["salary_expectation"], "missing": [], "handover": False}
        )
        assert "your application" in text
