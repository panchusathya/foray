"""Notes on a person: CRUD, authorship, and who is allowed to rewrite them."""

from __future__ import annotations


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
