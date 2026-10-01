"""Sending hours: candidate email only inside the org's working window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from juicebox.models import Org, utcnow
from juicebox.outreach.outbox import collect_in_flight
from juicebox.outreach.sequencer import enroll_candidates, run_due, sending_hold
from juicebox.outreach.window import SendWindow, next_open, org_window

PT = ZoneInfo("America/Los_Angeles")


def _window(start=(9, 0), end=(17, 0), days=(0, 1, 2, 3, 4)):
    from datetime import time

    return SendWindow(True, time(*start), time(*end), PT, frozenset(days))


def _pt(*args):
    return datetime(*args, tzinfo=PT).astimezone(UTC)


class TestTheWindow:
    def test_inside_is_open(self):
        tuesday_noon = _pt(2026, 9, 29, 12, 0)
        assert next_open(_window(), tuesday_noon) == tuesday_noon

    def test_before_opening_waits_for_nine_pacific(self):
        tuesday_early = _pt(2026, 9, 29, 6, 30)
        assert next_open(_window(), tuesday_early) == _pt(2026, 9, 29, 9, 0)

    def test_after_closing_waits_for_tomorrow(self):
        tuesday_evening = _pt(2026, 9, 29, 17, 0)
        assert next_open(_window(), tuesday_evening) == _pt(2026, 9, 30, 9, 0)

    def test_friday_evening_waits_for_monday(self):
        friday_evening = _pt(2026, 10, 2, 18, 0)
        assert next_open(_window(), friday_evening) == _pt(2026, 10, 5, 9, 0)

    def test_an_overnight_window_spans_midnight(self):
        window = _window(start=(22, 0), end=(2, 0), days=range(7))
        assert next_open(window, _pt(2026, 9, 30, 1, 0)) == _pt(2026, 9, 30, 1, 0)
        assert next_open(window, _pt(2026, 9, 30, 3, 0)) == _pt(2026, 9, 30, 22, 0)

    def test_off_is_always_open(self):
        org = Org(name="x", send_window_enabled=False)
        now = utcnow()
        assert next_open(org_window(org), now) == now


def _closed_now(session, org_id):
    """Set the org's hours to a window that is closed right now."""
    org = session.get(Org, org_id)
    local = utcnow().astimezone(PT)
    start = (local + timedelta(hours=2)).strftime("%H:%M")
    end = (local + timedelta(hours=3)).strftime("%H:%M")
    org.send_window_enabled = True
    org.send_window_start, org.send_window_end = start, end
    org.send_window_tz = "America/Los_Angeles"
    org.send_window_days = list(range(7))
    session.commit()


class TestTheSendPath:
    def test_nothing_goes_outside_the_window(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        rows = enroll_candidates(
            session, sequence=sequence, candidate_ids=[outreach_fixture["candidates"][0].id]
        )
        rows[0].next_send_at = utcnow()
        _closed_now(session, sequence.org_id)

        summary = run_due(session, org_id=sequence.org_id)

        assert summary.sent == 0 and mailbox.outbox == []
        assert sending_hold(session, sequence, now=utcnow()) == "outside_hours"

    def test_the_outbox_says_why(self, session, outreach_fixture, mailbox):
        from juicebox.jobs import KIND_OUTREACH_TICK
        from juicebox.models import Job
        from juicebox.worker import _handle_outreach_tick

        sequence = outreach_fixture["sequence"]
        [row] = enroll_candidates(
            session, sequence=sequence, candidate_ids=[outreach_fixture["candidates"][0].id]
        )
        row.next_send_at = utcnow()
        session.commit()
        _handle_outreach_tick(
            session,
            Job(org_id=sequence.org_id, kind=KIND_OUTREACH_TICK, payload={"org_id": sequence.org_id}),
            utcnow(),
        )
        session.commit()
        _closed_now(session, sequence.org_id)

        [item] = [i for i in collect_in_flight(session, org_id=sequence.org_id) if i.rank]

        assert (item.state, item.reason) == ("held", "outside_hours")


class TestSettings:
    def test_round_trip(self, client, account):
        headers = account["headers"]
        response = client.patch(
            "/v1/org/sending",
            json={
                "send_gap_seconds": 40,
                "send_window_enabled": True,
                "send_window_start": "08:30",
                "send_window_end": "18:00",
                "send_window_tz": "America/Los_Angeles",
                "send_window_days": [0, 1, 2, 3, 4],
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = client.get("/v1/org/sending", headers=headers).json()
        assert body["send_gap_seconds"] == 40 and body["send_gap_is_default"] is False
        assert body["send_window_enabled"] is True
        assert (body["send_window_start"], body["send_window_end"]) == ("08:30", "18:00")
        assert body["send_window_next_change"] is not None
        assert body["paused"] is False

    def test_rejects_nonsense(self, client, account):
        headers = account["headers"]
        bad = [
            {"send_window_start": "25:00"},
            {"send_window_tz": "Mars/Olympus"},
            {"send_window_days": []},
            {"send_gap_seconds": 0},
        ]
        for payload in bad:
            response = client.patch("/v1/org/sending", json=payload, headers=headers)
            assert response.status_code == 422, payload


class TestBreaks:
    def _lunch(self):
        from dataclasses import replace
        from datetime import time

        return replace(_window(start=(8, 30)), breaks=((time(12, 0), time(13, 0)),))

    def test_no_sends_over_lunch(self):
        assert next_open(self._lunch(), _pt(2026, 9, 29, 12, 30)) == _pt(2026, 9, 29, 13, 0)

    def test_open_either_side_of_lunch(self):
        window = self._lunch()
        assert next_open(window, _pt(2026, 9, 29, 11, 59)) == _pt(2026, 9, 29, 11, 59)
        assert next_open(window, _pt(2026, 9, 29, 13, 0)) == _pt(2026, 9, 29, 13, 0)

    def test_the_morning_stretch_closes_at_the_break(self):
        from juicebox.outreach.window import closes_at

        assert closes_at(self._lunch(), _pt(2026, 9, 29, 9, 0)) == _pt(2026, 9, 29, 12, 0)

    def test_breaks_round_trip_and_reject_backwards(self, client, account):
        headers = account["headers"]
        ok = client.patch(
            "/v1/org/sending",
            json={"send_window_breaks": [{"start": "12:00", "end": "13:00"}]},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["send_window_breaks"] == [{"start": "12:00", "end": "13:00"}]
        bad = client.patch(
            "/v1/org/sending",
            json={"send_window_breaks": [{"start": "13:00", "end": "12:00"}]},
            headers=headers,
        )
        assert bad.status_code == 422
