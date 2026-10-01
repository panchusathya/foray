"""Emails leave an org about a minute apart, in line order, whatever sent them."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest

from juicebox.jobs import KIND_OUTREACH_SEND, KIND_OUTREACH_TICK, enqueue
from juicebox.models import Enrollment, Job, utcnow
from juicebox.outreach.outbox import collect_in_flight
from juicebox.outreach.sequencer import (
    BLOCKED_KEY,
    dispatch_line,
    enroll_candidates,
    move_in_line,
    note_mail_sent,
    open_send_jobs,
    run_due,
    spacing_wait,
)
from juicebox.worker import _handle_outreach_tick, process_batch, process_job

MINUTE = timedelta(minutes=1)


@pytest.fixture(autouse=True)
def spacing(monkeypatch):
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("OUTREACH_SEND_SPACING_SECONDS", "60")
    # Pinned so gaps are exact; TestTheGapVaries covers the 45-60s range.
    monkeypatch.setenv("OUTREACH_SEND_SPACING_MAX_SECONDS", "60")
    reset_settings_cache()
    yield
    reset_settings_cache()


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _enrol_all(session, fixture, *, due):
    sequence = fixture["sequence"]
    rows = enroll_candidates(
        session,
        sequence=sequence,
        candidate_ids=[c.id for c in fixture["candidates"]],
        project_id=sequence.project_id,
    )
    for row in rows:
        row.next_send_at = due
    session.commit()
    return rows


def _tick(session, org_id, now):
    job = Job(org_id=org_id, kind=KIND_OUTREACH_TICK, payload={"org_id": org_id})
    result = _handle_outreach_tick(session, job, now)
    session.commit()
    return result


def _line_enrollments(session, org_id):
    return [job.payload["enrollment_id"] for job in open_send_jobs(session, org_id)]


def _queue(session, sequence, rows, *, now, ranks):
    step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
    jobs = []
    for row, rank in zip(rows, ranks, strict=True):
        job = enqueue(
            session,
            org_id=sequence.org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=f"{KIND_OUTREACH_SEND}:{row.id}:{step.id}",
            payload={"org_id": sequence.org_id, "enrollment_id": row.id, "step_id": step.id},
            run_at=now,
        )
        job.rank = rank
        jobs.append(job)
    session.commit()
    return jobs


class TestTheTickBuildsALine:
    def test_new_arrivals_are_ranked_in_the_order_they_fell_due(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        rows = _enrol_all(session, outreach_fixture, due=now)
        for index, row in enumerate(rows):
            row.next_send_at = now - timedelta(minutes=len(rows) - index)
        session.commit()

        _tick(session, org_id, now)

        assert _line_enrollments(session, org_id) == [row.id for row in rows]
        ranks = [job.rank for job in open_send_jobs(session, org_id)]
        assert ranks == sorted(ranks) and None not in ranks

    def test_a_later_tick_keeps_ranks_and_appends(self, session, outreach_fixture, mailbox):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        first, *rest = _enrol_all(session, outreach_fixture, due=now)
        for row in rest:
            row.next_send_at = now + timedelta(hours=1)
        session.commit()
        _tick(session, org_id, now)
        for row in rest:
            row.next_send_at = now
        session.commit()

        _tick(session, org_id, now + timedelta(seconds=10))

        assert _line_enrollments(session, org_id)[0] == first.id

    def test_only_the_head_of_the_line_is_awake(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        import juicebox.outreach.sequencer as sequencer

        monkeypatch.setattr(sequencer, "WAKE_WINDOW", 2)
        sequence = outreach_fixture["sequence"]
        now = utcnow()
        rows = _enrol_all(session, outreach_fixture, due=now)
        _queue(session, sequence, rows, now=now, ranks=range(1, len(rows) + 1))

        counts = dispatch_line(session, sequence.org_id, now)

        assert counts["awake"] == 2
        assert counts["asleep"] == len(rows) - 2

    def test_a_paused_sequence_never_takes_a_wake_slot(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        """The first deploy of the line sent nothing: paused sequences' sends,
        queued long ago, held ranks 1-5 and were woken every minute only to be
        held again, so nothing behind them ever got a turn."""
        import juicebox.outreach.sequencer as sequencer
        from juicebox.models import Sequence, SequenceStep

        monkeypatch.setattr(sequencer, "WAKE_WINDOW", 1)
        live = outreach_fixture["sequence"]
        now = utcnow()
        paused = Sequence(org_id=live.org_id, name="old", status="paused")
        session.add(paused)
        session.add(SequenceStep(sequence=paused, step_order=0, body_template="hi"))
        session.commit()
        candidates = outreach_fixture["candidates"]
        [old] = enroll_candidates(session, sequence=paused, candidate_ids=[candidates[0].id])
        [new] = enroll_candidates(session, sequence=live, candidate_ids=[candidates[1].id])
        old.next_send_at = new.next_send_at = now
        session.commit()
        _queue(session, paused, [old], now=now, ranks=[1])
        _queue(session, live, [new], now=now, ranks=[2])

        counts = dispatch_line(session, live.org_id, now)
        session.commit()
        process_batch(session, now=now)

        assert (counts["held"], counts["awake"]) == (1, 1)
        [sent] = mailbox.outbox
        assert sent.to == candidates[1].primary_email


class TestTheSendGate:
    def test_a_herd_sends_the_best_ranked_and_waits_out_the_gap(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        now = utcnow().replace(microsecond=0)
        rows = _enrol_all(session, outreach_fixture, due=now)
        # Ranked against creation order, so only rank can explain who wins.
        ranks = list(range(len(rows), 0, -1))
        jobs = _queue(session, sequence, rows, now=now, ranks=ranks)

        process_batch(session, now=now)

        [sent] = mailbox.outbox
        assert sent.to == rows[-1].candidate.primary_email
        waiting = [session.get(Job, job.id) for job in jobs if job.id != jobs[-1].id]
        assert {_aware(job.run_at) for job in waiting} == {now + MINUTE}

    def test_run_now_sends_one_and_leaves_the_rest_for_the_worker(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        rows = _enrol_all(session, outreach_fixture, due=now)

        summary = run_due(session, org_id=org_id, now=now)
        session.commit()

        assert summary.sent == 1
        assert len(mailbox.outbox) == 1
        still_due = [row for row in rows if session.get(Enrollment, row.id).current_step == 0]
        assert len(still_due) == len(rows) - 1

    def test_the_next_send_goes_once_the_minute_has_passed(
        self, session, outreach_fixture, mailbox
    ):
        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        _enrol_all(session, outreach_fixture, due=now)

        run_due(session, org_id=org_id, now=now)
        run_due(session, org_id=org_id, now=now + timedelta(seconds=30))
        assert len(mailbox.outbox) == 1
        run_due(session, org_id=org_id, now=now + MINUTE)
        assert len(mailbox.outbox) == 2


class TestMovingRows:
    def _line(self, session, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        now = utcnow()
        rows = _enrol_all(session, outreach_fixture, due=now)
        _queue(session, sequence, rows, now=now, ranks=range(1, len(rows) + 1))
        return sequence.org_id, [row.id for row in rows], now

    def test_top_keeps_the_picked_rows_in_their_own_order(self, session, outreach_fixture):
        org_id, ids, now = self._line(session, outreach_fixture)
        picked = [ids[2], ids[1]]

        move_in_line(session, org_id, enrollment_ids=picked, to="top", now=now)

        assert _line_enrollments(session, org_id) == [ids[1], ids[2], ids[0]]

    def test_bottom(self, session, outreach_fixture):
        org_id, ids, now = self._line(session, outreach_fixture)
        move_in_line(session, org_id, enrollment_ids=[ids[0]], to="bottom", now=now)
        assert _line_enrollments(session, org_id)[-1] == ids[0]

    def test_up_and_down_move_one_place(self, session, outreach_fixture):
        org_id, ids, now = self._line(session, outreach_fixture)
        move_in_line(session, org_id, enrollment_ids=[ids[2]], to="up", now=now)
        assert _line_enrollments(session, org_id)[:3] == [ids[0], ids[2], ids[1]]
        move_in_line(session, org_id, enrollment_ids=[ids[2]], to="down", now=now)
        assert _line_enrollments(session, org_id)[:3] == ids[:3]

    def test_up_at_the_top_stays_put(self, session, outreach_fixture):
        org_id, ids, now = self._line(session, outreach_fixture)
        move_in_line(session, org_id, enrollment_ids=[ids[0], ids[1]], to="up", now=now)
        assert _line_enrollments(session, org_id) == ids

    def test_to_a_position(self, session, outreach_fixture):
        org_id, ids, now = self._line(session, outreach_fixture)
        move_in_line(session, org_id, enrollment_ids=[ids[-1]], to="position", position=2, now=now)
        assert _line_enrollments(session, org_id)[1] == ids[-1]

    def test_the_endpoint(self, client, account, session, outreach_fixture):
        org_id, ids, _now = self._line(session, outreach_fixture)
        response = client.post(
            "/v1/outreach/in-flight/move",
            json={"enrollment_ids": [ids[-1]], "to": "top"},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["moved"] == 1
        session.expire_all()
        assert _line_enrollments(session, org_id)[0] == ids[-1]


class TestTheOutboxShowsTheLine:
    def test_rows_read_in_rank_order_with_rising_estimates(
        self, session, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        now = utcnow().replace(microsecond=0)
        rows = _enrol_all(session, outreach_fixture, due=now)
        _queue(session, sequence, rows, now=now, ranks=range(len(rows), 0, -1))

        items = collect_in_flight(session, org_id=sequence.org_id, now=now)

        assert [item.rank for item in items] == list(range(1, len(rows) + 1))
        assert [item.enrollment_id for item in items] == [row.id for row in reversed(rows)]
        times = [_aware(item.due_at) for item in items]
        assert times == sorted(times) and times[0] == now

    def test_a_row_waiting_on_a_full_mailbox_says_so(self, session, outreach_fixture, mailbox):
        sequence = outreach_fixture["sequence"]
        now = utcnow()
        rows = _enrol_all(session, outreach_fixture, due=now)
        [job, *_] = _queue(session, sequence, rows, now=now, ranks=range(1, len(rows) + 1))
        frees = now + timedelta(hours=2)
        job.payload = {**job.payload, BLOCKED_KEY: frees.isoformat()}
        session.commit()

        row = next(
            item
            for item in collect_in_flight(session, org_id=sequence.org_id, now=now)
            if item.enrollment_id == rows[0].id
        )

        assert (row.state, row.reason, row.rank) == ("held", "mailbox_full", 1)
        assert _aware(row.due_at) == frees


class TestTheGapVaries:
    def test_each_gap_lands_between_the_minimum_and_the_maximum(
        self, session, outreach_fixture, monkeypatch
    ):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("OUTREACH_SEND_SPACING_SECONDS", "45")
        monkeypatch.setenv("OUTREACH_SEND_SPACING_MAX_SECONDS", "60")
        reset_settings_cache()
        org_id = outreach_fixture["sequence"].org_id
        start = utcnow().replace(microsecond=0)
        gaps = set()
        for offset in range(12):
            sent = start + timedelta(minutes=offset)
            note_mail_sent(session, org_id, sent)
            gaps.add((spacing_wait(session, org_id, sent) - sent).total_seconds())

        assert all(45 <= gap <= 60 for gap in gaps)
        assert len(gaps) > 1


class TestEverySendSharesTheGap:
    def test_a_draft_ready_note_waits_for_its_mailboxs_gap(
        self, session, outreach_fixture, mailbox
    ):
        from juicebox.jobs import KIND_MAILBOX_NOTIFY
        from juicebox.models import MailboxConnection

        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        connection = MailboxConnection(
            org_id=org_id, provider="microsoft", address="me@acme-talent.com", status="active"
        )
        session.add(connection)
        session.flush()
        # The same mailbox sent moments ago (a sequence step, say).
        note_mail_sent(session, org_id, now, connection=connection)
        job = enqueue(
            session,
            org_id=org_id,
            kind=KIND_MAILBOX_NOTIFY,
            idempotency_key="notify-1",
            payload={"connection_id": connection.id, "subject": "ready", "text_body": "hi"},
            run_at=now,
        )
        session.commit()

        process_job(session, job, now=now + timedelta(seconds=5))

        assert job.status == "queued"
        assert _aware(job.run_at) == _aware(now) + MINUTE

    def test_a_sequence_send_waits_for_the_gap_after_any_other_mail(
        self, session, outreach_fixture, mailbox
    ):
        from juicebox.models import Org

        org_id = outreach_fixture["sequence"].org_id
        now = utcnow()
        _enrol_all(session, outreach_fixture, due=now)
        note_mail_sent(session, org_id, now)
        session.commit()
        assert session.get(Org, org_id).last_mail_sent_at is not None

        assert run_due(session, org_id=org_id, now=now + timedelta(seconds=20)).sent == 0
        assert run_due(session, org_id=org_id, now=now + MINUTE).sent == 1
