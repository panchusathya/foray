"""Warm-up: a newly connected mailbox ramps up to its cap instead of starting there.

Written from failure items 32-36 in juicebox/deliverability/README.md before
the code.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_integrations import _connect, ms_configured  # noqa: F401 - fixture

from juicebox.deliverability.warmup import effective_cap, ramp_cap, warmup_state
from juicebox.models import Candidate, MailboxConnection, Message, utcnow
from juicebox.outreach.rotation import (
    frees_at,
    pick_mailbox,
    pool_capacity,
    remaining_capacity,
)
from juicebox.outreach.sender import set_sender_factory

# Connecting goes through the Microsoft callback, which needs its credentials.
pytestmark = pytest.mark.usefixtures("ms_configured")


class TestTheRamp:
    @pytest.mark.parametrize(
        ("day", "cap"),
        [
            (0, 2),
            (6, 2),
            (7, 4),
            (13, 4),
            (14, 7),
            (21, 10),
            (28, 15),
            (35, 20),
            (42, 25),
            (48, 25),
            (49, 30),
            (56, 35),
            (63, 40),
        ],
    )
    def test_schedule(self, day, cap):
        assert ramp_cap(day) == cap

    def _box(self, *, cap=40, started_days_ago=None):
        now = utcnow()
        started = None if started_days_ago is None else now - timedelta(days=started_days_ago)
        return MailboxConnection(
            address="new@forayhire.com", daily_send_cap=cap, warmup_started_at=started
        ), now

    def test_not_warming_means_the_cap(self):
        box, now = self._box(cap=40)
        assert effective_cap(box, now) == 40

    def test_warming_follows_the_ramp(self):
        box, now = self._box(cap=40, started_days_ago=8)
        assert effective_cap(box, now) == 4

    def test_a_lower_cap_wins_over_the_ramp(self):
        box, now = self._box(cap=3, started_days_ago=30)
        assert effective_cap(box, now) == 3

    def test_raising_the_cap_does_not_skip_the_ramp(self):
        box, now = self._box(cap=500, started_days_ago=0)
        assert effective_cap(box, now) == 2

    def test_zero_still_means_off(self):
        box, now = self._box(cap=0, started_days_ago=0)
        assert effective_cap(box, now) == 0

    def test_state_says_when_it_finishes_and_then_that_it_has(self):
        box, now = self._box(cap=20, started_days_ago=10)
        state = warmup_state(box, now)
        assert state["active"] is True
        assert (state["day"], state["cap_today"]) == (10, 4)
        assert state["full_on"] == (box.warmup_started_at + timedelta(days=35)).date().isoformat()
        box.warmup_started_at = now - timedelta(days=40)
        assert warmup_state(box, now)["active"] is False


class _Sender:
    def __init__(self, connection, log):
        self.connection, self.log = connection, log

    def send(self, email):
        from juicebox.outreach.sender import SendResult

        self.log.append(self.connection.address)
        return SendResult(ok=True, provider_native_id=f"n{len(self.log)}")


class TestRotationUsesTheRampEverywhere:
    @pytest.fixture
    def pool(self, session, account):
        now = utcnow()
        seasoned = MailboxConnection(
            org_id=account["org_id"], address="old@goforay.io", daily_send_cap=5
        )
        fresh = MailboxConnection(
            org_id=account["org_id"],
            address="new@forayhire.com",
            daily_send_cap=40,
            warmup_started_at=now,
        )
        person = Candidate(org_id=account["org_id"], source="t", source_id="w1", full_name="W")
        session.add_all([seasoned, fresh, person])
        session.commit()
        set_sender_factory("microsoft", lambda db, conn: _Sender(conn, []))
        return {
            "seasoned": seasoned,
            "fresh": fresh,
            "now": now,
            "org_id": account["org_id"],
            "person": person,
        }

    def _sent(self, session, pool, connection, n, now):
        for i in range(n):
            session.add(
                Message(
                    org_id=connection.org_id,
                    candidate_id=pool["person"].id,
                    direction="outbound",
                    status="sent",
                    mailbox_id=connection.id,
                    sent_at=now - timedelta(minutes=i + 1),
                )
            )

    def test_room_and_freeing_follow_the_ramp(self, session, pool):
        fresh, now = pool["fresh"], pool["now"]
        assert remaining_capacity(session, connection=fresh, now=now) == 2
        self._sent(session, pool, fresh, 2, now)
        session.flush()
        assert remaining_capacity(session, connection=fresh, now=now) == 0
        assert frees_at(session, connection=fresh, now=now) > now

    def test_a_full_warming_mailbox_is_not_picked(self, session, pool):
        fresh, now = pool["fresh"], pool["now"]
        self._sent(session, pool, fresh, 2, now)
        session.flush()
        picked = pick_mailbox(session, org_id=pool["org_id"], now=now)
        assert picked.id == pool["seasoned"].id

    def test_pool_capacity_counts_todays_cap(self, session, pool):
        assert pool_capacity(session, org_id=pool["org_id"], now=pool["now"]) == 5 + 2


class TestConnecting:
    def test_a_new_mailbox_starts_warming(self, client, account, monkeypatch):
        body = _connect(client, account["headers"], monkeypatch, address="new@forayhire.com")
        [row] = body["connections"]
        assert row["warmup"]["active"] is True
        assert row["warmup"]["cap_today"] == 2
        assert row["effective_cap"] == 2

    def test_reconnecting_a_mailbox_does_not_restart_it(
        self, client, account, monkeypatch, session
    ):
        session.add(
            MailboxConnection(
                org_id=account["org_id"],
                address="tushar@goforay.io",
                daily_send_cap=5,
                provider="microsoft",
            )
        )
        session.commit()
        body = _connect(client, account["headers"], monkeypatch, address="tushar@goforay.io")
        [row] = body["connections"]
        assert row["warmup"]["active"] is False
        assert row["effective_cap"] == 5

    def test_warm_up_can_be_ended_and_started_by_hand(self, client, account, monkeypatch):
        body = _connect(client, account["headers"], monkeypatch, address="new@forayhire.com")
        mailbox_id = body["connections"][0]["id"]
        ended = client.patch(
            f"/v1/integrations/mailboxes/{mailbox_id}/sending",
            json={"warmup": False},
            headers=account["headers"],
        ).json()
        assert ended["warmup"]["active"] is False and ended["effective_cap"] == 40
        started = client.patch(
            f"/v1/integrations/mailboxes/{mailbox_id}/sending",
            json={"warmup": True},
            headers=account["headers"],
        ).json()
        assert started["warmup"]["active"] is True and started["effective_cap"] == 2


def test_the_deliverability_page_shows_todays_cap(client, account, monkeypatch):
    _connect(client, account["headers"], monkeypatch, address="new@forayhire.com")
    body = client.get("/v1/deliverability", headers=account["headers"]).json()
    [row] = [m for d in body["domains"] for m in d["mailboxes"]]
    assert row["effective_cap"] == 2
    assert row["warmup"]["active"] is True
