"""The two variables a rotating mailbox pool made necessary.

`sender_name` exists because one sequence now sends from several mailboxes, so
a hardcoded signature would sign half the outreach as the wrong person.
`suggested_date` exists because a follow-up that recomputed its own date would
contradict the email before it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from juicebox.models import Enrollment, MailboxConnection, Message, SequenceStep
from juicebox.outreach.personalize import sender_context
from juicebox.outreach.sender import SendResult, set_sender_factory
from juicebox.outreach.sequencer import (
    build_message_content,
    enroll_candidates,
    preview_step,
    run_due,
    suggested_date_context,
)


class TestSenderName:

    def test_a_nameless_mailbox_falls_back_to_its_address(self):
        """A Zoho mailbox connected without a display name still signs off with
        something a human would write, not an email address."""
        for address, expected in [
            ("tushar.gupta@goforay.io", "Tushar Gupta"),
            ("sathya_panchu@goforay.io", "Sathya Panchu"),
            ("outreach-two@goforay.io", "Outreach Two"),
            ("tushar@goforay.io", "Tushar"),
        ]:
            ctx = sender_context(MailboxConnection(address=address, display_name=""))
            assert ctx["sender_name"] == expected, address

    def test_it_is_never_empty(self):
        """An empty signature is worse than a slightly wrong one."""
        assert sender_context(None, None)["sender_name"] == "the team"
        assert sender_context(MailboxConnection(address="", display_name=""))["sender_name"]

    def test_two_mailboxes_sign_the_same_step_differently(
        self, session, account, outreach_fixture
    ):
        """The whole point: one sequence, one template, two senders."""
        a = MailboxConnection(
            org_id=account["org_id"], address="a@goforay.io", display_name="Alpha One"
        )
        b = MailboxConnection(
            org_id=account["org_id"], address="b@goforay.io", display_name="Bravo Two"
        )
        session.add_all([a, b])
        session.flush()
        sequence = outreach_fixture["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        step.body_template = "Best,\n{{ sender_name }}"
        candidate = outreach_fixture["candidates"][0]

        bodies = {
            conn.address: build_message_content(
                session, sequence=sequence, step=step, candidate=candidate, connection=conn
            )[1]
            for conn in (a, b)
        }
        assert bodies["a@goforay.io"] == "Best,\nAlpha One"
        assert bodies["b@goforay.io"] == "Best,\nBravo Two"


class TestSuggestedDate:
    @pytest.mark.parametrize(
        "sent_on, expected_day",
        [
            # The stated convention: send Monday, propose Thursday.
            ("2026-08-17", "Thursday"),   # Mon -> Thu
            ("2026-08-18", "Friday"),     # Tue -> Fri
            ("2026-08-19", "Monday"),     # Wed -> Mon (skips the weekend)
            ("2026-08-20", "Tuesday"),    # Thu -> Tue
            ("2026-08-21", "Wednesday"),  # Fri -> Wed
            ("2026-08-22", "Wednesday"),  # Sat -> Wed
            ("2026-08-23", "Wednesday"),  # Sun -> Wed
        ],
    )
    def test_weekends_are_skipped(self, session, sent_on, expected_day):
        now = datetime.fromisoformat(sent_on + "T09:00:00+00:00")
        ctx = suggested_date_context(session, enrollment=None, step=None, now=now)
        assert ctx["suggested_day"] == expected_day
        assert ctx["suggested_date"].startswith(expected_day)

    def test_it_renders_in_the_step_timezone(self, session):
        """Late-evening UTC is already tomorrow in Asia/Kolkata, and the weekday
        is the entire value of this variable."""
        # 20:00 Sunday UTC is 01:30 Monday in Kolkata. Anchored on Sunday the
        # answer is Wednesday; anchored on Monday it is Thursday.
        late_sunday_utc = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
        step = SequenceStep(step_order=0, send_timezone="Asia/Kolkata")
        in_utc = suggested_date_context(session, enrollment=None, step=None, now=late_sunday_utc)
        in_india = suggested_date_context(
            session, enrollment=None, step=step, now=late_sunday_utc
        )
        assert in_utc["suggested_day"] == "Wednesday"
        assert in_india["suggested_day"] == "Thursday"

    def test_an_unknown_timezone_does_not_stop_a_send(self, session):
        step = SequenceStep(step_order=0, send_timezone="Mars/Olympus_Mons")
        ctx = suggested_date_context(
            session, enrollment=None, step=step, now=datetime(2026, 8, 17, 9, tzinfo=UTC)
        )
        assert ctx["suggested_day"] == "Thursday"

    def test_a_follow_up_keeps_the_date_the_first_email_proposed(
        self, session, account, outreach_fixture
    ):
        """The requirement that makes this variable non-trivial: the anchor is
        the first send, not this step's send. A follow-up two days later must
        not propose a later day than the email it is following up on."""
        sequence = outreach_fixture["sequence"]
        for step in sequence.steps:
            step.body_template = "Does {{ suggested_day }} work? ({{ suggested_date }})"
        candidate = outreach_fixture["candidates"][0]
        connection = MailboxConnection(
            org_id=account["org_id"], address="a@goforay.io", display_name="Alpha"
        )
        session.add(connection)
        session.flush()
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = connection.id
        # A distinct native id per send: the partial unique index on
        # (org_id, provider_native_id) is real, and a fake that reuses one hits
        # it rather than the behaviour under test.
        sent = []

        class CountingSender:
            name = "graph"

            def send(self, email):
                sent.append(email)
                nid = f"n{len(sent)}"
                return SendResult(
                    ok=True, provider_native_id=nid, internet_message_id=f"<{nid}@t>"
                )

        set_sender_factory("microsoft", lambda db, conn: CountingSender())
        enroll_candidates(session, sequence=sequence, candidate_ids=[candidate.id])
        monday = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        # enroll_candidates schedules against the real clock; this test runs on
        # a simulated Monday, so the first step has to be made due then.
        session.query(Enrollment).one().next_send_at = monday
        session.commit()

        run_due(session, org_id=sequence.org_id, now=monday)
        session.commit()
        first = session.query(Message).filter_by(direction="outbound").one()
        assert "Thursday" in first.body

        # Two days later the follow-up goes out. Recomputing from its own send
        # date would say Friday; anchoring to the first send keeps Thursday.
        enrollment = session.query(Enrollment).one()
        enrollment.next_send_at = monday + timedelta(days=2)
        session.flush()
        run_due(session, org_id=sequence.org_id, now=monday + timedelta(days=2, hours=1))
        session.commit()

        bodies = [
            m.body
            for m in session.query(Message)
            .filter_by(direction="outbound")
            .order_by(Message.step_id)
            .all()
        ]
        assert len(bodies) == 2
        assert all("Thursday" in b for b in bodies), bodies
        assert bodies[0] == bodies[1]


class TestSignOffDefaults:
    """The shipped defaults sign off with a first name, not a full name.

    Three places have to agree or a sequence signs differently depending on
    where it came from: the drafting prompt, the offline template arc, and the
    seeded starter steps.
    """

    def test_a_full_name_mailbox_still_signs_with_one_word(
        self, session, account, outreach_fixture
    ):
        """The case that prompted this: an Outlook mailbox whose display name is
        "Tushar Gupta" should sign "Best, Tushar"."""
        connection = MailboxConnection(
            org_id=account["org_id"],
            address="tushar@goforay.io",
            display_name="Tushar Gupta",
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        step.body_template = "Best," + chr(10) + "{{ sender_first_name }}"
        _, body, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=step,
            candidate=outreach_fixture["candidates"][0],
            connection=connection,
        )
        assert body == "Best," + chr(10) + "Tushar"


class TestPreviewMatchesTheSend:
    """A preview signed by the wrong person is worse than no preview.

    The bug this class exists for: preview_step rendered without a mailbox, so
    sender_first_name fell through to OUTREACH_FROM_NAME. A sequence pinned to
    tushar@ previewed as "Sathya" while a real send correctly used "Tushar" --
    and nothing on screen revealed the difference.
    """

    def _pin(self, session, account, outreach_fixture, address, display_name):
        connection = MailboxConnection(
            org_id=account["org_id"], address=address, display_name=display_name
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = connection.id
        for step in sequence.steps:
            step.body_template = "Best," + chr(10) + "{{ sender_first_name }}"
        session.flush()
        return connection, sequence

    def test_a_pinned_sequence_previews_as_that_mailbox(
        self, session, account, outreach_fixture, monkeypatch
    ):
        monkeypatch.setenv("OUTREACH_FROM_NAME", "Sathya Panchu")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        _, sequence = self._pin(
            session, account, outreach_fixture, "tushar@goforay.io", "Tushar Gupta"
        )
        _, body, _ = preview_step(
            session, sequence=sequence, candidate=outreach_fixture["candidates"][0]
        )
        # The configured default must not win over the mailbox that will send.
        assert body == "Best," + chr(10) + "Tushar"
        assert "Sathya" not in body

    def test_the_preview_and_the_send_agree(self, session, account, outreach_fixture):
        """Rendered twice, once through each path, and compared."""
        connection, sequence = self._pin(
            session, account, outreach_fixture, "tushar@goforay.io", "Tushar Gupta"
        )
        candidate = outreach_fixture["candidates"][0]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]

        _, previewed, _ = preview_step(session, sequence=sequence, candidate=candidate)
        _, sent, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=step,
            candidate=candidate,
            connection=connection,
        )
        assert previewed == sent

    def test_a_rotating_sequence_previews_as_a_real_pool_member(
        self, session, account, outreach_fixture, monkeypatch
    ):
        monkeypatch.setenv("OUTREACH_FROM_NAME", "Sathya Panchu")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        for address, name in [("anna@goforay.io", "Anna"), ("vivian@goforay.io", "Vivian")]:
            session.add(
                MailboxConnection(
                    org_id=account["org_id"], address=address, display_name=name
                )
            )
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.sender_mode = "rotate"
        sequence.mailbox_id = None
        for step in sequence.steps:
            step.body_template = "Best," + chr(10) + "{{ sender_first_name }}"
        set_sender_factory("microsoft", lambda db, conn: None)

        _, body, _ = preview_step(
            session, sequence=sequence, candidate=outreach_fixture["candidates"][0]
        )
        assert body in ("Best," + chr(10) + "Anna", "Best," + chr(10) + "Vivian")
        assert "Sathya" not in body

    def test_an_enrollment_pin_beats_the_sequence_in_the_preview(
        self, session, account, outreach_fixture
    ):
        """Previewing a specific enrollment must show the address that already
        emailed that person, not the sequence's current preference."""
        pinned = MailboxConnection(
            org_id=account["org_id"], address="anna@goforay.io", display_name="Anna"
        )
        _sequence_pin, sequence = self._pin(
            session, account, outreach_fixture, "tushar@goforay.io", "Tushar Gupta"
        )
        session.add(pinned)
        session.flush()
        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=outreach_fixture["candidates"][0].id,
            mailbox_id=pinned.id,
        )
        session.add(enrollment)
        session.flush()

        _, body, _ = preview_step(
            session,
            sequence=sequence,
            candidate=outreach_fixture["candidates"][0],
            enrollment=enrollment,
        )
        assert body == "Best," + chr(10) + "Anna"

    def test_a_capped_pool_still_previews_a_real_name(
        self, session, account, outreach_fixture, monkeypatch
    ):
        """A cap decides when a send happens, not who it is from. Falling back
        to the configured default here would show the wrong signature exactly
        when the pool was busy."""
        monkeypatch.setenv("OUTREACH_FROM_NAME", "Sathya Panchu")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        session.add(
            MailboxConnection(
                org_id=account["org_id"],
                address="anna@goforay.io",
                display_name="Anna",
                daily_send_cap=0,
            )
        )
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.sender_mode = "rotate"
        sequence.mailbox_id = None
        for step in sequence.steps:
            step.body_template = "Best," + chr(10) + "{{ sender_first_name }}"

        _, body, _ = preview_step(
            session, sequence=sequence, candidate=outreach_fixture["candidates"][0]
        )
        assert body == "Best," + chr(10) + "Anna"
