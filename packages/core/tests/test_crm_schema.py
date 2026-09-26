"""The CRM record's schema: inert defaults, real cascades.

The property that matters most is that no existing person silently becomes
CRM-tracked, because that flag widens the thread-sync privacy guard: it is the
difference between storing someone's mailbox history and not.
"""

from __future__ import annotations

from sqlalchemy import select

from juicebox.models import Candidate, CandidateNote, CandidateTag


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

class TestNotes:
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

class TestTags:
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

