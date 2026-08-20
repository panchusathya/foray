"""The CRM record's schema: inert defaults, real cascades.

The property that matters most is that no existing person silently becomes
CRM-tracked, because that flag widens the thread-sync privacy guard: it is the
difference between storing someone's mailbox history and not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from juicebox.models import CRM_STATUSES, Candidate, CandidateNote, CandidateTag


def _candidate(session, org_id, name="Ada Lovelace"):
    candidate = Candidate(
        org_id=org_id, source="test", source_id=name.lower(), full_name=name
    )
    session.add(candidate)
    session.flush()
    return candidate


class TestDefaults:
    def test_a_new_person_is_not_crm_tracked(self, session, account):
        """The flag widens the mail privacy guard. Defaulting it on would
        retroactively opt every sourced profile into having their mail stored."""
        candidate = _candidate(session, account["org_id"])

        assert candidate.crm_tracked is False
        assert candidate.crm_status == "none"
        assert candidate.manual_fields == []

    def test_bulk_imported_people_are_not_tracked_either(self, client, loaded_org, session):
        tracked = session.scalars(
            select(Candidate).where(Candidate.crm_tracked.is_(True))
        ).all()
        assert tracked == []

    def test_do_not_contact_is_part_of_the_vocabulary(self):
        # The router validates against this tuple; the sequencer's suppression
        # wiring keys off this exact value.
        assert "do_not_contact" in CRM_STATUSES
        assert CRM_STATUSES[0] == "none"


class TestNotes:
    def test_a_note_records_who_and_when(self, session, account):
        candidate = _candidate(session, account["org_id"])
        note = CandidateNote(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            author_id=account["user_id"],
            author_name="Ada",
            body="Met at a conference.",
        )
        session.add(note)
        session.flush()

        assert note.id.startswith("note_")
        assert note.created_at is not None
        # Only set on edit, so "never edited" is distinguishable from "edited
        # the moment it was written".
        assert note.edited_at is None

    def test_deleting_the_person_takes_their_notes(self, session, account):
        candidate = _candidate(session, account["org_id"])
        session.add(
            CandidateNote(
                org_id=account["org_id"], candidate_id=candidate.id, body="ephemeral"
            )
        )
        session.flush()

        session.delete(candidate)
        session.flush()

        assert session.scalars(select(CandidateNote)).all() == []

    def test_the_byline_survives_a_missing_author(self, session, account):
        """author_id is unconstrained and nullable: an API key has no user."""
        candidate = _candidate(session, account["org_id"])
        note = CandidateNote(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            author_id=None,
            author_name="Automation",
            body="Imported.",
        )
        session.add(note)
        session.flush()

        assert note.author_id is None
        assert note.author_name == "Automation"


class TestTags:
    def test_the_same_tag_cannot_be_applied_twice(self, session, account):
        candidate = _candidate(session, account["org_id"])
        for _ in range(2):
            session.add(
                CandidateTag(
                    org_id=account["org_id"], candidate_id=candidate.id, tag="warm"
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_two_people_can_share_a_tag(self, session, account):
        first = _candidate(session, account["org_id"], "Ada Lovelace")
        second = _candidate(session, account["org_id"], "Grace Hopper")
        session.add(
            CandidateTag(org_id=account["org_id"], candidate_id=first.id, tag="warm")
        )
        session.add(
            CandidateTag(org_id=account["org_id"], candidate_id=second.id, tag="warm")
        )
        session.flush()

        assert len(session.scalars(select(CandidateTag)).all()) == 2

    def test_deleting_the_person_takes_their_tags(self, session, account):
        candidate = _candidate(session, account["org_id"])
        session.add(
            CandidateTag(org_id=account["org_id"], candidate_id=candidate.id, tag="warm")
        )
        session.flush()

        session.delete(candidate)
        session.flush()

        assert session.scalars(select(CandidateTag)).all() == []


class TestTheGuard:
    def test_tracking_a_person_brings_their_mail_into_scope(self, session, account):
        """The one clause this whole flag exists for. A hand-added or promoted
        contact has no project, no sequence and no prior message, so without
        it their mail would keep being discarded right after the user asked to
        track them."""
        from juicebox.integrations.matching import build_address_index

        candidate = _candidate(session, account["org_id"])
        candidate.emails = ["ada@example.com"]
        session.flush()

        index = build_address_index(session, org_id=account["org_id"])
        assert "ada@example.com" not in index

        candidate.crm_tracked = True
        session.flush()

        index = build_address_index(session, org_id=account["org_id"])
        assert index["ada@example.com"] == candidate.id

    def test_a_stranger_is_still_a_stranger(self, session, account):
        """Widening the guard must not open it. Someone with no candidate row
        at all is unreachable regardless."""
        from juicebox.integrations.matching import build_address_index

        _candidate(session, account["org_id"])
        session.flush()

        index = build_address_index(session, org_id=account["org_id"])
        assert "vendor@unrelated.example.com" not in index
