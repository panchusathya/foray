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
from juicebox.messaging.consent import DISCLOSURE_VOICE_SMS


class TestTheDisclosuresStayTrue:
    """A stored disclosure is the defence, so it has to match the message."""

    def test_the_voice_disclosure_keeps_the_question_and_drops_the_recital(self):
        """The question *is* the consent. Removing it would be removing consent."""
        assert "Can I text you updates at this number?" in DISCLOSURE_VOICE_SMS
        assert "reply STOP any time to opt out" not in DISCLOSURE_VOICE_SMS
        assert "explicit, unambiguous" in DISCLOSURE_VOICE_SMS


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


class TestCarrierReplyCopy:
    """The two strings a 10DLC vetter compares against the registration.

    Campaign CM8460efcf3183cf6b3a79ffec2f5da4b1 was rejected twice on consent
    verification, and one reason a reviewer could not tie the number to the
    brand was that HELP answered as "juicebox intake assistant" -- a name on no
    website and no filing. These assert the properties CTIA actually requires,
    not the exact wording, so the copy can still be tuned.
    """

    def test_help_identifies_the_brand_and_a_way_to_reach_a_human(self):
        reply = prompts.HELP_REPLY
        assert "Foray" in reply
        assert "@goforay.io" in reply or "goforay.io" in reply
        assert "STOP" in reply
        assert "juicebox" not in reply.lower()

    def test_opt_out_confirmation_identifies_the_brand(self):
        assert "Foray" in prompts.STOP_ACKNOWLEDGEMENT

    def test_first_text_names_the_registered_brand_not_the_old_one(self):
        greeting = prompts.VOICE_VERIFICATION_GREETING
        assert "Foray" in greeting
        assert "Go4A" not in greeting

    def test_first_text_asks_for_a_declared_opt_in_keyword(self):
        """The sample message and the campaign's opt-in keywords must agree."""
        assert "YES" in prompts.VOICE_VERIFICATION_GREETING
