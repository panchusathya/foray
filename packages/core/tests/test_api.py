"""HTTP-level tests: auth boundaries, search, pipelines, outreach and agents."""

from __future__ import annotations


class TestAuth:
    def test_duplicate_signup_is_rejected(self, client, account):
        response = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Other",
                "email": "recruiter@acme-talent.com",
                "password": "supersecret1",
            },
        )
        assert response.status_code == 409

    def test_unauthenticated_requests_are_rejected(self, client):
        assert client.get("/v1/candidates").status_code == 401
        assert client.post("/v1/search", json={"query": "x"}).status_code == 401

    def test_api_key_round_trip(self, client, account):
        created = client.post("/v1/auth/api-keys?name=ingest", headers=account["headers"])
        assert created.status_code == 201
        key = created.json()["key"]
        assert key.startswith("jbx_")

        listed = client.get("/v1/auth/api-keys", headers=account["headers"])
        assert listed.json()[0]["key"] is None  # never returned twice

        response = client.get("/v1/candidates", headers={"X-API-Key": key})
        assert response.status_code == 200

    def test_revoked_api_key_stops_working(self, client, account):
        created = client.post("/v1/auth/api-keys", headers=account["headers"]).json()
        client.delete(f"/v1/auth/api-keys/{created['id']}", headers=account["headers"])
        assert client.get("/v1/candidates", headers={"X-API-Key": created["key"]}).status_code == 401


class TestTenancy:
    def test_one_org_cannot_read_another_orgs_people(self, client, loaded_org):
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        headers = {"Authorization": f"Bearer {other['access_token']}"}

        assert client.get("/v1/candidates", headers=headers).json() == []
        results = client.post(
            "/v1/search", json={"query": "machine learning engineer"}, headers=headers
        ).json()
        assert results["results"] == []


class TestCandidates:
    def test_bulk_upsert_is_idempotent(self, client, account, sample_candidates):
        first = client.post(
            "/v1/candidates/bulk",
            json={"candidates": sample_candidates},
            headers=account["headers"],
        ).json()
        assert first["created"] == 3 and first["updated"] == 0

        second = client.post(
            "/v1/candidates/bulk",
            json={"candidates": sample_candidates},
            headers=account["headers"],
        ).json()
        assert second["created"] == 0 and second["updated"] == 3

    def test_bulk_passthrough_fields_survive_without_experiences(self, client, account):
        # Importers with no experiences[] (e.g. Foray) supply employer and
        # experience directly; these fields used to be silently dropped by the
        # schema before reaching normalize_candidate().
        payload = {
            "source": "foray",
            "source_id": "janedoe",
            "full_name": "Jane Doe",
            "github_url": "https://github.com/janedoe",
            "current_company": "Stripe",
            "current_company_domain": "stripe.com",
            "years_experience": 7.2,
            "emails": ["jane@stripe.com"],
            "skills": ["go", "infra"],
            "raw": {"foray": {"score": 71}},
        }
        result = client.post(
            "/v1/candidates/bulk",
            json={"candidates": [payload]},
            headers=account["headers"],
        ).json()
        assert result["created"] == 1

        detail = client.get(
            f"/v1/candidates/{result['ids'][0]}", headers=account["headers"]
        ).json()
        assert detail["current_company"] == "Stripe"
        assert detail["current_company_domain"] == "stripe.com"
        assert detail["years_experience"] == 7.2
        assert detail["emails"] == ["jane@stripe.com"]
        assert detail["source"] == "foray" and detail["source_id"] == "janedoe"

    def test_a_repeated_source_id_within_one_payload_collapses(self, client, account):
        # Two Foray pages can carry the same login, and a CSV can repeat a row.
        # Before the fix the second copy INSERTed again and the unique index
        # failed the whole batch with a 500.
        person = {
            "source": "foray",
            "source_id": "octocat",
            "full_name": "Octo Cat",
            "emails": ["octo@example.com"],
        }
        response = client.post(
            "/v1/candidates/bulk",
            json={"candidates": [person, {**person, "full_name": "Octo Cat (dupe)"}]},
            headers=account["headers"],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["created"] == 1 and body["updated"] == 1
        assert len(set(body["ids"])) == 1

        listing = client.get("/v1/candidates?q=octo", headers=account["headers"]).json()
        assert len(listing) == 1


class TestPipeline:
    def _project(self, client, headers):
        return client.post(
            "/v1/projects",
            json={"name": "Founding ML", "role_title": "Senior ML Engineer"},
            headers=headers,
        ).json()

    def test_delete_project_removes_pipeline_and_unlinks_sequences(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = self._project(client, headers)
        candidate_id = client.get("/v1/candidates", headers=headers).json()[0]["id"]
        client.post(
            f"/v1/projects/{project['id']}/candidates",
            json={"candidate_ids": [candidate_id]},
            headers=headers,
        )
        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "Tied to the project",
                "project_id": project["id"],
                "personalize": False,
                "steps": [
                    {
                        "delay_days": 0,
                        "subject_template": "Hi",
                        "body_template": "Hi there",
                    }
                ],
            },
            headers=headers,
        ).json()
        assert sequence["project_id"] == project["id"]

        deleted = client.delete(f"/v1/projects/{project['id']}", headers=headers)
        assert deleted.status_code == 204
        assert client.get(f"/v1/projects/{project['id']}", headers=headers).status_code == 404
        leftover = client.get(f"/v1/sequences/{sequence['id']}", headers=headers).json()
        assert leftover["project_id"] is None


class TestOutreachApi:
    def _sequence(self, client, headers, project_id=None):
        return client.post(
            "/v1/sequences",
            json={
                "name": "Two touch",
                "project_id": project_id,
                "personalize": False,
                "steps": [
                    {
                        "delay_days": 0,
                        "subject_template": "Hi {{ first_name }}",
                        "body_template": "Hi {{ first_name }}, {{ opening_line }}",
                    },
                    {
                        "delay_days": 3,
                        "subject_template": "",
                        "body_template": "Following up.",
                    },
                ],
            },
            headers=headers,
        ).json()

    def test_inbound_reply_webhook_stops_the_sequence(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        sequence = self._sequence(client, headers)
        candidate = client.get("/v1/candidates?q=ada", headers=headers).json()[0]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )
        client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers)

        reply = client.post(
            "/v1/webhooks/inbound-email",
            json={
                "from": "ada@example.com",
                "subject": "Re: Hi Ada",
                "body": "This sounds interesting, happy to chat.",
            },
            headers=headers,
        )
        assert reply.status_code == 200
        assert reply.json()["intent"] == "interested"

        enrollments = client.get(
            f"/v1/sequences/{sequence['id']}/enrollments", headers=headers
        ).json()
        assert enrollments[0]["status"] == "replied"

    def test_inbound_webhook_replay_is_idempotent(self, client, loaded_org, mailbox, session):
        """ESPs redeliver on any non-2xx and on their own timeouts. A replayed
        delivery must not store a second inbound row or re-run the intent
        machinery — with or without an RFC Message-ID in the payload."""
        from sqlalchemy import func, select

        from juicebox.models import Event, Message

        headers = loaded_org["headers"]
        sequence = self._sequence(client, headers)
        candidate = client.get("/v1/candidates?q=ada", headers=headers).json()[0]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )
        client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers)

        payload = {
            "from": "ada@example.com",
            "subject": "Re: Hi Ada",
            "body": "This sounds interesting, happy to chat.",
            "message_id": "<reply-1@mail.example.com>",
        }
        first = client.post("/v1/webhooks/inbound-email", json=payload, headers=headers)
        assert first.status_code == 200
        assert first.json()["intent"] == "interested"

        replay = client.post("/v1/webhooks/inbound-email", json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert replay.json()["intent"] == "interested"

        inbound_rows = session.scalar(
            select(func.count()).select_from(Message).where(Message.direction == "inbound")
        )
        assert inbound_rows == 1
        replied_events = session.scalar(
            select(func.count()).select_from(Event).where(Event.type == "outreach.replied")
        )
        assert replied_events == 1

        # No Message-ID either: the delivery hash still collapses the retry.
        del payload["message_id"]
        payload["body"] = "Following up — still interested."
        assert client.post("/v1/webhooks/inbound-email", json=payload, headers=headers).json().get(
            "duplicate"
        ) is None
        second = client.post("/v1/webhooks/inbound-email", json=payload, headers=headers)
        assert second.json()["duplicate"] is True

    def test_paused_sequence_does_not_send(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        sequence = self._sequence(client, headers)
        candidate = client.get("/v1/candidates?q=ada", headers=headers).json()[0]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )
        client.patch(f"/v1/sequences/{sequence['id']}/status?value=paused", headers=headers)

        run = client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers).json()
        assert run["sent"] == 0 and not mailbox.outbox


class TestOrgAndSuppressions:
    def test_suppression_list_blocks_sends(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        added = client.post(
            "/v1/suppressions",
            json={"email": "ada@example.com", "reason": "manual"},
            headers=headers,
        )
        assert added.status_code == 201
        listed = client.get("/v1/suppressions", headers=headers).json()
        assert listed[0]["email"] == "ada@example.com"

        sequence = TestOutreachApi()._sequence(client, headers)
        candidate = client.get("/v1/candidates?q=ada", headers=headers).json()[0]
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )
        run = client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers).json()
        assert run["sent"] == 0
        assert not mailbox.outbox

        removed = client.delete(
            "/v1/suppressions", params={"email": "ada@example.com"}, headers=headers
        )
        assert removed.status_code == 200


class TestAgentsApi:
    def test_second_run_does_not_re_add(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = client.post("/v1/projects", json={"name": "ML hires"}, headers=headers).json()
        agent = client.post(
            "/v1/agents",
            json={
                "name": "SF ML engineers",
                "project_id": project["id"],
                "query": "machine learning engineer",
                "min_score": 0.1,
                "max_per_run": 5,
            },
            headers=headers,
        ).json()

        first = client.post(f"/v1/agents/{agent['id']}/run", headers=headers).json()
        second = client.post(f"/v1/agents/{agent['id']}/run", headers=headers).json()
        assert first["added"] > 0
        assert second["added"] == 0


class TestAnalytics:
    def test_funnel_scopes_outreach_to_the_project(self, client, loaded_org, mailbox):
        """project_id narrows the outreach block, not just the stages block."""
        headers = loaded_org["headers"]
        candidates = client.get("/v1/candidates", headers=headers).json()

        def send_one(name, candidate):
            project = client.post(
                "/v1/projects", json={"name": name}, headers=headers
            ).json()
            client.post(
                f"/v1/projects/{project['id']}/candidates",
                json={"candidate_ids": [candidate["id"]]},
                headers=headers,
            )
            sequence = client.post(
                "/v1/sequences",
                json={
                    "name": f"{name} touch",
                    "project_id": project["id"],
                    "personalize": False,
                    "steps": [
                        {
                            "delay_days": 0,
                            "subject_template": "Hi",
                            "body_template": "Hello",
                        }
                    ],
                },
                headers=headers,
            ).json()
            client.post(
                f"/v1/sequences/{sequence['id']}/enroll",
                json={"candidate_ids": [candidate["id"]], "project_id": project["id"]},
                headers=headers,
            )
            client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers)
            return project

        ml = send_one("ML", candidates[0])
        infra = send_one("Infra", candidates[1])

        # The unscoped call must still see both. This half is what catches a
        # filter that leaks into the default, org-wide answer.
        unscoped = client.get("/v1/analytics/funnel", headers=headers).json()
        assert unscoped["outreach"]["sent"] == 2

        scoped = client.get(
            f"/v1/analytics/funnel?project_id={ml['id']}", headers=headers
        ).json()
        assert scoped["project_id"] == ml["id"]
        assert scoped["outreach"]["sent"] == 1
        assert scoped["stages"]["contacted"] == 1

        other = client.get(
            f"/v1/analytics/funnel?project_id={infra['id']}", headers=headers
        ).json()
        assert other["outreach"]["sent"] == 1


class TestSeededDemo:
    """The demo workspace is the first thing anyone touches — keep it working."""

    def test_demo_account_can_log_in_and_search(self, client, session):
        from juicebox.seed import DEMO_EMAIL, DEMO_PASSWORD, seed_demo

        seed_demo(session, candidate_count=20)
        session.commit()

        # Regression guard: reserved TLDs (.local/.test) fail EmailStr validation,
        # which silently made the documented demo credentials unusable.
        login = client.post(
            "/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        assert login.status_code == 200, login.text

        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        results = client.post(
            "/v1/search",
            json={"query": "machine learning engineer with pytorch", "limit": 5},
            headers=headers,
        ).json()
        assert results["results"], "the seeded corpus should be searchable"

        assert client.get("/v1/projects", headers=headers).json()
        assert client.get("/v1/sequences", headers=headers).json()
        assert client.get("/v1/agents", headers=headers).json()


class TestMeta:
    def test_openapi_schema_builds(self, client):
        assert client.get("/openapi.json").status_code == 200
