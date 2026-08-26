"""Quiet hours, budgets, and consent provenance — CANDIDATE_AGENT_PLAN §2-§4.

Every clock in these tests is frozen: a quiet-hours suite that reads the wall
clock passes at 2pm and fails at 10pm, and nobody debugs a test that only
fails after dinner. Winter dates throughout so Eastern is EST (UTC-5) and
Pacific is PST (UTC-8) without DST arithmetic in the assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from juicebox.jobs import KIND_INTAKE_TURN, enqueue
from juicebox.messaging import cadence
from juicebox.messaging.cadence import (
    MAX_NUDGES_PER_THREAD,
    REASON_IN_WINDOW,
    REASON_NUDGE_BUDGET,
    REASON_ORG_CIRCUIT_OPEN,
    REASON_QUIET_HOURS,
    REASON_REPLY_WINDOW,
    TZ_SOURCE_STATED,
    in_send_window,
    may_initiate,
    may_send_now,
    next_window_open,
    org_outbound_allowed,
    resolve_timezone,
)
from juicebox.messaging.channels import (
    LINQ,
    WEB,
    DeliveryResult,
    FakeChannel,
    OutboundMessage,
    set_channel,
)
from juicebox.messaging.consent import (
    DISCLOSURE_WEB_WIDGET,
    DISCLOSURE_WEB_WIDGET_VERSION,
    SCOPE_MESSAGING,
    SOURCE_WEB_WIDGET,
    record_consent,
    revoke_consent,
)
from juicebox.messaging.conversation import ACTION_DEFERRED, run_turn
from juicebox.messaging.suppression import handle_stop
from juicebox.messaging.threads import (
    STATUS_STOPPED,
    close_thread,
    open_thread,
    record_inbound,
    record_outbound,
)
from juicebox.models import ConsentRecord

# 2026-01-15. Eastern is EST (UTC-5); the unknown-zone window 11:00-21:00 ET
# is 16:00Z-02:00Z. Pacific is PST (UTC-8); 8am-9pm PT is 16:00Z-05:00Z.
TEN_AM_ET = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
HALF_ELEVEN_ET = datetime(2026, 1, 15, 16, 30, tzinfo=UTC)
HALF_NINE_PM_ET = datetime(2026, 1, 16, 2, 30, tzinfo=UTC)
SEVEN_AM_PT = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
NINE_AM_PT = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)


@pytest.fixture
def linq_channel():
    channel = FakeChannel(consent=True)
    set_channel(LINQ, channel)
    return channel


@pytest.fixture
def linq_thread(session, account, linq_channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-1",
        entry_point="linq_inbound",
        from_address="+14155550100",
        to_address="+16283865454",
        consent_messaging=True,
    )


class TestWindows:
    """The pure window arithmetic, no database involved."""

    def test_unknown_zone_uses_the_intersection_window(self):
        # 10:00 ET is inside the real TCPA window for an Eastern recipient,
        # but 7:00 PT for a Pacific one -- the intersection refuses it.
        assert not in_send_window(TEN_AM_ET)
        assert in_send_window(HALF_ELEVEN_ET)
        assert not in_send_window(HALF_NINE_PM_ET)

    def test_known_zone_gets_the_real_window(self):
        # The same instant refused under the unknown window is fine for a
        # known Eastern recipient (10am), and 9am PT is fine for a known
        # Pacific one.
        assert in_send_window(TEN_AM_ET, zone_name="America/New_York")
        assert not in_send_window(SEVEN_AM_PT, zone_name="America/Los_Angeles")
        assert in_send_window(NINE_AM_PT, zone_name="America/Los_Angeles")

    def test_next_window_open_before_and_after(self):
        # Before today's window: opens at 11:00 ET today (16:00Z).
        opens = next_window_open(TEN_AM_ET)
        assert opens == datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
        # After it: opens at 11:00 ET tomorrow.
        opens = next_window_open(HALF_NINE_PM_ET)
        assert opens == datetime(2026, 1, 16, 16, 0, tzinfo=UTC)
        # Already open: now is the answer, nothing to wait for.
        assert next_window_open(HALF_ELEVEN_ET) == HALF_ELEVEN_ET

    def test_unparseable_zone_falls_back_to_the_conservative_window(self):
        assert not in_send_window(TEN_AM_ET, zone_name="Not/AZone")


class TestTimezoneResolution:
    def test_stated_location_wins_and_is_persisted(self, session, linq_thread):
        record = dict(linq_thread.record or {})
        record["spec"] = {**(record.get("spec") or {}), "locations": ["San Francisco"]}
        linq_thread.record = record

        zone, source = resolve_timezone(session, thread=linq_thread)
        assert zone == "America/Los_Angeles"
        assert source == TZ_SOURCE_STATED
        # Stored, so the window a send ran under stays auditable.
        assert linq_thread.timezone == "America/Los_Angeles"
        assert linq_thread.timezone_source == TZ_SOURCE_STATED

    def test_unknown_location_resolves_to_nothing(self, session, linq_thread):
        record = dict(linq_thread.record or {})
        record["spec"] = {**(record.get("spec") or {}), "locations": ["Atlantis"]}
        linq_thread.record = record
        zone, source = resolve_timezone(session, thread=linq_thread)
        assert zone == "" and source == ""
        assert linq_thread.timezone == ""

    def test_state_names_resolve(self, session, linq_thread):
        record = dict(linq_thread.record or {})
        record["spec"] = {**(record.get("spec") or {}), "locations": ["Texas"]}
        linq_thread.record = record
        zone, _ = resolve_timezone(session, thread=linq_thread)
        assert zone == "America/Chicago"


class TestMaySendNow:
    def test_a_reply_is_not_a_solicitation(self, session, linq_thread):
        # 21:30 ET, but they texted us half an hour ago: answer them.
        linq_thread.last_inbound_at = HALF_NINE_PM_ET - timedelta(minutes=30)
        decision = may_send_now(session, thread=linq_thread, now=HALF_NINE_PM_ET)
        assert decision.allowed and decision.reason == REASON_REPLY_WINDOW

    def test_a_stale_turn_waits_for_the_window(self, session, linq_thread):
        # The same instant, but their message is two hours old -- a queue
        # retry at 21:30 ET must hold until the window opens, not send.
        linq_thread.last_inbound_at = HALF_NINE_PM_ET - timedelta(hours=2)
        decision = may_send_now(session, thread=linq_thread, now=HALF_NINE_PM_ET)
        assert not decision.allowed
        assert decision.reason == REASON_QUIET_HOURS
        assert decision.resume_at == datetime(2026, 1, 16, 16, 0, tzinfo=UTC)

    def test_known_zone_widens_the_window(self, session, linq_thread):
        record = dict(linq_thread.record or {})
        record["spec"] = {**(record.get("spec") or {}), "locations": ["New York"]}
        linq_thread.record = record
        linq_thread.last_inbound_at = TEN_AM_ET - timedelta(hours=2)
        # 10am ET is outside the unknown window but inside New York's own.
        decision = may_send_now(session, thread=linq_thread, now=TEN_AM_ET)
        assert decision.allowed and decision.reason == REASON_IN_WINDOW


class TestRunTurnDeferral:
    def test_quiet_hours_defer_the_turn(self, session, linq_thread, monkeypatch):
        record_inbound(session, thread=linq_thread, body="hi", provider_native_id="m1")
        linq_thread.last_inbound_at = HALF_NINE_PM_ET - timedelta(hours=2)
        monkeypatch.setattr(cadence, "utcnow", lambda: HALF_NINE_PM_ET)

        result = run_turn(session, thread=linq_thread)
        assert result.action == ACTION_DEFERRED
        assert result.resume_at == datetime(2026, 1, 16, 16, 0, tzinfo=UTC)
        assert result.sent == []

    def test_web_chat_is_never_gated(self, session, account, monkeypatch):
        # The widget's delivery is the HTTP response to somebody sitting in
        # front of it; TCPA has no carrier in that path.
        set_channel(WEB, FakeChannel())
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        record_inbound(session, thread=thread, body="hi")
        thread.last_inbound_at = HALF_NINE_PM_ET - timedelta(hours=2)
        monkeypatch.setattr(cadence, "utcnow", lambda: HALF_NINE_PM_ET)

        result = run_turn(session, thread=thread)
        assert result.action != ACTION_DEFERRED

    def test_deferred_job_requeues_at_the_window(self, session, account, linq_thread, monkeypatch):
        from juicebox.worker import process_job

        record_inbound(session, thread=linq_thread, body="hi", provider_native_id="m2")
        linq_thread.last_inbound_at = HALF_NINE_PM_ET - timedelta(hours=2)
        monkeypatch.setattr(cadence, "utcnow", lambda: HALF_NINE_PM_ET)

        job = enqueue(
            session,
            org_id=account["org_id"],
            kind=KIND_INTAKE_TURN,
            idempotency_key="turn:test-defer",
            payload={"thread_id": linq_thread.id},
        )
        job.status = "running"
        process_job(session, job, now=HALF_NINE_PM_ET)

        assert job.status == "queued"
        run_at = job.run_at.replace(tzinfo=UTC) if job.run_at.tzinfo is None else job.run_at
        assert run_at == datetime(2026, 1, 16, 16, 0, tzinfo=UTC)


class TestInitiatedOutbound:
    def _proactive(self, session, thread, *, sent_at, body="nudge"):
        row = record_outbound(
            session,
            thread=thread,
            message=OutboundMessage(body=body),
            result=DeliveryResult(status="sent"),
            origin="proactive",
        )
        row.sent_at = sent_at
        session.flush()
        return row

    def test_nudge_budget_is_spent_by_sent_rows(self, session, linq_thread):
        now = HALF_ELEVEN_ET
        for i in range(MAX_NUDGES_PER_THREAD):
            self._proactive(session, linq_thread, sent_at=now - timedelta(days=10 + i * 4))
        decision = may_initiate(session, thread=linq_thread, now=now)
        assert not decision.allowed and decision.reason == REASON_NUDGE_BUDGET
        # Budget refusals carry no retry time: waiting does not restore one.
        assert decision.resume_at is None

    def test_nudges_keep_their_distance(self, session, linq_thread):
        now = HALF_ELEVEN_ET
        self._proactive(session, linq_thread, sent_at=now - timedelta(hours=24))
        decision = may_initiate(session, thread=linq_thread, now=now)
        assert not decision.allowed and decision.reason == REASON_NUDGE_BUDGET

        # 72h later the second (and last) nudge is allowed.
        later = now + timedelta(hours=49)
        decision = may_initiate(session, thread=linq_thread, now=later)
        assert decision.allowed

    def test_initiated_sends_have_no_reply_window(self, session, linq_thread):
        # A fresh inbound message lets a *reply* through at 21:30 ET, but a
        # nudge is not a reply and waits for the window regardless.
        linq_thread.last_inbound_at = HALF_NINE_PM_ET - timedelta(minutes=5)
        decision = may_initiate(session, thread=linq_thread, now=HALF_NINE_PM_ET)
        assert not decision.allowed and decision.reason == REASON_QUIET_HOURS

    def test_optout_circuit_breaker(self, session, account, linq_channel):
        org_id = account["org_id"]
        now = HALF_ELEVEN_ET
        for i in range(5):
            thread = open_thread(
                session,
                org_id=org_id,
                channel=LINQ,
                provider="linq",
                provider_thread_id=f"chat-stop-{i}",
                from_address=f"+1415555020{i}",
                consent_messaging=True,
            )
            close_thread(session, thread=thread, status=STATUS_STOPPED, reason="opted out")
            thread.completed_at = now - timedelta(hours=1)
        session.flush()

        assert not org_outbound_allowed(session, org_id=org_id, now=now)
        victim = open_thread(
            session,
            org_id=org_id,
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-victim",
            from_address="+14155551000",
            consent_messaging=True,
        )
        decision = may_initiate(session, thread=victim, now=now)
        assert not decision.allowed and decision.reason == REASON_ORG_CIRCUIT_OPEN

        # Opt-outs age out of the rolling day; the breaker closes on its own.
        assert org_outbound_allowed(session, org_id=org_id, now=now + timedelta(hours=30))


class TestConsentRecords:
    def test_widget_session_writes_a_grant(self, client, session, account):
        from juicebox.routers.messaging import PURPOSE_WIDGET
        from juicebox.security import signed_intake_token

        set_channel(WEB, FakeChannel())
        org_id = account["org_id"]
        token = f"{org_id}.{signed_intake_token(PURPOSE_WIDGET, org_id)}"
        response = client.post("/v1/intake/chat/sessions", json={"token": token})
        assert response.status_code == 201, response.text
        thread_id = response.json()["thread_id"]

        row = session.scalar(
            select(ConsentRecord).where(ConsentRecord.thread_id == thread_id)
        )
        assert row is not None
        assert row.source == SOURCE_WEB_WIDGET
        assert row.scope == SCOPE_MESSAGING
        assert row.disclosure_version == DISCLOSURE_WEB_WIDGET_VERSION
        assert row.granted and row.revoked_at is None

    def test_record_consent_is_idempotent_per_thread_and_scope(
        self, session, account, linq_thread
    ):
        first = record_consent(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            scope=SCOPE_MESSAGING,
            source=SOURCE_WEB_WIDGET,
            disclosure_text=DISCLOSURE_WEB_WIDGET,
            disclosure_version=DISCLOSURE_WEB_WIDGET_VERSION,
            thread_id=linq_thread.id,
        )
        second = record_consent(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            scope=SCOPE_MESSAGING,
            source=SOURCE_WEB_WIDGET,
            disclosure_text=DISCLOSURE_WEB_WIDGET,
            disclosure_version=DISCLOSURE_WEB_WIDGET_VERSION,
            thread_id=linq_thread.id,
        )
        assert first.id == second.id
        rows = session.scalars(
            select(ConsentRecord).where(ConsentRecord.thread_id == linq_thread.id)
        ).all()
        assert len(rows) == 1
        assert rows[0].disclosure_version == DISCLOSURE_WEB_WIDGET_VERSION

    def test_stop_revokes_the_grant(self, session, account, linq_thread):
        record_consent(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            scope=SCOPE_MESSAGING,
            source=SOURCE_WEB_WIDGET,
            disclosure_text=DISCLOSURE_WEB_WIDGET,
            disclosure_version=DISCLOSURE_WEB_WIDGET_VERSION,
            thread_id=linq_thread.id,
        )
        handle_stop(session, thread=linq_thread)
        row = session.scalar(
            select(ConsentRecord).where(ConsentRecord.thread_id == linq_thread.id)
        )
        assert row is not None and row.revoked_at is not None

    def test_revoke_is_a_stamp_not_a_delete(self, session, account, linq_thread):
        record_consent(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            scope=SCOPE_MESSAGING,
            source=SOURCE_WEB_WIDGET,
            disclosure_text=DISCLOSURE_WEB_WIDGET,
            disclosure_version=DISCLOSURE_WEB_WIDGET_VERSION,
            thread_id=linq_thread.id,
        )
        assert revoke_consent(session, thread_id=linq_thread.id) == 1
        # A second revocation finds nothing left to stamp.
        assert revoke_consent(session, thread_id=linq_thread.id) == 0
        assert (
            session.scalar(
                select(ConsentRecord).where(ConsentRecord.thread_id == linq_thread.id)
            )
            is not None
        )


class TestOriginColumn:
    def test_outbound_defaults_to_reply(self, session, linq_thread):
        row = record_outbound(
            session,
            thread=linq_thread,
            message=OutboundMessage(body="hello"),
            result=DeliveryResult(status="sent"),
        )
        assert row.origin == "reply"

    def test_failed_proactive_rows_do_not_spend_the_budget(self, session, linq_thread):
        record_outbound(
            session,
            thread=linq_thread,
            message=OutboundMessage(body="nudge"),
            result=DeliveryResult(status="failed", error="boom"),
            origin="proactive",
        )
        decision = may_initiate(session, thread=linq_thread, now=HALF_ELEVEN_ET)
        assert decision.allowed


class TestQuietHoursSwitch:
    """`QUIET_HOURS_ENABLED=false` turns the whole control off.

    On by default, because TCPA damages are statutory per message and the safe
    state has to be the one you get without configuring anything. Off is for a
    deployment whose only recipients are the people testing it.
    """

    def _at(self, hour: int):
        from datetime import UTC, datetime

        # 06:00 UTC is 02:00 Eastern -- outside every window there is.
        return datetime(2026, 8, 26, hour, 0, tzinfo=UTC)

    def test_the_middle_of_the_night_is_closed_by_default(self, monkeypatch):
        from juicebox.config import reset_settings_cache
        from juicebox.messaging import cadence

        monkeypatch.delenv("QUIET_HOURS_ENABLED", raising=False)
        reset_settings_cache()
        try:
            assert cadence.in_send_window(self._at(6)) is False
            assert cadence.in_send_window(self._at(6), zone_name="America/Los_Angeles") is False
        finally:
            reset_settings_cache()

    def test_off_opens_every_hour(self, monkeypatch):
        from juicebox.config import reset_settings_cache
        from juicebox.messaging import cadence

        monkeypatch.setenv("QUIET_HOURS_ENABLED", "false")
        reset_settings_cache()
        try:
            for hour in range(24):
                assert cadence.in_send_window(self._at(hour)) is True
        finally:
            reset_settings_cache()

    def test_off_reaches_a_thread_with_a_known_zone(self, monkeypatch):
        """The case the env hours could not touch: a resolved timezone used the
        hardcoded constants, so only a real switch turns it off."""
        from juicebox.config import reset_settings_cache
        from juicebox.messaging import cadence

        monkeypatch.setenv("QUIET_HOURS_ENABLED", "false")
        reset_settings_cache()
        try:
            assert cadence.in_send_window(self._at(6), zone_name="America/Los_Angeles") is True
        finally:
            reset_settings_cache()

    def test_off_means_no_deferral(self, monkeypatch):
        """A deferred send is re-enqueued at `next_window_open`; with the
        control off that has to be now, or the message still waits."""
        from juicebox.config import reset_settings_cache
        from juicebox.messaging import cadence

        monkeypatch.setenv("QUIET_HOURS_ENABLED", "false")
        reset_settings_cache()
        try:
            now = self._at(6)
            assert cadence.next_window_open(now) == now
            assert cadence.next_window_open(now, zone_name="America/Los_Angeles") == now
        finally:
            reset_settings_cache()

    def test_on_still_defers_to_the_window(self, monkeypatch):
        from juicebox.config import reset_settings_cache
        from juicebox.messaging import cadence

        monkeypatch.setenv("QUIET_HOURS_ENABLED", "true")
        reset_settings_cache()
        try:
            now = self._at(6)
            assert cadence.next_window_open(now) > now
        finally:
            reset_settings_cache()
