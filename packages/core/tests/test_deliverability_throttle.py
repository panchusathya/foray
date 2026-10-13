"""Auto-throttle: seeds in spam take a mailbox off new conversations, or rest it.

Written from failure items 38-45 in juicebox/deliverability/README.md before
the code.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from juicebox.deliverability.probes import due_tests, send_seed_test
from juicebox.deliverability.throttle import apply, decide, evidence, plan
from juicebox.deliverability.warmup import effective_cap
from juicebox.models import (
    Candidate,
    Event,
    MailboxConnection,
    Message,
    Org,
    SeedInbox,
    SeedTest,
    utcnow,
)
from juicebox.outreach.rotation import pick_mailbox, remaining_capacity
from juicebox.outreach.sender import SendResult, _bind, set_sender_factory


class _Ok:
    def __init__(self, connection):
        self.connection = connection

    def send(self, email):
        return SendResult(ok=True, provider_native_id="n", internet_message_id="<n@x>")


@pytest.fixture
def org_setup(session, account):
    org = session.get(Org, account["org_id"])
    now = utcnow()
    a = MailboxConnection(org_id=org.id, address="a@goforay.io", daily_send_cap=10)
    b = MailboxConnection(org_id=org.id, address="b@goforay.io", daily_send_cap=10)
    seed = SeedInbox(
        org_id=org.id,
        address="s@gmail.com",
        provider_label="gmail",
        kind="gmail_api",
        status="active",
    )
    person = Candidate(org_id=org.id, source="t", source_id="p", full_name="P")
    session.add_all([a, b, seed, person])
    session.commit()
    set_sender_factory("microsoft", lambda db, conn: _Ok(conn))
    return {"org": org, "a": a, "b": b, "seed": seed, "person": person, "now": now}


def _results(session, s, mailbox, statuses, *, hours_ago=2):
    for i, status in enumerate(statuses):
        session.add(
            SeedTest(
                org_id=mailbox.org_id,
                mailbox_id=mailbox.id,
                seed_id=s["seed"].id,
                job_key=f"{mailbox.id}-{status}-{i}-{hours_ago}",
                status=status,
                sent_at=s["now"] - timedelta(hours=hours_ago, minutes=i),
            )
        )
    session.commit()


def _block(session, s, mailbox, *, hours_ago=2):
    session.add(
        Message(
            org_id=mailbox.org_id,
            candidate_id=s["person"].id,
            direction="inbound",
            status="received",
            mailbox_id=mailbox.id,
            reply_intent="bounce",
            sent_at=s["now"] - timedelta(hours=hours_ago),
            reply_text="550 5.7.1 rejected: detected as spam",
        )
    )
    session.commit()


class TestDecisions:
    def test_too_few_results_is_no_evidence(self, session, org_setup):
        _results(session, org_setup, org_setup["a"], ["spam", "spam"])
        assert (
            decide(
                org_setup["a"],
                evidence(session, org_setup["a"], org_setup["now"]),
                org_setup["now"],
            ).state
            == "ok"
        )

    def test_a_quarter_in_spam_throttles(self, session, org_setup):
        _results(session, org_setup, org_setup["a"], ["spam", "inbox", "inbox", "other_tab"])
        d = decide(
            org_setup["a"], evidence(session, org_setup["a"], org_setup["now"]), org_setup["now"]
        )
        assert d.state == "throttled"
        assert "1 of 4" in d.reason

    def test_half_in_spam_rests_for_three_days(self, session, org_setup):
        _results(session, org_setup, org_setup["a"], ["spam", "missing", "inbox", "inbox"])
        d = decide(
            org_setup["a"], evidence(session, org_setup["a"], org_setup["now"]), org_setup["now"]
        )
        assert d.state == "resting"
        assert d.rest_until == org_setup["now"] + timedelta(days=3)

    def test_archived_and_tabs_are_delivered(self, session, org_setup):
        _results(
            session, org_setup, org_setup["a"], ["other_tab", "other_tab", "inbox", "other_tab"]
        )
        assert (
            decide(
                org_setup["a"],
                evidence(session, org_setup["a"], org_setup["now"]),
                org_setup["now"],
            ).state
            == "ok"
        )

    def test_policy_blocks(self, session, org_setup):
        _block(session, org_setup, org_setup["a"])
        assert (
            decide(
                org_setup["a"],
                evidence(session, org_setup["a"], org_setup["now"]),
                org_setup["now"],
            ).state
            == "throttled"
        )
        _block(session, org_setup, org_setup["a"], hours_ago=3)
        assert (
            decide(
                org_setup["a"],
                evidence(session, org_setup["a"], org_setup["now"]),
                org_setup["now"],
            ).state
            == "resting"
        )

    def test_old_spam_from_before_the_last_change_does_not_count(self, session, org_setup):
        a = org_setup["a"]
        _results(session, org_setup, a, ["spam", "spam", "spam"], hours_ago=30)
        a.throttle_state = "throttled"
        a.throttle_changed_at = org_setup["now"] - timedelta(hours=20)
        _results(session, org_setup, a, ["inbox", "inbox", "inbox"], hours_ago=2)
        assert decide(a, evidence(session, a, org_setup["now"]), org_setup["now"]).state == "ok"

    def test_a_throttled_mailbox_needs_fresh_clean_results_to_recover(self, session, org_setup):
        a = org_setup["a"]
        a.throttle_state = "throttled"
        a.throttle_changed_at = org_setup["now"] - timedelta(hours=5)
        _results(session, org_setup, a, ["inbox", "inbox"])
        assert (
            decide(a, evidence(session, a, org_setup["now"]), org_setup["now"]).state == "throttled"
        )

    def test_rest_holds_until_it_ends_then_ramps_back(self, session, org_setup):
        a, now = org_setup["a"], org_setup["now"]
        a.throttle_state = "resting"
        a.throttle_changed_at = now - timedelta(days=1)
        a.rest_until = now + timedelta(days=2)
        assert decide(a, evidence(session, a, now), now).state == "resting"
        later = now + timedelta(days=2, minutes=1)
        d = decide(a, evidence(session, a, later), later)
        assert d.state == "ok" and d.restart_warmup is True


class TestModes:
    def _spammy(self, session, s):
        _results(session, s, s["a"], ["spam", "spam", "inbox", "inbox"])

    def test_watch_shows_and_changes_nothing(self, session, org_setup):
        self._spammy(session, org_setup)
        org_setup["org"].auto_throttle = "watch"
        session.commit()
        decisions = plan(session, org_setup["org"], org_setup["now"])
        assert [(d.mailbox_id, d.state) for d in decisions] == [(org_setup["a"].id, "resting")]
        assert apply(session, org_setup["org"], org_setup["now"]) == []
        session.refresh(org_setup["a"])
        assert org_setup["a"].throttle_state == "ok"

    def test_off_shows_nothing(self, session, org_setup):
        self._spammy(session, org_setup)
        org_setup["org"].auto_throttle = "off"
        session.commit()
        assert plan(session, org_setup["org"], org_setup["now"]) == []

    def test_on_writes_the_state_and_an_event(self, session, org_setup):
        self._spammy(session, org_setup)
        org_setup["org"].auto_throttle = "on"
        session.commit()
        applied = apply(session, org_setup["org"], org_setup["now"])
        session.commit()
        assert [d.state for d in applied] == ["resting"]
        a = session.get(MailboxConnection, org_setup["a"].id)
        assert a.throttle_state == "resting" and a.throttle_reason
        assert session.query(Event).filter(Event.type == "deliverability.throttle").count() == 1

    def test_leaving_rest_restarts_warm_up(self, session, org_setup):
        a, now = org_setup["a"], org_setup["now"]
        a.throttle_state, a.rest_until = "resting", now - timedelta(minutes=1)
        a.throttle_changed_at = now - timedelta(days=3)
        org_setup["org"].auto_throttle = "on"
        session.commit()
        apply(session, org_setup["org"], now)
        session.commit()
        a = session.get(MailboxConnection, a.id)
        assert a.throttle_state == "ok"
        assert effective_cap(a, now) == 2


class TestEffectOnSending:
    def test_throttled_takes_no_new_conversations_but_keeps_its_threads(self, session, org_setup):
        a, b, now = org_setup["a"], org_setup["b"], org_setup["now"]
        a.throttle_state = "throttled"
        session.commit()
        for _ in range(5):
            assert pick_mailbox(session, org_id=a.org_id, now=now).id == b.id
        assert effective_cap(a, now) == 5  # half of 10
        assert remaining_capacity(session, connection=a, now=now) == 5
        resolved = _bind(session, a.org_id, a.id, now=now)
        assert not resolved.deferred and resolved.connection.id == a.id

    def test_resting_holds_its_threads_until_the_rest_ends(self, session, org_setup):
        a, now = org_setup["a"], org_setup["now"]
        a.throttle_state, a.rest_until = "resting", now + timedelta(days=2)
        session.commit()
        assert effective_cap(a, now) == 0
        resolved = _bind(session, a.org_id, a.id, now=now)
        assert resolved.deferred and resolved.retry_at == a.rest_until

    def test_a_throttled_mailbox_still_gets_seed_tests(self, session, org_setup):
        a, b = org_setup["a"], org_setup["b"]
        a.throttle_state = "throttled"
        session.commit()
        due = {c.id for c, _s in due_tests(session, org_setup["org"], org_setup["now"])}
        assert {a.id, b.id} <= due

    def test_a_resting_mailbox_sends_no_seed_tests(self, session, org_setup):
        a, now = org_setup["a"], org_setup["now"]
        a.throttle_state, a.rest_until = "resting", now + timedelta(days=2)
        session.commit()
        result = send_seed_test(session, connection=a, seed=org_setup["seed"], now=now, key="k")
        assert result["status"] == "skipped" and result["reason"] == "resting"

    def test_every_mailbox_throttled_defers_new_conversations(
        self, session, org_setup, outreach_fixture
    ):
        from juicebox.outreach.sender import resolve_sender

        for box in (org_setup["a"], org_setup["b"]):
            box.throttle_state = "throttled"
        session.commit()
        sequence = outreach_fixture["sequence"]
        resolved = resolve_sender(session, sequence, None, now=org_setup["now"])
        assert resolved.deferred
        assert resolved.connection is None


class TestApi:
    def test_the_setting(self, client, account):
        assert (
            client.get("/v1/deliverability", headers=account["headers"]).json()["settings"][
                "auto_throttle"
            ]
            == "watch"
        )
        ok = client.patch(
            "/v1/deliverability/settings", json={"auto_throttle": "on"}, headers=account["headers"]
        )
        assert ok.status_code == 200
        bad = client.patch(
            "/v1/deliverability/settings",
            json={"auto_throttle": "maybe"},
            headers=account["headers"],
        )
        assert bad.status_code == 422

    def test_overview_shows_state_and_what_watch_would_do(
        self, client, account, session, org_setup
    ):
        _results(session, org_setup, org_setup["a"], ["spam", "inbox", "inbox", "inbox"])
        body = client.get("/v1/deliverability", headers=account["headers"]).json()
        rows = {m["address"]: m for d in body["domains"] for m in d["mailboxes"]}
        assert rows["a@goforay.io"]["throttle"]["state"] == "ok"
        assert rows["a@goforay.io"]["throttle"]["would"] == "throttled"
        assert rows["b@goforay.io"]["throttle"]["would"] is None

    def test_clearing_by_hand(self, client, account, session, org_setup):
        a = org_setup["a"]
        a.throttle_state, a.rest_until = "resting", org_setup["now"] + timedelta(days=2)
        session.commit()
        response = client.post(
            f"/v1/deliverability/mailboxes/{a.id}/clear-throttle", headers=account["headers"]
        )
        assert response.status_code == 200
        session.expire_all()
        a = session.get(MailboxConnection, a.id)
        assert (a.throttle_state, a.rest_until) == ("ok", None)
        assert a.throttle_changed_at is not None  # old spam no longer counts
