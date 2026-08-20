"""Notes on a person: CRUD, authorship, and who is allowed to rewrite them."""

from __future__ import annotations

from sqlalchemy import select

from juicebox.models import Candidate, CandidateNote


def _person(client, headers, name="Ada Lovelace") -> str:
    response = client.post(
        "/v1/candidates",
        json={"full_name": name, "source": "manual", "source_id": name.lower()},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _write(client, headers, candidate_id, body="Met at a conference."):
    return client.post(
        f"/v1/candidates/{candidate_id}/notes", json={"body": body}, headers=headers
    )


class TestCrud:
    def test_a_note_round_trips_with_its_byline(self, client, account):
        candidate_id = _person(client, account["headers"])

        created = _write(client, account["headers"], candidate_id)
        assert created.status_code == 201, created.text
        note = created.json()
        assert note["body"] == "Met at a conference."
        # The signup fixture's user is called Ada.
        assert note["author_name"] == "Ada"
        assert note["edited_at"] is None

        listed = client.get(
            f"/v1/candidates/{candidate_id}/notes", headers=account["headers"]
        ).json()
        assert [n["id"] for n in listed] == [note["id"]]

    def test_notes_come_back_newest_first(self, client, account):
        candidate_id = _person(client, account["headers"])
        for body in ("first", "second", "third"):
            _write(client, account["headers"], candidate_id, body)

        listed = client.get(
            f"/v1/candidates/{candidate_id}/notes", headers=account["headers"]
        ).json()
        assert [n["body"] for n in listed] == ["third", "second", "first"]

    def test_editing_stamps_edited_at(self, client, account):
        candidate_id = _person(client, account["headers"])
        note = _write(client, account["headers"], candidate_id).json()

        updated = client.patch(
            f"/v1/notes/{note['id']}",
            json={"body": "Actually, met at a dinner."},
            headers=account["headers"],
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["body"] == "Actually, met at a dinner."
        # Distinguishes "never edited" from "edited the instant it was written".
        assert body["edited_at"] is not None

    def test_deleting_removes_it(self, client, account, session):
        candidate_id = _person(client, account["headers"])
        note = _write(client, account["headers"], candidate_id).json()

        assert (
            client.delete(f"/v1/notes/{note['id']}", headers=account["headers"]).status_code
            == 204
        )
        assert session.scalars(select(CandidateNote)).all() == []

    def test_an_empty_note_is_rejected(self, client, account):
        candidate_id = _person(client, account["headers"])
        assert _write(client, account["headers"], candidate_id, "").status_code == 422


class TestTracking:
    def test_writing_a_note_is_an_explicit_crm_action(self, client, account, session):
        candidate_id = _person(client, account["headers"])
        _write(client, account["headers"], candidate_id)

        session.expire_all()
        assert session.get(Candidate, candidate_id).crm_tracked is True


class TestScoping:
    def test_another_org_cannot_read_or_write_your_notes(self, client, account):
        candidate_id = _person(client, account["headers"])
        note = _write(client, account["headers"], candidate_id).json()

        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
                "name": "Bo",
            },
        ).json()
        headers = {"Authorization": f"Bearer {other['access_token']}"}

        assert (
            client.get(f"/v1/candidates/{candidate_id}/notes", headers=headers).status_code
            == 404
        )
        assert (
            client.patch(
                f"/v1/notes/{note['id']}", json={"body": "mine now"}, headers=headers
            ).status_code
            == 404
        )
        assert client.delete(f"/v1/notes/{note['id']}", headers=headers).status_code == 404

    def test_notes_for_an_unknown_person_are_a_404(self, client, account):
        assert (
            client.get("/v1/candidates/cand_nope/notes", headers=account["headers"]).status_code
            == 404
        )


class TestAuthorship:
    def test_a_teammate_cannot_rewrite_your_note(self, client, account, session):
        """A note is a record of who thought what. Editing someone else's is
        not an edit, it is a forgery."""
        from juicebox.models import User
        from juicebox.security import hash_password

        candidate_id = _person(client, account["headers"])
        note = _write(client, account["headers"], candidate_id).json()

        session.add(
            User(
                org_id=account["org_id"],
                email="colleague@acme-talent.com",
                name="Bo",
                password_hash=hash_password("supersecret1"),
                role="recruiter",
            )
        )
        session.commit()

        token = client.post(
            "/v1/auth/login",
            json={"email": "colleague@acme-talent.com", "password": "supersecret1"},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert (
            client.patch(
                f"/v1/notes/{note['id']}", json={"body": "no"}, headers=headers
            ).status_code
            == 403
        )
        # They can still read it: notes are shared context, not private.
        listed = client.get(f"/v1/candidates/{candidate_id}/notes", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

    def test_an_owner_can_clean_up_after_a_departure(self, client, account, session):
        from juicebox.models import User
        from juicebox.security import hash_password

        candidate_id = _person(client, account["headers"])

        session.add(
            User(
                org_id=account["org_id"],
                email="leaver@acme-talent.com",
                name="Bo",
                password_hash=hash_password("supersecret1"),
                role="recruiter",
            )
        )
        session.commit()
        token = client.post(
            "/v1/auth/login",
            json={"email": "leaver@acme-talent.com", "password": "supersecret1"},
        ).json()["access_token"]
        note = _write(
            client, {"Authorization": f"Bearer {token}"}, candidate_id, "theirs"
        ).json()

        # The signup fixture's user is the org owner.
        assert (
            client.delete(f"/v1/notes/{note['id']}", headers=account["headers"]).status_code
            == 204
        )
