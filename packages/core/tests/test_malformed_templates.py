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
    build_message_content,
    enroll_candidates,
    preview_step,
    run_due,
)
from juicebox.outreach.templates import MalformedTemplate

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

    def test_a_long_unterminated_span_is_truncated_to_one_line(self):
        found = templates.malformed_placeholders("{{ " + "x" * 200 + "\nnext line")
        assert len(found) == 1
        assert found[0].endswith("…") and "\n" not in found[0]

    def test_repeats_are_reported_once_in_first_seen_order(self):
        found = templates.malformed_placeholders("{{B x}} {{A y}} {{B x}}")
        assert found == ["{{B x}}", "{{A y}}"]

    def test_a_broken_placeholder_beside_good_ones_is_isolated(self):
        template = "Hi {{ first_name }}, {{Bad name}} at {{ current_company }}"
        assert templates.malformed_placeholders(template) == ["{{Bad name}}"]

    def test_the_other_guards_still_cannot_see_it(self):
        """Why this function had to exist: missing_variables() and
        keep_known_variables() both iterate _VAR_RE, so the placeholder that
        actually shipped is invisible to them."""
        # Both see the well-formed placeholder beside it and neither sees this one.
        assert templates.missing_variables(BROKEN, {}) == ["sender_first_name"]
        assert (
            templates.keep_known_variables(BROKEN, ["first_name", "sender_first_name"])
            == BROKEN
        )
        assert "{{First name}}" in templates.render(BROKEN, {"first_name": "Ada"})


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

    def test_building_content_raises_before_any_model_call(
        self, session, broken_sequence
    ):
        sequence = broken_sequence["sequence"]
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        with pytest.raises(MalformedTemplate) as caught:
            build_message_content(
                session,
                sequence=sequence,
                step=step,
                candidate=broken_sequence["candidates"][0],
            )
        assert "{{First name}}" in str(caught.value)


class TestPreview:
    def test_preview_shows_the_raw_braces_rather_than_raising(
        self, session, broken_sequence
    ):
        """A preview must show what would actually go out. For this mistake
        that is the literal braces -- which is the thing that makes it obvious."""
        _, body, _ = preview_step(
            session,
            sequence=broken_sequence["sequence"],
            candidate=broken_sequence["candidates"][0],
        )
        assert "{{First name}}" in body

    def test_the_api_names_the_broken_placeholder(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "Broken preview",
                "personalize": False,
                "steps": [
                    {
                        "delay_days": 0,
                        "subject_template": "Hello {{ first_name }}",
                        "body_template": "Hi {{First name}},\n\nBest,\nAnna",
                    }
                ],
            },
            headers=headers,
        ).json()
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]

        response = client.post(
            f"/v1/sequences/{sequence['id']}/preview",
            json={"candidate_id": candidate_id},
            headers=headers,
        )
        assert response.status_code == 200
        preview = response.json()
        assert preview["warnings"] == ["{{First name}}"]
        assert "{{First name}}" in preview["body"]

    def test_a_clean_template_warns_about_nothing(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "Clean preview",
                "personalize": False,
                "steps": [
                    {
                        "delay_days": 0,
                        "subject_template": "Hello {{ first_name }}",
                        "body_template": "Hi {{ first_name | there }}, plain body.",
                    }
                ],
            },
            headers=headers,
        ).json()
        candidate_id = client.get("/v1/candidates?q=ada", headers=headers).json()[0]["id"]

        preview = client.post(
            f"/v1/sequences/{sequence['id']}/preview",
            json={"candidate_id": candidate_id},
            headers=headers,
        ).json()
        assert preview["warnings"] == []
