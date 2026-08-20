"""The LLM is only called when the template consumes what it writes.

personalize() costs a live model call — 24s measured against production's
reasoning model, versus 5ms for the heuristic path — and was gated only on
`sequence.personalize`, never on whether the step referenced any of
`opening_line` / `why_relevant` / `subject_hint`. Four of six production steps
referenced none of them, so every preview and every send paid for text that was
then discarded.

These tests assert the call count, not the output, because "did we spend money
and make the user wait" is the actual behaviour under test.
"""

from __future__ import annotations

import pytest

from juicebox.llm.client import FakeTransport, LLMClient, set_llm
from juicebox.outreach import templates
from juicebox.outreach.personalize import reset_fragment_cache
from juicebox.outreach.sequencer import build_message_content, preview_step

FRAGMENTS = {
    "opening_line": "Model-written opening.",
    "why_relevant": "Model-written relevance.",
    "subject_hint": "Model-written subject",
    "hooks": ["a hook"],
}


@pytest.fixture
def counting_llm():
    """A live-looking LLM whose calls are counted.

    FakeTransport already records every call in ``.calls``, so there is nothing
    to subclass -- wrapping it to count again just double-counts.
    """
    reset_fragment_cache()
    transport = FakeTransport(default=FRAGMENTS)
    set_llm(LLMClient(transport=transport, enabled=True))
    yield transport
    set_llm(None)


def _set_step(sequence, *, subject: str, body: str, personalize: bool = True):
    """The shared fixture ships personalize=False; these tests are about what
    happens when it is on, so turn it on unless a case says otherwise."""
    sequence.personalize = personalize
    step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
    step.subject_template = subject
    step.body_template = body
    return step


class TestUsesLlmVariables:
    """The pure predicate, so the gate itself is checkable."""

    @pytest.mark.parametrize(
        "template, expected",
        [
            ("Hi {{ first_name }}", False),
            ("Hi {{ first_name }}, {{ opening_line }}", True),
            ("{{why_relevant}}", True),
            ("{{ subject_hint | A quick note }}", True),
            ("Best,\n{{ sender_first_name }}", False),
            ("Does {{ suggested_day }} work?", False),
            ("", False),
            # A directive is not a written variable: it makes its own call.
            ("{{ai: say why this fits}}", False),
        ],
    )
    def test_detects_only_model_written_variables(self, template, expected):
        assert templates.uses_llm_variables(template) is expected

    def test_the_set_matches_what_personalize_produces(self):
        """If the schema grows a field, this list has to grow with it."""
        from juicebox.llm.prompts import PERSONALIZE_SCHEMA

        written = set(PERSONALIZE_SCHEMA["properties"]) - {"hooks"}
        assert written == templates.LLM_WRITTEN_VARIABLES


class TestNoCallWhenNothingConsumesIt:
    def test_a_template_without_written_variables_makes_no_call(
        self, session, outreach_fixture, counting_llm
    ):
        """The production case: hand-written copy with no model variables."""
        sequence = outreach_fixture["sequence"]
        _set_step(
            sequence,
            subject="Founding engineer at Foray",
            body="Hi {{ first_name }},\n\nWe are hiring.\n\nBest,\n{{ sender_first_name }}",
        )
        _, body, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=sorted(sequence.steps, key=lambda s: s.step_order)[0],
            candidate=outreach_fixture["candidates"][0],
        )
        assert counting_llm.calls == []
        assert "We are hiring." in body

    def test_a_template_that_uses_one_still_calls(
        self, session, outreach_fixture, counting_llm
    ):
        sequence = outreach_fixture["sequence"]
        _set_step(sequence, subject="A role", body="Hi,\n\n{{ opening_line }}")
        _, body, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=sorted(sequence.steps, key=lambda s: s.step_order)[0],
            candidate=outreach_fixture["candidates"][0],
        )
        assert len(counting_llm.calls) == 1
        assert "Model-written opening." in body

    def test_an_empty_subject_still_calls(self, session, outreach_fixture, counting_llm):
        """build_message_content falls back to context['subject_hint'] to title
        the email, so an empty subject consumes model text even though no
        {{ }} names it."""
        sequence = outreach_fixture["sequence"]
        _set_step(sequence, subject="", body="Hi {{ first_name }}, no variables here.")
        subject, _, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=sorted(sequence.steps, key=lambda s: s.step_order)[0],
            candidate=outreach_fixture["candidates"][0],
        )
        assert len(counting_llm.calls) == 1
        assert subject == "Model-written subject"

    def test_personalize_false_still_wins(self, session, outreach_fixture, counting_llm):
        """The operator's off switch is unchanged; the new gate only skips work
        nobody reads."""
        sequence = outreach_fixture["sequence"]
        _set_step(
            sequence, subject="A role", body="{{ opening_line }}", personalize=False
        )
        _, body, _, _ = build_message_content(
            session,
            sequence=sequence,
            step=sorted(sequence.steps, key=lambda s: s.step_order)[0],
            candidate=outreach_fixture["candidates"][0],
        )
        assert counting_llm.calls == []
        # The heuristic fragment is used instead of the model's.
        assert "Model-written opening." not in body
        assert body.strip()

    def test_the_preview_path_is_gated_too(self, session, outreach_fixture, counting_llm):
        sequence = outreach_fixture["sequence"]
        _set_step(sequence, subject="A role", body="Hi {{ first_name }}, nothing written.")
        preview_step(
            session, sequence=sequence, candidate=outreach_fixture["candidates"][0]
        )
        assert counting_llm.calls == []


class TestFragmentCache:
    def test_a_second_render_of_the_same_candidate_does_not_call_again(
        self, session, outreach_fixture, counting_llm
    ):
        """A preview has no enrollment, so enrollment.personalization — the only
        cache that existed — could never serve it."""
        sequence = outreach_fixture["sequence"]
        _set_step(sequence, subject="A role", body="{{ opening_line }}")
        candidate = outreach_fixture["candidates"][0]
        for _ in range(3):
            preview_step(session, sequence=sequence, candidate=candidate)
        assert len(counting_llm.calls) == 1

    def test_a_different_candidate_is_a_different_entry(
        self, session, outreach_fixture, counting_llm
    ):
        sequence = outreach_fixture["sequence"]
        _set_step(sequence, subject="A role", body="{{ opening_line }}")
        for candidate in outreach_fixture["candidates"][:2]:
            preview_step(session, sequence=sequence, candidate=candidate)
        assert len(counting_llm.calls) == 2


class TestFastBudget:
    def test_personalize_uses_the_fast_model_and_no_retries(
        self, session, outreach_fixture, counting_llm
    ):
        """A user is waiting on this call, so it must not inherit the 120s
        drafting budget or its two retries."""
        sequence = outreach_fixture["sequence"]
        _set_step(sequence, subject="A role", body="{{ opening_line }}")
        preview_step(
            session, sequence=sequence, candidate=outreach_fixture["candidates"][0]
        )
        [call] = counting_llm.calls
        from juicebox.config import get_settings

        assert call["model"] == get_settings().resolved_llm_fast_model
        assert call["timeout"] == get_settings().llm_fast_timeout_seconds

    def test_a_failing_fast_call_is_not_retried(
        self, session, outreach_fixture
    ):
        """Retrying a call somebody is watching just multiplies the wait; the
        heuristic text is right there."""
        reset_fragment_cache()
        transport = FakeTransport(responses=[TimeoutError("too slow")], default=FRAGMENTS)
        set_llm(LLMClient(transport=transport, enabled=True))
        try:
            sequence = outreach_fixture["sequence"]
            _set_step(sequence, subject="A role", body="{{ opening_line }}")
            _, body, _, _ = build_message_content(
                session,
                sequence=sequence,
                step=sorted(sequence.steps, key=lambda s: s.step_order)[0],
                candidate=outreach_fixture["candidates"][0],
            )
            # One attempt, then the heuristic fallback — not three attempts.
            assert len(transport.calls) == 1
            assert "Model-written opening." not in body
            assert body.strip()
        finally:
            set_llm(None)
