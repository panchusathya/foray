"""The two variables a rotating mailbox pool made necessary.

`sender_name` exists because one sequence now sends from several mailboxes, so
a hardcoded signature would sign half the outreach as the wrong person.
`suggested_date` exists because a follow-up that recomputed its own date would
contradict the email before it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from juicebox.models import Enrollment, MailboxConnection, Message, SequenceStep, utcnow
from juicebox.outreach.personalize import sender_context
from juicebox.outreach.sender import SendResult, set_sender_factory
from juicebox.outreach.sequencer import (
    SUGGESTED_DATE_BUSINESS_DAYS,
    build_message_content,
    enroll_candidates,
    run_due,
    suggested_date_context,
)


class TestSenderName:
    def test_the_mailbox_display_name_wins(self):
        connection = MailboxConnection(
            address="tushar@goforay.io", display_name="Tushar Gupta"
        )
        ctx = sender_context(connection, None, fallback_name="Talent Team")
        assert ctx["sender_name"] == "Tushar Gupta"
        assert ctx["sender_first_name"] == "Tushar"
        assert ctx["sender_email"] == "tushar@goforay.io"

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

    def test_no_mailbox_falls_back_to_the_sequence_then_the_default(self, outreach_fixture):
        sequence = outreach_fixture["sequence"]
        sequence.from_name = "Acme Talent"
        assert sender_context(None, sequence)["sender_name"] == "Acme Talent"
        sequence.from_name = ""
        ctx = sender_context(None, sequence, fallback_name="Configured Default")
        assert ctx["sender_name"] == "Configured Default"

    def test_it_is_never_empty(self):
        """An empty signature is worse than a slightly wrong one."""
        assert sender_context(None, None)["sender_name"] == "the team"
        assert sender_context(MailboxConnection(address="", display_name=""))["sender_name"]

    def test_rendering_signs_off_with_the_sending_mailbox(
        self, session, account, outreach_fixture
    ):
        connection = MailboxConnection(
            org_id=account["org_id"],
            address="zoho-one@goforay.io",
            display_name="Sathya Panchu",
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        step.body_template = "Hi {{ first_name }},\n\nBest,\n{{ sender_name }}"

        _, body, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=step,
            candidate=outreach_fixture["candidates"][0],
            connection=connection,
        )
        assert body.endswith("Best,\nSathya Panchu")

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

    def test_monday_gives_thursday_the_full_date(self, session):
        now = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)  # a Monday
        ctx = suggested_date_context(session, enrollment=None, step=None, now=now)
        assert ctx["suggested_date"] == "Thursday 20 August"

    def test_the_offset_is_two_clear_business_days(self):
        """Named so the convention is checkable rather than folklore."""
        assert SUGGESTED_DATE_BUSINESS_DAYS == 3

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

    def test_before_any_send_the_anchor_is_now(self, session, outreach_fixture):
        """A preview has nothing to anchor to, so it shows today's answer."""
        enrollment = Enrollment(
            sequence_id=outreach_fixture["sequence"].id,
            candidate_id=outreach_fixture["candidates"][0].id,
        )
        session.add(enrollment)
        session.flush()
        now = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        ctx = suggested_date_context(session, enrollment=enrollment, step=None, now=now)
        assert ctx["suggested_day"] == "Thursday"


class TestBothInOneRender:
    def test_a_template_can_use_every_new_variable(
        self, session, account, outreach_fixture
    ):
        connection = MailboxConnection(
            org_id=account["org_id"], address="z@goforay.io", display_name="Sathya Panchu"
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        step.body_template = (
            "Hi {{ first_name }},\n\nAre you free {{ suggested_day }}?\n\n"
            "Best,\n{{ sender_first_name }}"
        )
        _, body, context, _ = build_message_content(
            session,
            sequence=sequence,
            step=step,
            candidate=outreach_fixture["candidates"][0],
            connection=connection,
            now=utcnow(),
        )
        assert "Best,\nSathya" in body
        assert context["sender_name"] == "Sathya Panchu"
        assert context["suggested_day"] in {
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
        }
