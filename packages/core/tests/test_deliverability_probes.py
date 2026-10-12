"""Sending seed tests and reading their results.

Written from the failure list in juicebox/deliverability/README.md (items 1-11)
before the code. The rails a seed test must obey are the same ones real
outreach obeys -- the whole point is to measure the domain without spending
its reputation on unmetered extra mail.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from juicebox.deliverability import seeds as seeds_module
from juicebox.deliverability.probes import (
    check_seed,
    due_tests,
    queue_test_now,
    send_seed_test,
)
from juicebox.deliverability.seeds import Placement, SeedReadError
from juicebox.integrations.crypto import encrypt_json
from juicebox.integrations.discovery_filters import own_addresses
from juicebox.models import (
    Job,
    MailboxConnection,
    Message,
    Org,
    SeedInbox,
    SeedTest,
    utcnow,
)
from juicebox.outreach.rotation import frees_at, sent_in_window
from juicebox.outreach.sender import SendResult, set_sender_factory
from juicebox.outreach.sequencer import enroll_candidates


class LogSender:
    def __init__(self, connection, log):
        self.connection = connection
        self.log = log

    def send(self, email):
        self.log.append(email)
        n = len(self.log)
        return SendResult(
            ok=True,
            provider_message_id=f"<seed-{n}@test>",
            provider_native_id=f"native-seed-{n}",
            internet_message_id=f"<seed-{n}@test>",
            conversation_id=f"conv-seed-{n}",
        )


@pytest.fixture
def setup(session, account, outreach_fixture):
    org_id = account["org_id"]
    connection = MailboxConnection(
        org_id=org_id, address="ryan@goforay.io", daily_send_cap=5, provider="microsoft"
    )
    session.add(connection)
    session.flush()
    log: list = []
    set_sender_factory("microsoft", lambda db, conn: LogSender(conn, log))

    sequence = outreach_fixture["sequence"]
    candidate = outreach_fixture["candidates"][0]
    enrollment = enroll_candidates(
        session, sequence=sequence, candidate_ids=[candidate.id], project_id=sequence.project_id
    )[0]
    first_step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
    now = utcnow()
    real = Message(
        org_id=org_id,
        enrollment_id=enrollment.id,
        candidate_id=candidate.id,
        step_id=first_step.id,
        direction="outbound",
        source="sequence",
        status="sent",
        mailbox_id=connection.id,
        from_address=connection.address,
        to_address=candidate.emails[0],
        subject="Senior Machine Learning Engineer at Founding ML Engineer",
        body="Hi Ada",
        sent_at=now - timedelta(hours=30),
    )
    session.add(real)
    seeds = []
    for address, label in (("seed1@gmail.com", "gmail"), ("seed2@yahoo.com", "yahoo")):
        seed = SeedInbox(
            org_id=org_id,
            address=address,
            kind="imap",
            provider_label=label,
            imap_host="imap.example.com",
            imap_port=993,
            username=address,
            secret_blob=encrypt_json({"password": "app-pass"}),
        )
        session.add(seed)
        seeds.append(seed)
    session.commit()
    return {
        "org_id": org_id,
        "connection": connection,
        "seeds": seeds,
        "log": log,
        "real": real,
        "sequence": sequence,
        "now": now,
    }


def _send(session, setup, *, seed=None, key="k1", now=None):
    result = send_seed_test(
        session,
        connection=setup["connection"],
        seed=seed or setup["seeds"][0],
        now=now or setup["now"],
        key=key,
    )
    session.commit()
    return result


class TestWhatGetsSent:
    def test_a_real_recent_email_goes_to_the_seed_from_that_mailbox(self, session, setup):
        result = _send(session, setup)
        assert result["status"] == "sent"
        [email] = setup["log"]
        assert email.to == "seed1@gmail.com"
        assert email.from_email == "ryan@goforay.io"
        # Re-rendered from the step: the subject the candidate got, and the
        # step's body, not a canned test message.
        assert email.subject == setup["real"].subject
        assert "Hi Ada" in email.text_body
        test = session.query(SeedTest).one()
        assert test.status == "sent"
        assert test.internet_message_id == "<seed-1@test>"
        assert test.source_message_id == setup["real"].id

    def test_a_seed_test_is_not_a_message(self, session, setup):
        before = session.query(Message).count()
        _send(session, setup)
        assert session.query(Message).count() == before

    def test_tracking_points_at_the_seed_test_never_the_real_message(self, session, setup):
        setup["sequence"].track_opens = True
        setup["sequence"].track_clicks = True
        session.commit()
        _send(session, setup)
        [email] = setup["log"]
        test = session.query(SeedTest).one()
        assert setup["real"].id not in email.html_body
        assert test.id in email.html_body

    def test_no_recent_email_means_no_test(self, session, setup):
        setup["real"].sent_at = setup["now"] - timedelta(days=30)
        session.commit()
        result = _send(session, setup)
        assert result == {"status": "skipped", "reason": "no_recent_send"}
        assert setup["log"] == []

    def test_a_retry_after_success_does_not_send_again(self, session, setup):
        _send(session, setup, key="same")
        again = _send(session, setup, key="same")
        assert again["status"] == "sent"
        assert len(setup["log"]) == 1
        assert session.query(SeedTest).count() == 1


class TestTheRailsRealMailObeys:
    def test_it_counts_against_the_daily_cap(self, session, setup):
        connection = setup["connection"]
        before = sent_in_window(session, mailbox_id=connection.id, now=setup["now"])
        _send(session, setup)
        assert sent_in_window(session, mailbox_id=connection.id, now=setup["now"]) == before + 1

    def test_a_full_mailbox_defers_until_it_has_room(self, session, setup):
        connection = setup["connection"]
        connection.daily_send_cap = 1
        _send(session, setup, key="first")  # fills the one slot
        result = _send(session, setup, seed=setup["seeds"][1], key="second")
        assert result["status"] == "deferred"
        assert result["defer_until"] == frees_at(session, connection=connection, now=setup["now"])
        assert len(setup["log"]) == 1

    def test_the_org_kill_switch_stops_it(self, session, setup):
        session.get(Org, setup["org_id"]).outreach_paused_at = setup["now"]
        session.commit()
        result = _send(session, setup)
        assert result == {"status": "skipped", "reason": "org_paused"}
        assert setup["log"] == []

    def test_outside_sending_hours_it_waits(self, session, setup):
        org = session.get(Org, setup["org_id"])
        org.send_window_enabled = True
        org.send_window_tz = "UTC"
        org.send_window_days = [0, 1, 2, 3, 4, 5, 6]
        hour = setup["now"].hour
        org.send_window_start = f"{(hour + 2) % 24:02d}:00"
        org.send_window_end = f"{(hour + 3) % 24:02d}:00"
        session.commit()
        result = _send(session, setup)
        assert result["status"] == "deferred"
        assert result["defer_until"] > setup["now"]
        assert setup["log"] == []

    def test_the_domain_gap_holds_it(self, session, setup, monkeypatch):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("OUTREACH_SEND_SPACING_SECONDS", "60")
        monkeypatch.setenv("OUTREACH_SEND_SPACING_MAX_SECONDS", "60")
        reset_settings_cache()
        setup["connection"].last_sent_at = setup["now"] - timedelta(seconds=10)
        session.commit()
        result = _send(session, setup)
        reset_settings_cache()
        assert result["status"] == "deferred"
        assert setup["log"] == []

    def test_a_sent_seed_starts_the_domain_gap(self, session, setup):
        _send(session, setup)
        assert setup["connection"].last_sent_at is not None

    @pytest.mark.parametrize(
        "change",
        [
            {"send_enabled": False},
            {"daily_send_cap": 0},
            {"status": "revoked"},
            {"status": "error"},
        ],
    )
    def test_a_mailbox_that_is_off_sends_nothing(self, session, setup, change):
        for key, value in change.items():
            setattr(setup["connection"], key, value)
        session.commit()
        result = _send(session, setup)
        assert result["status"] == "skipped"
        assert setup["log"] == []


class TestCadence:
    def test_every_eligible_mailbox_is_due_once_per_interval(self, session, setup):
        org = session.get(Org, setup["org_id"])
        due = due_tests(session, org, setup["now"])
        assert [(c.id, s.id) for c, s in due] == [(setup["connection"].id, setup["seeds"][0].id)]
        _send(session, setup)
        assert due_tests(session, org, setup["now"] + timedelta(hours=1)) == []
        later = due_tests(session, org, setup["now"] + timedelta(hours=25))
        # The next one goes to the seed this mailbox has tested least.
        assert [s.id for _c, s in later] == [setup["seeds"][1].id]

    def test_interval_zero_switches_scheduled_tests_off(self, session, setup):
        org = session.get(Org, setup["org_id"])
        org.seed_test_interval_hours = 0
        session.commit()
        assert due_tests(session, org, setup["now"]) == []

    def test_no_active_seeds_means_nothing_due(self, session, setup):
        for seed in setup["seeds"]:
            seed.status = "paused"
        session.commit()
        org = session.get(Org, setup["org_id"])
        assert due_tests(session, org, setup["now"]) == []

    def test_test_now_queues_one_per_seed_within_the_room_left(self, session, setup):
        setup["connection"].daily_send_cap = 1
        session.commit()
        result = queue_test_now(session, setup["connection"], setup["now"])
        session.commit()
        assert result == {"queued": 1, "seeds": 2, "room": 1}
        jobs = session.query(Job).filter(Job.kind == "deliverability.seed_send").all()
        assert len(jobs) == 1


class FakeReader:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.spam_visible = True

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        return False

    def find(self, probe):
        return self.answer


@pytest.fixture
def reader(monkeypatch):
    holder = {"reader": FakeReader(Placement(found=False))}
    monkeypatch.setattr(seeds_module, "_reader_factory", lambda seed: holder["reader"])
    return holder


class TestReadingResults:
    def test_a_found_message_records_its_placement(self, session, setup, reader):
        _send(session, setup)
        reader["reader"] = FakeReader(Placement(found=True, placement="spam", folder="Bulk"))
        check_seed(session, setup["seeds"][0], setup["now"] + timedelta(minutes=5))
        session.commit()
        test = session.query(SeedTest).one()
        assert (test.status, test.folder) == ("spam", "Bulk")
        assert test.checked_at is not None

    def test_not_found_yet_stays_pending(self, session, setup, reader):
        _send(session, setup)
        check_seed(session, setup["seeds"][0], setup["now"] + timedelta(hours=2))
        session.commit()
        assert session.query(SeedTest).one().status == "sent"

    def test_not_found_after_a_day_is_missing(self, session, setup, reader):
        _send(session, setup)
        check_seed(session, setup["seeds"][0], setup["now"] + timedelta(hours=25))
        session.commit()
        assert session.query(SeedTest).one().status == "missing"

    def test_a_seed_we_cannot_read_never_marks_anything_missing(self, session, setup, reader):
        _send(session, setup)
        reader["reader"] = FakeReader(error=SeedReadError("Could not sign in to seed1@gmail.com"))
        check_seed(session, setup["seeds"][0], setup["now"] + timedelta(hours=30))
        session.commit()
        seed = session.get(SeedInbox, setup["seeds"][0].id)
        assert seed.status == "error"
        assert "sign in" in seed.error
        assert session.query(SeedTest).one().status == "sent"

    def test_a_good_read_clears_an_old_error(self, session, setup, reader):
        seed = setup["seeds"][0]
        seed.status, seed.error = "error", "Could not sign in"
        session.commit()
        check_seed(session, seed, setup["now"])
        session.commit()
        assert (seed.status, seed.error) == ("active", "")


class TestNoSideEffects:
    def test_seed_addresses_are_never_discovered_as_contacts(self, session, setup):
        own = own_addresses(session, org_id=setup["org_id"])
        assert {"seed1@gmail.com", "seed2@yahoo.com"} <= own
