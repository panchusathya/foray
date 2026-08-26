"""Bullets for a role LinkedIn handed over blank — and the guard on them.

The complaint: "it has nothing on here." A profile fetch returns six real
jobs and no descriptions, because almost nobody fills in LinkedIn's
description box, and the resume built from it is a list of places somebody
has been.

The fix relocates what the candidate already told us during intake onto the
page. Which makes the guard the interesting part of this module, and most of
this file: a bullet arrives carrying the span of evidence it came from, and
anything that cannot be traced back to the candidate's own words character
for character is thrown away. The adversarial tests below hand the drafter a
transport that fabricates anyway — a plausible bullet with an invented quote,
a real quote with an inflated number bolted on — and assert the resume ships
bare instead.
"""

from __future__ import annotations

import json

from juicebox.docgen.bullets import (
    MAX_BULLETS_PER_ROLE,
    candidate_evidence,
    fill_missing_bullets,
)
from juicebox.llm.client import FakeTransport, LLMClient, LLMUnavailable, set_llm
from juicebox.messaging.threads import open_thread
from juicebox.models import Candidate, IntakeCall, IntakeMessage
from juicebox.repository import upsert_candidates

TRANSCRIPT = (
    "Agent: Tell me about the work at Camerbally.\n"
    "Candidate: I was on the payments team. I rewrote the settlement job that "
    "reconciles merchant payouts, and it took the nightly run from about six "
    "hours down to forty minutes, which meant support stopped getting paged "
    "at 3am.\n"
    "Agent: And the teaching assistant role?\n"
    "Candidate: I ran the lab sections for the intro systems course at Rice, "
    "about sixty students a semester, and I wrote the autograder that's still "
    "in use.\n"
)

SETTLEMENT_QUOTE = (
    "I rewrote the settlement job that reconciles merchant payouts, and it "
    "took the nightly run from about six hours down to forty minutes"
)
TEACHING_QUOTE = "I ran the lab sections for the intro systems course at Rice"
AUTOGRADER_QUOTE = "I wrote the autograder that's still in use"

BARE = {
    "contact": {"name": "Rico Camel", "email": "rico@example.com"},
    "summary": "",
    "experience": [
        {
            "company": "Camerbally",
            "title": "Software Engineer",
            "start": "2024-05",
            "end": "2025-05",
            "location": "New York, United States",
            "bullets": [],
        },
        {
            "company": "Rice University",
            "title": "Teaching Assistant",
            "start": "2024-08",
            "end": "2025-05",
            "location": "Houston, Texas, United States",
            "bullets": [],
        },
        {
            "company": "US Congress",
            "title": "Congressional Intern",
            "start": "2022-01",
            "end": "2022-11",
            "location": "",
            "bullets": [],
        },
    ],
    "education": [],
    "skills": [],
}


def _drafted(*roles) -> dict:
    return {"roles": list(roles)}


def _role(company: str, *pairs) -> dict:
    return {
        "company": company,
        "bullets": [{"text": text, "quote": quote} for text, quote in pairs],
    }


def _transport(payload: dict) -> FakeTransport:
    transport = FakeTransport(default=json.dumps(payload))
    set_llm(LLMClient(transport=transport, enabled=True))
    return transport


class TestDrafting:
    def test_a_role_the_candidate_described_gets_their_own_words(self):
        _transport(
            _drafted(
                _role(
                    "Camerbally",
                    (
                        "Rewrote the settlement job reconciling merchant payouts, "
                        "cutting the nightly run from six hours to forty minutes.",
                        SETTLEMENT_QUOTE,
                    ),
                )
            )
        )
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)

        assert added == 1
        assert "settlement job" in filled["experience"][0]["bullets"][0]
        # The other two roles were not described, so they stay bare.
        assert filled["experience"][1]["bullets"] == []
        assert filled["experience"][2]["bullets"] == []

    def test_the_input_resume_is_never_mutated(self):
        _transport(_drafted(_role("Camerbally", ("A bullet.", SETTLEMENT_QUOTE))))
        before = json.dumps(BARE, sort_keys=True)
        fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert json.dumps(BARE, sort_keys=True) == before

    def test_a_quote_survives_the_provider_reflowing_it(self):
        """Line breaks and smart quotes are not a change of meaning.

        A transcript arrives with the provider's wrapping and the model
        normalises an apostrophe on the way back. Failing an honest bullet
        over either would only teach us to loosen the guard that matters.
        """
        curly = "’"  # noqa: RUF001 - the point of the test is that it is curly
        reflowed = AUTOGRADER_QUOTE.replace(" ", "\n  ").replace("'", curly)
        _transport(_drafted(_role("Rice University", ("Wrote the autograder.", reflowed))))
        _, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == 1

    def test_more_bullets_than_a_role_can_hold_are_trimmed(self):
        _transport(
            _drafted(
                _role(
                    "Camerbally",
                    *[
                        (f"Rewrote the settlement job, take {word}.", SETTLEMENT_QUOTE)
                        for word in ("one", "two", "three", "four", "five", "six")
                    ],
                )
            )
        )
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == MAX_BULLETS_PER_ROLE
        assert len(filled["experience"][0]["bullets"]) == MAX_BULLETS_PER_ROLE

    def test_a_promotion_does_not_print_the_same_bullets_twice(self):
        """Two bare rows at one employer is what a promotion looks like."""
        promoted = json.loads(json.dumps(BARE))
        promoted["experience"].insert(
            1,
            {
                "company": "Camerbally",
                "title": "Senior Software Engineer",
                "start": "2025-05",
                "end": "2026-01",
                "location": "New York, United States",
                "bullets": [],
            },
        )
        _transport(_drafted(_role("Camerbally", ("Rewrote settlement.", SETTLEMENT_QUOTE))))

        filled, added = fill_missing_bullets(promoted, evidence=TRANSCRIPT)
        assert added == 1
        assert filled["experience"][0]["bullets"] == ["Rewrote settlement."]
        assert filled["experience"][1]["bullets"] == []

    def test_a_role_the_candidate_already_described_is_left_alone(self):
        described = json.loads(json.dumps(BARE))
        described["experience"][0]["bullets"] = ["What they wrote themselves"]
        _transport(_drafted(_role("Camerbally", ("Ours instead.", SETTLEMENT_QUOTE))))

        filled, added = fill_missing_bullets(described, evidence=TRANSCRIPT)
        assert added == 0
        assert filled["experience"][0]["bullets"] == ["What they wrote themselves"]

    def test_nothing_told_to_us_means_no_model_call_at_all(self):
        transport = _transport(_drafted(_role("Camerbally", ("x", SETTLEMENT_QUOTE))))
        filled, added = fill_missing_bullets(BARE, evidence="   ")
        assert (added, transport.calls) == (0, [])
        assert filled is BARE

    def test_no_model_configured_ships_the_bare_resume(self):
        set_llm(None)
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert (filled, added) == (BARE, 0)

    def test_a_model_failure_ships_the_bare_resume(self):
        set_llm(LLMClient(transport=FakeTransport([LLMUnavailable("down")]), enabled=True))
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert (filled, added) == (BARE, 0)


class TestTheGuard:
    """A transport that invents anyway, and the bullet that does not ship."""

    def test_an_invented_quote_is_discarded(self):
        _transport(
            _drafted(
                _role(
                    "US Congress",
                    (
                        "Briefed the member on constituent casework across two "
                        "committees.",
                        "I handled casework for two committees and briefed the member "
                        "every week.",
                    ),
                )
            )
        )
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        # Entirely plausible for a congressional intern, and never said.
        assert added == 0
        assert filled["experience"][2]["bullets"] == []

    def test_a_number_the_quote_does_not_carry_is_discarded(self):
        _transport(
            _drafted(
                _role(
                    "Rice University",
                    (
                        "Ran lab sections for the intro systems course for 400 "
                        "students a semester.",
                        TEACHING_QUOTE,
                    ),
                )
            )
        )
        # The transcript says sixty. Scale is the thing that gets checked.
        _, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == 0

    def test_a_number_the_quote_does_carry_survives(self):
        _transport(
            _drafted(
                _role(
                    "Camerbally",
                    (
                        "Cut the nightly settlement run from six hours to forty "
                        "minutes.",
                        SETTLEMENT_QUOTE,
                    ),
                )
            )
        )
        _, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == 1

    def test_a_quote_too_short_to_be_provenance_is_discarded(self):
        _transport(_drafted(_role("Camerbally", ("Rewrote a job.", "the job"))))
        _, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == 0

    def test_a_bullet_for_a_role_that_is_not_on_the_resume_lands_nowhere(self):
        _transport(_drafted(_role("Goldman Sachs", ("Ran the desk.", SETTLEMENT_QUOTE))))
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == 0
        assert [job["company"] for job in filled["experience"]] == [
            "Camerbally",
            "Rice University",
            "US Congress",
        ]

    def test_a_bullet_longer_than_a_bullet_is_discarded(self):
        _transport(
            _drafted(
                _role("Camerbally", (" ".join(["word"] * 60) + ".", SETTLEMENT_QUOTE))
            )
        )
        _, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert added == 0

    def test_a_malformed_response_ships_the_bare_resume(self):
        _transport({"roles": "not a list"})
        filled, added = fill_missing_bullets(BARE, evidence=TRANSCRIPT)
        assert (filled, added) == (BARE, 0)


class TestEvidence:
    """Their words. Not ours, and not a vendor's guess at them."""

    def _candidate(self, session, org_id) -> Candidate:
        upsert_candidates(
            session,
            org_id=org_id,
            payloads=[
                {
                    "source": "intake_thread",
                    "source_id": "rico@example.com",
                    "full_name": "Rico Camel",
                    "emails": ["rico@example.com"],
                }
            ],
        )
        session.commit()
        return session.query(Candidate).filter_by(full_name="Rico Camel").one()

    def test_a_call_transcript_is_evidence(self, session, account):
        candidate = self._candidate(session, account["org_id"])
        session.add(
            IntakeCall(
                org_id=account["org_id"],
                candidate_id=candidate.id,
                provider="fake",
                provider_call_id="call-1",
                status="completed",
                transcript=TRANSCRIPT,
            )
        )
        session.commit()
        evidence = candidate_evidence(session, candidate)
        assert SETTLEMENT_QUOTE in evidence
        # The agent's half of the dialogue is full of quotable-looking
        # sentences the candidate never said, so it is not in the evidence
        # a bullet can be grounded in.
        assert "Tell me about the work" not in evidence

    def test_only_the_candidate_side_of_a_thread_is_evidence(self, session, account):
        candidate = self._candidate(session, account["org_id"])
        thread = open_thread(
            session, org_id=account["org_id"], candidate_id=candidate.id
        )
        session.add_all(
            [
                IntakeMessage(
                    org_id=account["org_id"],
                    thread_id=thread.id,
                    seq=1,
                    direction="outbound",
                    body="What did you build at Camerbally?",
                ),
                IntakeMessage(
                    org_id=account["org_id"],
                    thread_id=thread.id,
                    seq=2,
                    direction="inbound",
                    body="I rewrote the settlement job on the payments team.",
                ),
            ]
        )
        session.commit()

        evidence = candidate_evidence(session, candidate)
        assert "I rewrote the settlement job" in evidence
        # Our agent's question is not a claim the candidate can be held to.
        assert "What did you build" not in evidence

    def test_a_candidate_who_has_told_us_nothing_has_no_evidence(self, session, account):
        candidate = self._candidate(session, account["org_id"])
        assert candidate_evidence(session, candidate) == ""

    def test_another_candidates_call_is_not_evidence(self, session, account):
        candidate = self._candidate(session, account["org_id"])
        session.add(
            IntakeCall(
                org_id=account["org_id"],
                candidate_id=None,
                provider="fake",
                provider_call_id="call-stranger",
                status="completed",
                transcript=TRANSCRIPT,
            )
        )
        session.commit()
        assert candidate_evidence(session, candidate) == ""


STRIPE_QUOTE = "at Stripe I built the model that scores refund abuse on the risk team"

STRIPE_TRANSCRIPT = (
    "Agent: What were you doing before Modal?\n"
    f"Candidate: {STRIPE_QUOTE}, and I owned it through two holiday peaks.\n"
)


class TestThroughTheProfileResume:
    """The whole promise, end to end: a bare role comes back described."""

    def _ada_with_a_call(self, session, account, sample_candidates) -> Candidate:
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
        session.commit()
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        session.add(
            IntakeCall(
                org_id=account["org_id"],
                candidate_id=ada.id,
                provider="fake",
                provider_call_id="call-ada",
                status="completed",
                transcript=STRIPE_TRANSCRIPT,
            )
        )
        session.commit()
        return ada

    def test_a_bare_role_reaches_the_pdf_described(
        self, session, account, sample_candidates
    ):
        from juicebox.docgen.parse import extract_pdf_text
        from juicebox.docgen.profile import refresh_profile_resume
        from juicebox.storage import read_bytes

        ada = self._ada_with_a_call(session, account, sample_candidates)
        # Stripe is the role the sample candidate has no description for.
        _transport(
            _drafted(
                _role(
                    "Stripe",
                    ("Built the model scoring refund abuse for the risk team.", STRIPE_QUOTE),
                )
            )
        )

        doc, error = refresh_profile_resume(session, ada)
        session.commit()

        assert error == ""
        # A drafted bullet is the model's work, and the row says so.
        assert doc.generated_by == "llm"
        text = extract_pdf_text(read_bytes(doc.storage_path))
        assert "refund abuse" in text
        # Modal's own description is untouched by any of this.
        assert "inference platform" in text

    def test_an_ungrounded_draft_leaves_the_role_bare_and_the_row_honest(
        self, session, account, sample_candidates
    ):
        from juicebox.docgen.parse import extract_pdf_text
        from juicebox.docgen.profile import refresh_profile_resume
        from juicebox.storage import read_bytes

        ada = self._ada_with_a_call(session, account, sample_candidates)
        _transport(
            _drafted(
                _role(
                    "Stripe",
                    (
                        "Led the fraud platform rewrite across four teams.",
                        "I led the fraud platform rewrite across four teams at Stripe.",
                    ),
                )
            )
        )

        doc, error = refresh_profile_resume(session, ada)
        session.commit()

        assert error == ""
        assert doc.generated_by == "template"
        assert "fraud platform" not in extract_pdf_text(read_bytes(doc.storage_path))
