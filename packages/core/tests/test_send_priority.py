"""Which steps go first when the send line is backed up."""

from __future__ import annotations

from datetime import timedelta

from juicebox.jobs import KIND_OUTREACH_TICK
from juicebox.models import Job, Org, utcnow
from juicebox.outreach.sequencer import (
    enroll_candidates,
    move_in_line,
    open_send_jobs,
)
from juicebox.worker import _handle_outreach_tick


def _tick(session, org_id, now):
    _handle_outreach_tick(
        session, Job(org_id=org_id, kind=KIND_OUTREACH_TICK, payload={"org_id": org_id}), now
    )
    session.commit()


def _order(session, org_id):
    return [job.payload["enrollment_id"] for job in open_send_jobs(session, org_id)]


def _set_priority(session, org_id, value):
    session.get(Org, org_id).send_priority = value
    session.commit()


def _mixed_line(session, fixture, now):
    """A (follow-up, due first), B (first email), C (follow-up, due last)."""
    sequence = fixture["sequence"]
    a, b, c = enroll_candidates(
        session,
        sequence=sequence,
        candidate_ids=[x.id for x in fixture["candidates"][:3]],
        project_id=sequence.project_id,
    )
    a.current_step, b.current_step, c.current_step = 1, 0, 1
    a.next_send_at = now - timedelta(minutes=30)
    b.next_send_at = now - timedelta(minutes=20)
    c.next_send_at = now - timedelta(minutes=10)
    session.commit()
    return a, b, c


class TestTheDefault:
    def test_first_emails_go_ahead_of_follow_ups(self, session, outreach_fixture, mailbox):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        a, b, c = _mixed_line(session, outreach_fixture, now)

        _tick(session, org_id, now)

        assert _order(session, org_id) == [b.id, a.id, c.id]

    def test_a_new_first_email_joins_behind_first_emails_but_ahead_of_follow_ups(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        a, b, c = _mixed_line(session, outreach_fixture, now)
        b.next_send_at = now + timedelta(hours=1)  # not due yet
        session.commit()
        _tick(session, org_id, now)
        assert _order(session, org_id) == [a.id, c.id]

        b.next_send_at = now
        session.commit()
        _tick(session, org_id, now + timedelta(minutes=1))

        assert _order(session, org_id) == [b.id, a.id, c.id]


class TestTheOtherSettings:
    def test_queue_order_is_plain_due_order(self, session, outreach_fixture, mailbox):
        org_id = outreach_fixture["sequence"].org_id
        _set_priority(session, org_id, "queue")
        now = utcnow()
        a, b, c = _mixed_line(session, outreach_fixture, now)

        _tick(session, org_id, now)

        assert _order(session, org_id) == [a.id, b.id, c.id]

    def test_follow_ups_first(self, session, outreach_fixture, mailbox):
        org_id = outreach_fixture["sequence"].org_id
        _set_priority(session, org_id, "follow_ups")
        now = utcnow()
        a, b, c = _mixed_line(session, outreach_fixture, now)

        _tick(session, org_id, now)

        assert _order(session, org_id) == [a.id, c.id, b.id]


class TestHandMovesSurvive:
    def test_a_row_moved_to_the_bottom_stays_there_as_the_line_grows(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        a, b, c = _mixed_line(session, outreach_fixture, now)
        a.current_step = c.current_step = 0
        c.next_send_at = now + timedelta(hours=1)
        session.commit()
        _tick(session, org_id, now)
        move_in_line(session, org_id, enrollment_ids=[a.id], to="bottom", now=now)
        session.commit()

        c.next_send_at = now
        session.commit()
        _tick(session, org_id, now + timedelta(minutes=1))

        assert _order(session, org_id) == [b.id, c.id, a.id]


class TestChangingTheSetting:
    def test_the_endpoint_resorts_the_line_and_leaves_the_pause_alone(
        self, client, account, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        a, b, c = _mixed_line(session, outreach_fixture, now)
        _tick(session, org_id, now)
        assert _order(session, org_id) == [b.id, a.id, c.id]

        response = client.patch(
            "/v1/org/sending", json={"send_priority": "follow_ups"}, headers=account["headers"]
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["send_priority"] == "follow_ups"
        assert body["paused"] is False
        session.expire_all()
        assert _order(session, org_id) == [a.id, c.id, b.id]

    def test_pausing_still_works_without_a_priority(self, client, account):
        response = client.patch("/v1/org/sending", json={"paused": True}, headers=account["headers"])
        assert response.status_code == 200
        assert response.json()["paused"] is True
        assert response.json()["send_priority"] == "first_emails"
