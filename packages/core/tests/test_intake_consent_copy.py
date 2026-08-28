"""The consent copy, and the line between a sentence and a mechanism.

Every greeting and the voice wrap-up used to carry a sentence about stopping.
They no longer do -- see ``INTAKE_COMPLIANCE.md`` section 6a for the reasoning
and, more importantly, for the exposure it creates. What these tests are for is
the distinction that change makes easy to lose:

**Removing a sentence about a mechanism is not removing the mechanism.** STOP
still stops. HELP is still held out of the voice transform. The single
acknowledgement a carrier expects still goes out. If a later edit ever takes
the copy change one step further and touches the handling, ``TestKeywordsStillWork``
is what fails.

And the consent *question* is not copy. It is the TCPA opt-in itself, so it
stays word for word -- what was removed is the sentence that followed it.
"""

from __future__ import annotations

from juicebox.messaging import prompts
from juicebox.messaging.consent import (
    DISCLOSURE_INBOUND_TEXT,
    DISCLOSURE_INBOUND_TEXT_VERSION,
    DISCLOSURE_VOICE_SMS,
    DISCLOSURE_VOICE_SMS_VERSION,
    DISCLOSURE_WEB_WIDGET,
    DISCLOSURE_WEB_WIDGET_VERSION,
)
from juicebox.messaging.linkedin_flow import intro_message
from juicebox.messaging.prompts import greeting_for


def _greetings() -> dict[str, str]:
    return {
        "web": greeting_for(org="Acme Talent", recruiter="vivian"),
        "linkedin_first": intro_message(recruiter="vivian", org="Acme Talent"),
        # The name == org rendering ("it's foray") is its own greeting surface.
        "linkedin_same_name": intro_message(recruiter="Foray", org="Foray"),
        "template": prompts.GREETING.format(recruiter="vivian", org="Acme Talent"),
    }


class TestNoRecitals:
    def test_no_greeting_recites_the_opt_out(self):
        for name, body in _greetings().items():
            lowered = body.lower()
            assert "stop any time" not in lowered, name
            assert "opt out" not in lowered, name
            assert "help for info" not in lowered, name

    def test_no_greeting_names_a_carrier_keyword(self):
        for name, body in _greetings().items():
            for keyword in ("STOP", "HELP", "UNSTOP", "START"):
                assert keyword not in body, f"{name} still says {keyword}"


class TestTheDisclosuresStayTrue:
    """A stored disclosure is the defence, so it has to match the message."""

    def test_every_disclosure_moved_to_v2(self):
        assert DISCLOSURE_WEB_WIDGET_VERSION == "web-widget-v2"
        assert DISCLOSURE_INBOUND_TEXT_VERSION == "inbound-text-v2"
        assert DISCLOSURE_VOICE_SMS_VERSION == "voice-wrapup-v2"

    def test_the_quoted_greeting_no_longer_promises_an_opt_out(self):
        assert "stop any time" not in DISCLOSURE_WEB_WIDGET.lower()

    def test_the_voice_disclosure_keeps_the_question_and_drops_the_recital(self):
        """The question *is* the consent. Removing it would be removing consent."""
        assert "Can I text you updates at this number?" in DISCLOSURE_VOICE_SMS
        assert "reply STOP any time to opt out" not in DISCLOSURE_VOICE_SMS
        assert "explicit, unambiguous" in DISCLOSURE_VOICE_SMS

    def test_the_inbound_disclosure_still_claims_the_keywords_are_honoured(self):
        """Because they are -- and that claim is now the only place it is made."""
        assert "STOP/HELP are honoured" in DISCLOSURE_INBOUND_TEXT


class TestTheAgentStillAsks:
    """Read out of the deployed agent definition, not out of a copy of it.

    ``deploy/`` is stdlib-only and cannot import the app, which is why
    ``test_the_persona_matches_the_deploy_script`` already exists. Same trick:
    the prompt is the artifact, so assert against the artifact.
    """

    def _prompt(self) -> str:
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[3]
        source = (root / "deploy" / "retell_agents.py").read_text()
        start = source.index("_SHARED_RULES = ")
        block = source[start : source.index('"""', source.index('"""', start) + 3)]
        # Whitespace-normalised: the prompt is hard-wrapped prose, so asserting
        # on it verbatim makes every reflow a test failure about nothing.
        return re.sub(r"\s+", " ", block)

    def test_the_consent_question_is_unchanged(self):
        assert "can I text you updates at this number?" in self._prompt()

    def test_the_recital_is_gone_from_the_ask(self):
        assert "reply STOP any time to opt out" not in self._prompt()

    def test_the_agent_is_told_not_to_add_one(self):
        """Otherwise a model helpfully supplies the sentence we just removed."""
        assert "Do not add anything about stopping or opting out" in self._prompt()

    def test_it_still_answers_a_direct_question_about_stopping(self):
        """Refusing to say how to stop when asked would be a different thing."""
        assert "If they ask how to stop" in self._prompt()


class TestKeywordsStillWork:
    """The mechanism, which the copy change must never reach."""

    def test_stop_is_still_recognised(self):
        from juicebox.messaging.suppression import looks_like_stop

        assert looks_like_stop("STOP") is True
        assert looks_like_stop(" stop. ") is True
        assert looks_like_stop("stop me if you have heard this one") is False

    def test_the_acknowledgement_still_exists(self):
        assert prompts.STOP_ACKNOWLEDGEMENT.strip()

    def test_keywords_survive_the_lowercase_transform(self):
        """Held out for the same reason URLs are: they are not our prose."""
        assert "STOP" in prompts.lowercase_outbound("Reply STOP to opt out")
        assert "HELP" in prompts.lowercase_outbound("Reply HELP for info")
