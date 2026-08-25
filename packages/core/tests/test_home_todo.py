"""What Home reads: the to-do feed, and an activity row you can click.

Both exist because the dashboard was decoration. The tests that matter here
are the ones about *not* reporting work: a reply from an hour ago is not a
backlog item, and an answered reply is not waiting on anyone.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from juicebox.models import Candidate, Message, utcnow


def _candidate(session, org_id: str, name: str = "Ada Lovelace") -> Candidate:
    row = Candidate(org_id=org_id, full_name=name, source="manual", source_id=f"test:{name}:{org_id}")
    session.add(row)
    session.flush()
    return row


def _message(session, *, org_id, candidate_id, direction, hours_ago, status="sent"):
    at = utcnow() - timedelta(hours=hours_ago)
    row = Message(
        org_id=org_id,
        candidate_id=candidate_id,
        direction=direction,
        status=status,
        sent_at=at,
        subject="re: the role",
    )
    session.add(row)
    session.flush()
    return row


def _items(client, headers) -> dict:
    response = client.get("/v1/analytics/todo", headers=headers)
    assert response.status_code == 200, response.text
    return {item["key"]: item for item in response.json()["items"]}


class TestRepliesWaiting:
    def test_an_unanswered_reply_is_reported_with_its_age(self, client, session, account):
        org_id = account["org_id"]
        candidate = _candidate(session, org_id)
        _message(session, org_id=org_id, candidate_id=candidate.id, direction="outbound", hours_ago=100)
        _message(session, org_id=org_id, candidate_id=candidate.id, direction="inbound", hours_ago=72)
        session.commit()

        item = _items(client, account["headers"])["replies_waiting"]
        assert item["count"] == 1
        # Three days, give or take the second the test took to run.
        assert 71 <= item["oldest_hours"] <= 74

    def test_a_reply_we_answered_is_not_waiting(self, client, session, account):
        org_id = account["org_id"]
        candidate = _candidate(session, org_id)
        _message(session, org_id=org_id, candidate_id=candidate.id, direction="inbound", hours_ago=72)
        _message(session, org_id=org_id, candidate_id=candidate.id, direction="outbound", hours_ago=40)
        session.commit()

        assert "replies_waiting" not in _items(client, account["headers"])

    def test_a_reply_from_an_hour_ago_is_not_a_backlog_item(self, client, session, account):
        """The grace window is the difference between a to-do list and a nag."""
        org_id = account["org_id"]
        candidate = _candidate(session, org_id)
        _message(session, org_id=org_id, candidate_id=candidate.id, direction="inbound", hours_ago=1)
        session.commit()

        assert "replies_waiting" not in _items(client, account["headers"])

    def test_another_org_s_backlog_is_not_ours(self, client, session, account):
        from juicebox.models import Org

        other = Org(name="Somebody Else", slug="somebody-else")
        session.add(other)
        session.flush()
        stranger = _candidate(session, other.id, "Grace Hopper")
        _message(session, org_id=other.id, candidate_id=stranger.id, direction="inbound", hours_ago=200)
        session.commit()

        assert _items(client, account["headers"]) == {}


class TestOtherChores:
    def test_a_failed_send_is_reported(self, client, session, account):
        org_id = account["org_id"]
        candidate = _candidate(session, org_id)
        _message(
            session,
            org_id=org_id,
            candidate_id=candidate.id,
            direction="outbound",
            hours_ago=5,
            status="bounced",
        )
        session.commit()

        assert _items(client, account["headers"])["sends_failed"]["count"] == 1

    def test_an_empty_workspace_reports_nothing(self, client, account):
        assert _items(client, account["headers"]) == {}


class TestActivityIsClickable:
    def test_an_outreach_event_names_the_person_not_just_the_message(
        self, client, session, account
    ):
        """A message has no page. Without this the row cannot be opened."""
        from juicebox.models import record_event

        org_id = account["org_id"]
        candidate = _candidate(session, org_id)
        message = _message(
            session, org_id=org_id, candidate_id=candidate.id, direction="inbound", hours_ago=2
        )
        record_event(
            session,
            org_id=org_id,
            type="outreach.replied",
            subject_type="message",
            subject_id=message.id,
            payload={"intent": "interested"},
        )
        session.commit()

        feed = client.get("/v1/analytics/activity", headers=account["headers"]).json()
        row = next(event for event in feed if event["type"] == "outreach.replied")
        assert row["candidate_id"] == candidate.id

    @pytest.mark.parametrize("subject_type", ["candidate", "message"])
    def test_a_subject_we_cannot_resolve_leaves_the_field_empty(
        self, client, session, account, subject_type
    ):
        """Never a guessed id: a wrong link opens somebody else's record."""
        from juicebox.models import record_event

        record_event(
            session,
            org_id=account["org_id"],
            type="agent.run",
            subject_type=subject_type,
            subject_id="cand_does_not_exist" if subject_type == "message" else "",
            payload={},
        )
        session.commit()

        feed = client.get("/v1/analytics/activity", headers=account["headers"]).json()
        row = next(event for event in feed if event["type"] == "agent.run")
        assert row["candidate_id"] == ""
