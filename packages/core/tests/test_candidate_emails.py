"""The address projection, and refusing to create a duplicate person.

The projection exists so "who owns this address?" is one indexed lookup rather
than a scan of every JSON list in the org. It is deliberately a *subset* of
``candidates.emails``: one address belongs to one person, but nothing has ever
stopped the same address appearing in two JSON lists, so the ones that lose a
collision keep their JSON copy and get no row here.
"""

from __future__ import annotations

from sqlalchemy import select

from juicebox.models import Candidate, CandidateEmail
from juicebox.repository import owner_of_email, upsert_candidates


def _create(client, headers, name, **extra):
    payload = {"full_name": name, "source": "manual", **extra}
    return client.post("/v1/candidates", json=payload, headers=headers)


class TestProjectionTracksTheColumn:
    def test_editing_the_addresses_moves_the_rows(self, client, session, account):
        candidate_id = _create(
            client, account["headers"], "Ada", source_id="ada", emails=["old@example.com"]
        ).json()["id"]

        client.patch(
            f"/v1/candidates/{candidate_id}",
            json={"emails": ["new@example.com"]},
            headers=account["headers"],
        )
        session.expire_all()

        rows = session.scalars(select(CandidateEmail)).all()
        assert [r.email for r in rows] == ["new@example.com"]

    def test_deleting_the_person_takes_the_rows(self, client, session, account):
        candidate_id = _create(
            client, account["headers"], "Ada", source_id="ada", emails=["ada@example.com"]
        ).json()["id"]

        client.delete(f"/v1/candidates/{candidate_id}", headers=account["headers"])
        session.expire_all()

        assert session.scalars(select(CandidateEmail)).all() == []

    def test_every_projected_row_matches_some_candidates_json(
        self, client, session, account, loaded_org
    ):
        """One-directional on purpose. The converse -- every JSON address has a
        row -- is false whenever two people share an address, which the JSON
        column has always permitted."""
        session.expire_all()
        by_id = {c.id: set(c.emails or []) for c in session.scalars(select(Candidate)).all()}

        for row in session.scalars(select(CandidateEmail)).all():
            assert row.email in by_id[row.candidate_id]


class TestCollisions:
    def test_a_second_claim_on_an_address_is_skipped_not_stolen(
        self, session, account
    ):
        """Matches build_address_index: first writer wins. The loser keeps the
        address in their own JSON list."""
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "t",
                    "source_id": "first",
                    "full_name": "First Owner",
                    "emails": ["shared@example.com"],
                }
            ],
        )
        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "t",
                    "source_id": "second",
                    "full_name": "Second Owner",
                    "emails": ["shared@example.com", "own@example.com"],
                }
            ],
        )
        session.flush()

        rows = session.scalars(
            select(CandidateEmail).where(CandidateEmail.email == "shared@example.com")
        ).all()
        assert len(rows) == 1

        second = session.scalar(select(Candidate).where(Candidate.source_id == "second"))
        # Their JSON is untouched; only the projection defers.
        assert "shared@example.com" in second.emails
        assert rows[0].candidate_id != second.id

    def test_owner_of_email_is_org_scoped(self, client, session, account):
        _create(
            client, account["headers"], "Ada", source_id="ada", emails=["ada@example.com"]
        )
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
                "name": "Bo",
            },
        ).json()

        assert owner_of_email(
            session, org_id=account["org_id"], email="ada@example.com"
        ) is not None
        assert (
            owner_of_email(
                session, org_id=other["user"]["org_id"], email="ada@example.com"
            )
            is None
        )


class TestTracking:
    def test_adding_someone_by_hand_tracks_them(self, client, session, account):
        candidate_id = _create(
            client, account["headers"], "Ada", source_id="ada", emails=["ada@example.com"]
        ).json()["id"]

        session.expire_all()
        assert session.get(Candidate, candidate_id).crm_tracked is True

    def test_a_bulk_import_does_not(self, client, session, account, loaded_org):
        """Sourcing thousands of profiles is not consent to retain their mail."""
        session.expire_all()
        assert session.scalars(
            select(Candidate).where(Candidate.crm_tracked.is_(True))
        ).all() == []
