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


class TestStepScheduling:
    def test_scheduling_fields_roundtrip(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(
            client,
            headers,
            steps=[
                {
                    "delay_days": 0,
                    "delay_unit": "business_days",
                    "send_at_time": "09:00",
                    "send_timezone": "America/Los_Angeles",
                    "subject_template": "Hello",
                    "body_template": "Hi {{ first_name }}",
                }
            ],
        )
        step = sequence["steps"][0]
        assert step["delay_unit"] == "business_days"
        assert step["send_at_time"] == "09:00"
        assert step["send_timezone"] == "America/Los_Angeles"

    def test_scheduling_fields_default(self, client, loaded_org):
        sequence = _create_sequence(client, loaded_org["headers"])
        step = sequence["steps"][0]
        assert step["delay_unit"] == "days"
        assert step["send_at_time"] == ""

    def test_invalid_send_at_time_rejected(self, client, loaded_org):
        response = client.post(
            "/v1/sequences",
            json={
                "name": "Bad time",
                "steps": [
                    {"body_template": "Hi", "send_at_time": "25:99"},
                ],
            },
            headers=loaded_org["headers"],
        )
        assert response.status_code == 422


class TestReplaceSteps:
    def test_replaces_and_reorders(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.put(
            f"/v1/sequences/{sequence['id']}/steps",
            json=[
                {"delay_days": 0, "subject_template": "One", "body_template": "First."},
                {"delay_days": 2, "subject_template": "Two", "body_template": "Second."},
                {"delay_days": 4, "subject_template": "Three", "body_template": "Third."},
            ],
            headers=headers,
        )
        assert response.status_code == 200, response.text
        steps = response.json()["steps"]
        assert [s["subject_template"] for s in steps] == ["One", "Two", "Three"]
        assert [s["step_order"] for s in steps] == [0, 1, 2]

        fetched = client.get(f"/v1/sequences/{sequence['id']}", headers=headers).json()
        assert len(fetched["steps"]) == 3

    def test_empty_step_list_rejected(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.put(
            f"/v1/sequences/{sequence['id']}/steps", json=[], headers=headers
        )
        assert response.status_code == 422

    def test_blocked_while_enrollments_are_active(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        candidate = _first_candidate(client, headers)
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )

        new_steps = [{"delay_days": 0, "subject_template": "New", "body_template": "New body."}]
        blocked = client.put(
            f"/v1/sequences/{sequence['id']}/steps", json=new_steps, headers=headers
        )
        assert blocked.status_code == 409

        client.patch(f"/v1/sequences/{sequence['id']}/status?value=paused", headers=headers)
        allowed = client.put(
            f"/v1/sequences/{sequence['id']}/steps", json=new_steps, headers=headers
        )
        assert allowed.status_code == 200
        assert len(allowed.json()["steps"]) == 1


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

    def test_unknown_project_rejected(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        response = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"project_id": "proj_doesnotexist"},
            headers=headers,
        )
        assert response.status_code == 404


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

    def test_enrolling_with_an_unknown_project_is_a_404_not_a_500(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        candidate = _first_candidate(client, headers)
        response = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]], "project_id": "proj_does_not_exist"},
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
        # The trimmed draft keeps the close-out as the final step.
        assert "Closing the loop" in draft["steps"][-1]["subject_template"]

    def test_draft_is_directly_creatable(self, client, loaded_org):
        headers = loaded_org["headers"]
        draft = client.post(
            "/v1/sequences/generate",
            json={"prompt": "Backend engineers"},
            headers=headers,
        ).json()["draft"]
        created = client.post("/v1/sequences", json=draft, headers=headers)
        assert created.status_code == 201, created.text
        assert len(created.json()["steps"]) == 3

    def test_project_context_names_the_sequence(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = client.post(
            "/v1/projects",
            json={"name": "Robotics", "role_title": "ML Engineer"},
            headers=headers,
        ).json()
        response = client.post(
            "/v1/sequences/generate",
            json={"prompt": "outreach", "project_id": project["id"]},
            headers=headers,
        )
        body = response.json()
        assert body["draft"]["project_id"] == project["id"]
        assert "ML Engineer" in body["draft"]["name"]

    def test_unknown_project_rejected(self, client, loaded_org):
        response = client.post(
            "/v1/sequences/generate",
            json={"prompt": "outreach", "project_id": "proj_missing"},
            headers=loaded_org["headers"],
        )
        assert response.status_code == 404


class TestDeleteSequence:
    def test_delete_removes_sequence_and_enrollments(self, client, loaded_org):
        headers = loaded_org["headers"]
        sequence = _create_sequence(client, headers)
        candidate = _first_candidate(client, headers)
        enrolled = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )
        assert enrolled.status_code == 200
        assert client.get(
            f"/v1/sequences/{sequence['id']}/enrollments", headers=headers
        ).json()

        deleted = client.delete(f"/v1/sequences/{sequence['id']}", headers=headers)
        assert deleted.status_code == 204
        assert client.get(f"/v1/sequences/{sequence['id']}", headers=headers).status_code == 404

    def test_delete_unknown_sequence_is_404(self, client, account):
        assert client.delete("/v1/sequences/seq_nope", headers=account["headers"]).status_code == 404
