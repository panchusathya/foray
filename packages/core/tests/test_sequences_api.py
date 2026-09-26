"""Sequence editing contracts: step replacement, metadata patch, clone, generate.

These are the endpoints the sequence-editor UI builds against. The LLM is
disabled in tests, so /generate exercises the template fallback path.
"""

from __future__ import annotations


def _create_sequence(client, headers, *, project_id=None, steps=None):
    payload = {
        "name": "Editor test",
        "project_id": project_id,
        "personalize": False,
        "steps": steps
        or [
            {
                "delay_days": 0,
                "subject_template": "Hi {{ first_name }}",
                "body_template": "Hi {{ first_name }}, {{ opening_line }}",
            },
            {
                "delay_days": 3,
                "subject_template": "Re: Hi",
                "body_template": "Following up.",
            },
        ],
    }
    response = client.post("/v1/sequences", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _first_candidate(client, headers):
    return client.get("/v1/candidates?q=ada", headers=headers).json()[0]


class TestReplaceSteps:
    def test_empty_step_list_rejected(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.put(
            f"/v1/sequences/{sequence['id']}/steps", json=[], headers=headers
        )
        assert response.status_code == 422


class TestUpdateSequence:
    def test_partial_update_leaves_other_fields(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"name": "Renamed", "personalize": True},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Renamed"
        assert body["personalize"] is True
        assert body["stop_on_reply"] is True  # untouched
        assert len(body["steps"]) == 2  # steps untouched


class TestRotationSubset:
    """Which mailboxes a rotating sequence is allowed to use."""

    def test_a_mailbox_from_another_org_is_refused(self, client, loaded_org, session):
        """The column is JSON and carries no foreign key, so nothing else would
        catch a borrowed id until a send tried to use it."""
        from juicebox.models import MailboxConnection, Org

        other = Org(name="Other Firm", slug="other-firm")
        session.add(other)
        session.flush()
        foreign = MailboxConnection(
            org_id=other.id, provider="microsoft", address="them@elsewhere.io", status="active"
        )
        session.add(foreign)
        session.commit()

        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"mailbox_ids": [foreign.id]},
            headers=headers,
        )
        assert response.status_code == 404
        assert foreign.id in response.json()["detail"]


class TestCloneSequence:
    def test_clone_copies_steps_and_starts_paused(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.post(
            f"/v1/sequences/{sequence['id']}/clone", headers=headers
        )
        assert response.status_code == 201, response.text
        copy = response.json()
        assert copy["id"] != sequence["id"]
        assert copy["name"] == "Editor test (copy)"
        assert copy["status"] == "paused"
        assert [s["subject_template"] for s in copy["steps"]] == [
            s["subject_template"] for s in sequence["steps"]
        ]

    def test_clone_keeps_the_sending_mailbox(self, client, loaded_org, session):
        # Dropping mailbox_id sends the copy from the server default instead of
        # the connected Outlook account the original was set up with.
        from juicebox.models import MailboxConnection

        headers = loaded_org["headers"]
        connection = MailboxConnection(
            org_id=loaded_org["org_id"],
            provider="microsoft",
            address="recruiter@goforay.io",
            status="active",
        )
        session.add(connection)
        session.commit()

        sequence = _create_sequence(client, headers)
        patched = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"mailbox_id": connection.id},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text

        copy = client.post(f"/v1/sequences/{sequence['id']}/clone", headers=headers).json()
        assert copy["mailbox_id"] == connection.id

    def test_clone_into_another_orgs_project_is_rejected(self, client, loaded_org):
        # Same guard create, patch and enroll make: the FK only asks that the
        # project row exist, and a cross-tenant one is read back out through
        # /preview as that org's role_title, project_name and location.
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        rival = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival-clone@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        rival_headers = {"Authorization": f"Bearer {rival['access_token']}"}
        rival_project = client.post(
            "/v1/projects", json={"name": "Rival role"}, headers=rival_headers
        ).json()

        response = client.post(
            f"/v1/sequences/{sequence['id']}/clone",
            json={"project_id": rival_project["id"]},
            headers=headers,
        )
        assert response.status_code == 404

    def test_clone_keeps_the_rotation_subset(self, client, loaded_org, session):
        # Same reasoning as the pinned mailbox: a copy that fell back to the
        # whole pool would send from addresses the original was kept off.
        from juicebox.models import MailboxConnection

        headers = loaded_org["headers"]
        connection = MailboxConnection(
            org_id=loaded_org["org_id"],
            provider="microsoft",
            address="warming@goforay.io",
            status="active",
        )
        session.add(connection)
        session.commit()

        sequence = _create_sequence(client, headers)
        client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"mailbox_ids": [connection.id]},
            headers=headers,
        )
        copy = client.post(f"/v1/sequences/{sequence['id']}/clone", headers=headers).json()
        assert copy["mailbox_ids"] == [connection.id]


class TestEnrollGuards:
    def test_enrolling_into_another_orgs_project_is_rejected(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        candidate = _first_candidate(client, headers)

        rival = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival-enroll@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        rival_headers = {"Authorization": f"Bearer {rival['access_token']}"}
        rival_project = client.post(
            "/v1/projects", json={"name": "Rival role"}, headers=rival_headers
        ).json()

        response = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]], "project_id": rival_project["id"]},
            headers=headers,
        )
        assert response.status_code == 404


class TestGenerateSequence:
    def test_template_fallback_without_llm(self, client, loaded_org):
        headers = loaded_org["headers"]
        response = client.post(
            "/v1/sequences/generate",
            json={"prompt": "ML engineers for a robotics startup", "num_steps": 2},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["generated_by"] == "template"
        draft = body["draft"]
        assert len(draft["steps"]) == 2
        assert draft["steps"][0]["delay_days"] == 0
        # The trimmed draft keeps the close-out as the final step. Identified
        # by its body: only step 1 carries a subject now, because follow-ups
        # reply on its thread and inherit its line.
        assert "Last note from me" in draft["steps"][-1]["body_template"]
        assert draft["steps"][0]["subject_template"]
        assert draft["steps"][-1]["subject_template"] == ""
