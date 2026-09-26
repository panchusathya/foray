"""Placeholders the renderer cannot parse must never reach a candidate.

``{{First name}}`` -- a space is not a legal variable character -- matched
neither _VAR_RE nor _AI_RE, so ``render()`` left it alone and an email went out
reading "Hi {{First name}},". Every guard that existed keyed off the same two
patterns that failed to match it, so nothing saw it. These tests pin the third
guard: the send path refuses, and the preview says why.
"""

from __future__ import annotations

import pytest

from juicebox.models import Enrollment, Message
from juicebox.outreach import templates
from juicebox.outreach.sequencer import (
    enroll_candidates,
    run_due,
)

BROKEN = "Hi {{First name}},\n\nBest,\n{{ sender_first_name }}"


@pytest.fixture
def broken_sequence(session, outreach_fixture):
    """The seeded fixture with step 1's body rewritten as the real email was."""
    sequence = outreach_fixture["sequence"]
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    steps[0].subject_template = "About {{ role_title }}"
    steps[0].body_template = BROKEN
    session.commit()
    return outreach_fixture


class TestDetection:
    @pytest.mark.parametrize(
        "template, expected",
        [
            ("Hi {{First name}},", ["{{First name}}"]),
            ("Hi {{ First name }},", ["{{ First name }}"]),
            ("Hi {{first name}},", ["{{first name}}"]),
            ("{{Company Name}} is hiring", ["{{Company Name}}"]),
        ],
    )
    def test_a_space_in_the_name_is_reported(self, template, expected):
        assert templates.malformed_placeholders(template) == expected

    @pytest.mark.parametrize(
        "template",
        [
            "Hi {{ first_name }},",
            "Hi {{ first_name | there }},",
            "{{ai: say why this fits}}",
            "{{ AI : mention their stack }}",
            "Plain copy with no placeholders at all.",
            "",
        ],
    )
    def test_valid_templates_report_nothing(self, template):
        assert templates.malformed_placeholders(template) == []

    def test_an_unterminated_placeholder_is_reported(self):
        """It has no closing braces to match on, and ships just as literally."""
        assert templates.malformed_placeholders("Hi {{ first_name,") == [
            "{{ first_name,"
        ]
        assert templates.malformed_placeholders("{{ first_name\nnext line") == [
            "{{ first_name"
        ]


class TestSending:
    def _enroll_and_run(self, session, fixture):
        sequence = fixture["sequence"]
        ada = next(c for c in fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        summary = run_due(session, org_id=sequence.org_id)
        session.commit()
        return summary

    def test_the_send_fails_and_nothing_leaves_the_mailbox(
        self, session, broken_sequence, mailbox
    ):
        summary = self._enroll_and_run(session, broken_sequence)

        assert summary.failed == 1 and summary.sent == 0
        assert mailbox.outbox == []
        message = session.query(Message).one()
        assert message.status == "failed"
        assert message.error.startswith("malformed_template")
        assert "{{First name}}" in message.error
        assert "{{First name}}" not in (message.body or "")

    def test_the_enrollment_survives_so_a_fixed_template_sends(
        self, session, broken_sequence, mailbox
    ):
        """The fix is a human editing the step. Stopping the enrollment would
        make them re-enrol the candidate to recover from a typo."""
        self._enroll_and_run(session, broken_sequence)

        enrollment = session.query(Enrollment).one()
        assert enrollment.status == "active"
        assert enrollment.current_step == 0
        assert enrollment.next_send_at is not None  # backed off, not hot-looped

        sequence = broken_sequence["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        step.body_template = "Hi {{ first_name | there }},\n\nBest,\n{{ sender_first_name }}"
        enrollment.next_send_at = enrollment.created_at
        session.commit()

        summary = run_due(session, org_id=sequence.org_id)
        session.commit()
        assert summary.sent == 1
        assert "Hi Ada," in mailbox.outbox[0].text_body

    def test_a_broken_subject_is_caught_too(self, session, broken_sequence, mailbox):
        sequence = broken_sequence["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        step.subject_template = "A role for {{First name}}"
        step.body_template = "Hi {{ first_name }},"
        session.commit()

        assert self._enroll_and_run(session, broken_sequence).sent == 0
        assert mailbox.outbox == []
