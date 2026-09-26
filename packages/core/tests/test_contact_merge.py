"""The one writer for "they told us their email, number or name".

Two channels ask a candidate the same two questions, and until this existed
they did different things with the answers: the call path added a phone and an
address and ignored the name, the thread path wrote none of the three onto the
candidate at all. So a texter's number never reached ``candidate_phones``, and
the dial-in line did not recognise them when they rang from the phone they had
just been texting from.

Three rules live here because each one is easy to get wrong on its own, and
each one has a specific consequence when it is:

**A detail somebody else owns is reported, never taken.** Appending to
``Candidate.emails`` before checking would leave the JSON column claiming an
address the projection does not have -- a half-write that reads as ownership
everywhere except the index that decides ownership.

**A placeholder name loses to a real one; a real name never loses.** The first
channel to reach somebody must not permanently decide what they are called, and
two real names disagreeing is a merge question rather than an import.

**A newly learned address for a suppressed person is suppressed too**, or
do-not-contact is defeated by the person mentioning a second mailbox.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.models import Candidate, CandidateEmail, CandidatePhone
from juicebox.outreach.suppressions import is_suppressed, suppress
from juicebox.repository import (
    is_placeholder_name,
    merge_contact_details,
    sync_candidate_emails,
    sync_candidate_phones,
)


def a_candidate(session, account, *, name, emails=(), phones=()):
    candidate = Candidate(
        org_id=account["org_id"],
        source="test",
        source_id=f"test-{name}-{'-'.join(emails)}-{'-'.join(phones)}",
        full_name=name,
        emails=list(emails),
        phones=list(phones),
    )
    session.add(candidate)
    session.flush()
    sync_candidate_emails(session, candidate)
    sync_candidate_phones(session, candidate)
    return candidate


class TestPlaceholderName:
    @pytest.mark.parametrize(
        "name", ["", "   ", "priya@example.com", "+14155550123", "(415) 555-0123"]
    )
    def test_a_stand_in_is_recognised(self, session, account, name):
        assert is_placeholder_name(a_candidate(session, account, name=name))

    @pytest.mark.parametrize("name", ["Priya Raman", "Priya", "O'Brien", "Jean-Luc Picard"])
    def test_a_real_name_is_not(self, session, account, name):
        assert not is_placeholder_name(a_candidate(session, account, name=name))


class TestMerge:

    def test_the_projections_are_written_too(self, session, account):
        """A JSON column nothing indexed is a detail no lookup can find."""
        candidate = a_candidate(session, account, name="Priya Raman")
        merge_contact_details(
            session, candidate, email="priya@example.com", phone="+14155550123"
        )
        assert session.scalar(
            select(CandidateEmail.candidate_id).where(
                CandidateEmail.email == "priya@example.com"
            )
        ) == candidate.id
        assert session.scalar(
            select(CandidatePhone.candidate_id).where(CandidatePhone.phone == "+14155550123")
        ) == candidate.id

    def test_a_number_already_held_in_another_spelling_is_not_duplicated(
        self, session, account
    ):
        """"(415) 555-0123" and "+1 415 555 0123" are one number."""
        candidate = a_candidate(session, account, name="Priya Raman", phones=["(415) 555-0123"])
        merged = merge_contact_details(session, candidate, phone="+14155550123")
        assert merged.phones_added == ()

    def test_an_address_somebody_else_owns_is_not_taken(self, session, account):
        owner = a_candidate(session, account, name="Priya Raman", emails=["priya@example.com"])
        other = a_candidate(session, account, name="Someone Else")

        merged = merge_contact_details(session, other, email="priya@example.com")
        assert merged.conflicts == ("priya@example.com",)
        assert "priya@example.com" not in (other.emails or [])
        assert session.scalar(
            select(CandidateEmail.candidate_id).where(
                CandidateEmail.email == "priya@example.com"
            )
        ) == owner.id

    def test_a_number_somebody_else_owns_is_not_taken(self, session, account):
        """A wrong row here points the dial-in line at the wrong person."""
        owner = a_candidate(session, account, name="Priya Raman", phones=["+14155550123"])
        other = a_candidate(session, account, name="Someone Else")

        merged = merge_contact_details(session, other, phone="+14155550123")
        assert merged.conflicts == ("+14155550123",)
        assert session.scalar(
            select(CandidatePhone.candidate_id).where(CandidatePhone.phone == "+14155550123")
        ) == owner.id

    def test_a_number_with_no_country_code_is_not_stored_as_a_key(self, session, account):
        """``lookup_key`` refuses to guess a country, and so does this."""
        candidate = a_candidate(session, account, name="Priya Raman")
        merged = merge_contact_details(session, candidate, phone="555 0123")
        assert merged.phones_added == ()


class TestNaming:
    def test_a_placeholder_is_promoted(self, session, account):
        candidate = a_candidate(
            session, account, name="priya@example.com", emails=["priya@example.com"]
        )
        merged = merge_contact_details(session, candidate, name="Priya Raman")
        assert merged.named
        assert candidate.full_name == "Priya Raman"
        assert (candidate.first_name, candidate.last_name) == ("Priya", "Raman")

    def test_a_real_name_survives(self, session, account):
        candidate = a_candidate(session, account, name="Priyanka Raman")
        merged = merge_contact_details(session, candidate, name="Priya Raman")
        assert not merged.named
        assert candidate.full_name == "Priyanka Raman"


class TestSuppression:
    def test_a_new_address_for_a_suppressed_person_is_suppressed(self, session, account):
        """Otherwise do-not-contact is defeated by a second mailbox."""
        candidate = a_candidate(session, account, name="Priya Raman", emails=["old@example.com"])
        suppress(session, org_id=account["org_id"], email="old@example.com", reason="unsubscribe")

        merged = merge_contact_details(session, candidate, email="new@example.com")
        assert merged.emails_added == ("new@example.com",)
        assert merged.suppressed == ("new@example.com",)
        assert is_suppressed(session, org_id=account["org_id"], email="new@example.com")

    def test_a_new_number_for_a_suppressed_person_is_suppressed(self, session, account):
        candidate = a_candidate(session, account, name="Priya Raman", phones=["+14155559999"])
        suppress(session, org_id=account["org_id"], phone="+14155559999", reason="unsubscribe")

        merged = merge_contact_details(session, candidate, phone="+14155550123")
        assert merged.suppressed == ("+14155550123",)
        assert is_suppressed(session, org_id=account["org_id"], phone="+14155550123")

    def test_an_ordinary_person_is_not_suppressed(self, session, account):
        candidate = a_candidate(session, account, name="Priya Raman", emails=["old@example.com"])
        merged = merge_contact_details(session, candidate, email="new@example.com")
        assert merged.suppressed == ()
        assert not is_suppressed(session, org_id=account["org_id"], email="new@example.com")
