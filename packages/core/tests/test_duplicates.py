"""Suggesting duplicate people, and joining them without losing anything.

Merging is the only destructive operation in the CRM and it cannot be undone,
so most of this file is about what must survive it. The suggestion half is
about not proposing nonsense: a shared name is not proof, and a list that
keeps re-asking an answered question stops being read.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.duplicates import merge, normalise_name, suggestions
from juicebox.models import (
    Candidate,
    CandidateEmail,
    CandidateNote,
    CandidateTag,
    Enrollment,
    Message,
    Project,
    ProjectCandidate,
    Sequence,
    utcnow,
)
from juicebox.repository import sync_candidate_emails


def _person(session, org_id, name, *, emails=(), company_domain="", **extra):
    person = Candidate(
        org_id=org_id,
        source=extra.pop("source", "manual"),
        source_id=extra.pop("source_id", name.lower() + (emails[0] if emails else "")),
        full_name=name,
        emails=list(emails),
        current_company_domain=company_domain,
        **extra,
    )
    session.add(person)
    session.flush()
    sync_candidate_emails(session, person)
    return person


class TestWhatGetsSuggested:
    def test_the_same_name_at_the_same_company_is_the_strongest_signal(
        self, session, account
    ):
        _person(session, account["org_id"], "Adia Chenumbay",
                emails=["adia@northwind.com"], company_domain="northwind.com")
        _person(session, account["org_id"], "Adia Chenumbay",
                emails=["a.chenumbay@northwind.com"], company_domain="northwind.com")
        session.flush()

        groups = suggestions(session, org_id=account["org_id"])
        assert len(groups) == 1
        assert groups[0].confidence == "same_company"

    def test_a_work_and_a_personal_address_is_the_common_real_case(
        self, session, account
    ):
        _person(session, account["org_id"], "Adia Chenumbay",
                emails=["adia@northwind.com"])
        _person(session, account["org_id"], "Adia Chenumbay",
                emails=["adia.chenumbay@gmail.com"])
        session.flush()

        groups = suggestions(session, org_id=account["org_id"])
        assert groups[0].confidence == "work_and_personal"
        assert "gmail.com" in groups[0].reason

    def test_a_bare_name_match_is_offered_but_ranked_last(self, session, account):
        _person(session, account["org_id"], "John Smith", emails=["js@acme.com"])
        _person(session, account["org_id"], "John Smith", emails=["john@contoso.com"])
        _person(session, account["org_id"], "Adia Chenumbay",
                emails=["adia@northwind.com"], company_domain="northwind.com")
        _person(session, account["org_id"], "Adia Chenumbay",
                emails=["a.c@northwind.com"], company_domain="northwind.com")
        session.flush()

        groups = suggestions(session, org_id=account["org_id"])
        # Strongest evidence first, so the safe merges are the ones in reach.
        assert [g.confidence for g in groups] == ["same_company", "name_only"]

    def test_single_token_names_are_never_paired(self, session, account):
        """Every Info, Support and first-name-only contact would otherwise
        collide with every other one."""
        _person(session, account["org_id"], "Info", emails=["info@a.com"])
        _person(session, account["org_id"], "Info", emails=["info@b.com"])
        session.flush()

        assert suggestions(session, org_id=account["org_id"]) == []

    def test_contacts_named_by_their_address_are_never_paired(
        self, session, account
    ):
        """An unnamed contact falls back to its address. Matching on that
        would pair people for having no name in common."""
        _person(session, account["org_id"], "js@acme.com", emails=["js@acme.com"])
        _person(session, account["org_id"], "jb@acme.com", emails=["jb@acme.com"])
        session.flush()

        assert suggestions(session, org_id=account["org_id"]) == []

    def test_punctuation_and_case_do_not_hide_a_duplicate(self):
        assert normalise_name("O'Brien, Seán") == normalise_name("o brien  seán")

    def test_another_org_is_never_in_the_group(self, session, account, client):
        _person(session, account["org_id"], "Adia Chenumbay", emails=["a@x.com"])
        # Commit first: an open write transaction locks the SQLite file and
        # TestClient opens its own connection.
        session.commit()
        other = client.post(
            "/v1/auth/signup",
            json={"org_name": "Rival", "email": "r@rival-talent.com",
                  "password": "supersecret1", "name": "Bo"},
        ).json()
        _person(session, other["user"]["org_id"], "Adia Chenumbay", emails=["a@y.com"])
        session.flush()

        assert suggestions(session, org_id=account["org_id"]) == []


class TestDismissal:
    def test_a_dismissed_pair_stops_coming_back(self, client, session, account):
        first = _person(session, account["org_id"], "John Smith", emails=["js@acme.com"])
        second = _person(session, account["org_id"], "John Smith", emails=["j@contoso.com"])
        session.commit()

        assert client.get("/v1/crm/duplicates", headers=account["headers"]).json()

        client.post(
            "/v1/crm/duplicates/dismiss",
            json={"candidate_ids": [first.id, second.id]},
            headers=account["headers"],
        )
        session.expire_all()

        assert client.get("/v1/crm/duplicates", headers=account["headers"]).json() == []

    def test_dismissing_one_pair_does_not_hide_a_third_person(
        self, client, session, account
    ):
        first = _person(session, account["org_id"], "John Smith", emails=["a@x.com"])
        second = _person(session, account["org_id"], "John Smith", emails=["b@y.com"])
        _person(session, account["org_id"], "John Smith", emails=["c@z.com"])
        session.commit()

        client.post(
            "/v1/crm/duplicates/dismiss",
            json={"candidate_ids": [first.id, second.id]},
            headers=account["headers"],
        )
        session.expire_all()

        groups = client.get("/v1/crm/duplicates", headers=account["headers"]).json()
        assert groups, "the third John Smith is still unresolved against both"


class TestMergeKeepsEverything:
    @pytest.fixture
    def pair(self, session, account):
        org = account["org_id"]
        primary = _person(session, org, "Adia Chenumbay", emails=["adia@northwind.com"])
        other = _person(session, org, "Adia Chenumbay", emails=["adia@gmail.com"],
                        current_title="Robotics Lead", linkedin_url="https://in/adia")
        session.flush()
        return primary, other

    def test_the_email_history_follows_the_survivor(self, session, account, pair):
        primary, other = pair
        for candidate, days in ((primary, 5), (other, 40), (other, 60)):
            session.add(
                Message(
                    org_id=account["org_id"], candidate_id=candidate.id,
                    direction="outbound", source="microsoft", status="sent",
                    sent_at=utcnow() - timedelta(days=days),
                )
            )
        session.flush()

        result = merge(session, org_id=account["org_id"],
                       primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()

        assert result["moved"]["messages"] == 2
        kept = session.scalars(
            select(Message).where(Message.candidate_id == primary.id)
        ).all()
        assert len(kept) == 3, "merging must not drop the duplicate's mail"

    def test_notes_and_addresses_follow_too(self, session, account, pair):
        primary, other = pair
        session.add(
            CandidateNote(org_id=account["org_id"], candidate_id=other.id, body="Met at a dinner")
        )
        session.flush()

        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()

        notes = session.scalars(select(CandidateNote)).all()
        assert [n.candidate_id for n in notes] == [primary.id]
        session.refresh(primary)
        assert sorted(primary.emails) == ["adia@gmail.com", "adia@northwind.com"]
        # The projection is rebuilt, so both addresses resolve to the survivor.
        owners = {
            row.email: row.candidate_id
            for row in session.scalars(select(CandidateEmail)).all()
        }
        assert set(owners.values()) == {primary.id}

    def test_blank_fields_are_filled_but_present_ones_are_not_overwritten(
        self, session, account, pair
    ):
        primary, other = pair
        primary.current_title = "Staff Engineer"
        session.flush()

        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()
        session.refresh(primary)

        # The survivor is the record someone chose to keep.
        assert primary.current_title == "Staff Engineer"
        # ...but a gap it had is worth filling.
        assert primary.linkedin_url == "https://in/adia"

    def test_a_shared_project_does_not_break_the_merge(self, session, account, pair):
        """project_candidates is unique per (project, candidate); reassigning
        blindly would trip the constraint and take the whole merge down."""
        primary, other = pair
        project = Project(org_id=account["org_id"], name="Robotics")
        session.add(project)
        session.flush()
        for candidate in (primary, other):
            session.add(
                ProjectCandidate(project_id=project.id, candidate_id=candidate.id)
            )
        session.flush()

        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()

        rows = session.scalars(select(ProjectCandidate)).all()
        assert len(rows) == 1
        assert rows[0].candidate_id == primary.id

    def test_a_shared_sequence_does_not_break_the_merge(self, session, account, pair):
        primary, other = pair
        sequence = Sequence(org_id=account["org_id"], name="Outreach", status="active")
        session.add(sequence)
        session.flush()
        for candidate in (primary, other):
            session.add(
                Enrollment(sequence_id=sequence.id, candidate_id=candidate.id,
                           status="active")
            )
        session.flush()

        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()

        rows = session.scalars(select(Enrollment)).all()
        assert len(rows) == 1
        assert rows[0].candidate_id == primary.id

    def test_a_shared_tag_does_not_break_the_merge(self, session, account, pair):
        primary, other = pair
        for candidate in (primary, other):
            session.add(
                CandidateTag(org_id=account["org_id"], candidate_id=candidate.id, tag="warm")
            )
        session.add(
            CandidateTag(org_id=account["org_id"], candidate_id=other.id, tag="referral")
        )
        session.flush()

        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()

        tags = sorted(row.tag for row in session.scalars(select(CandidateTag)).all())
        assert tags == ["referral", "warm"]

    def test_the_duplicate_is_gone_afterwards(self, session, account, pair):
        primary, other = pair
        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()

        assert session.get(Candidate, other.id) is None
        assert session.get(Candidate, primary.id) is not None

    def test_tracking_survives_if_either_side_had_it(self, session, account, pair):
        primary, other = pair
        other.crm_tracked = True
        other.crm_status = "active"
        session.flush()

        merge(session, org_id=account["org_id"],
              primary_id=primary.id, duplicate_ids=[other.id])
        session.flush()
        session.refresh(primary)

        assert primary.crm_tracked is True
        assert primary.crm_status == "active"


class TestMergeRefuses:
    def test_another_org_cannot_merge_your_people(self, client, session, account):
        primary = _person(session, account["org_id"], "Adia Chenumbay", emails=["a@x.com"])
        other = _person(session, account["org_id"], "Adia Chenumbay", emails=["b@x.com"])
        session.commit()

        rival = client.post(
            "/v1/auth/signup",
            json={"org_name": "Rival", "email": "r@rival-talent.com",
                  "password": "supersecret1", "name": "Bo"},
        ).json()
        response = client.post(
            "/v1/crm/duplicates/merge",
            json={"primary_id": primary.id, "duplicate_ids": [other.id]},
            headers={"Authorization": f"Bearer {rival['access_token']}"},
        )
        assert response.status_code == 404
        assert session.get(Candidate, other.id) is not None

    def test_merging_a_candidate_into_itself_is_refused(
        self, client, session, account
    ):
        primary = _person(session, account["org_id"], "Adia Chenumbay", emails=["a@x.com"])
        session.commit()

        response = client.post(
            "/v1/crm/duplicates/merge",
            json={"primary_id": primary.id, "duplicate_ids": [primary.id]},
            headers=account["headers"],
        )
        assert response.status_code == 404
        assert session.get(Candidate, primary.id) is not None
