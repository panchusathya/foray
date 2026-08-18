"""HTTP-level tests: auth boundaries, search, pipelines, outreach and agents."""

from __future__ import annotations


class TestAuth:
    def test_signup_returns_a_usable_token(self, client, account):
        response = client.get("/v1/auth/me", headers=account["headers"])
        assert response.status_code == 200
        assert response.json()["email"] == "recruiter@acme-talent.com"

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

    def test_login_with_a_bad_password_fails(self, client, account):
        response = client.post(
            "/v1/auth/login",
            json={"email": "recruiter@acme-talent.com", "password": "wrong-password"},
        )
        assert response.status_code == 401

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

    def test_enrichment_is_visible_on_read(self, client, loaded_org):
        listing = client.get(
            "/v1/candidates?q=ada", headers=loaded_org["headers"]
        ).json()
        assert len(listing) == 1

        detail = client.get(
            f"/v1/candidates/{listing[0]['id']}", headers=loaded_org["headers"]
        ).json()
        assert detail["seniority"] == "senior"
        assert detail["current_company"] == "Modal"
        assert "pytorch" in detail["skills"]
        assert len(detail["experiences"]) == 2

    def test_email_guessing_endpoint(self, client, loaded_org):
        candidate_id = client.get(
            "/v1/candidates?q=ada", headers=loaded_org["headers"]
        ).json()[0]["id"]
        response = client.get(
            f"/v1/candidates/{candidate_id}/email-guesses?domain=modal.com",
            headers=loaded_org["headers"],
        ).json()
        assert response["guesses"][0]["email"] == "ada.okonkwo@modal.com"

    def test_missing_candidate_returns_404(self, client, account):
        assert client.get("/v1/candidates/cand_nope", headers=account["headers"]).status_code == 404


class TestSearchApi:
    def test_search_returns_ranked_explained_results(self, client, loaded_org):
        response = client.post(
            "/v1/search",
            json={
                "query": "senior machine learning engineer in San Francisco with pytorch",
                "limit": 5,
            },
            headers=loaded_org["headers"],
        )
        assert response.status_code == 200
        body = response.json()
        assert body["results"][0]["candidate"]["full_name"] == "Ada Okonkwo"
        assert body["parsed"]["job_titles"] == ["machine learning engineer"]
        assert body["results"][0]["criteria"], "every hit must carry its evidence"
        assert body["search_id"]

    def test_overrides_narrow_the_result_set(self, client, loaded_org):
        response = client.post(
            "/v1/search",
            json={"query": "engineer", "overrides": {"locations": ["berlin"]}},
            headers=loaded_org["headers"],
        ).json()
        top = response["results"][0]
        assert top["candidate"]["full_name"] == "Bo Lindqvist"

    def test_a_persisted_search_can_be_replayed(self, client, loaded_org):
        search_id = client.post(
            "/v1/search",
            json={"query": "backend engineer with go"},
            headers=loaded_org["headers"],
        ).json()["search_id"]

        replay = client.get(f"/v1/searches/{search_id}", headers=loaded_org["headers"])
        assert replay.status_code == 200
        assert replay.json()["results"][0]["candidate"]["full_name"] == "Cleo Rahman"

    def test_index_stats(self, client, loaded_org):
        stats = client.get("/v1/search/index-stats", headers=loaded_org["headers"]).json()
        assert stats["documents"] == 3
        assert stats["embedding_dim"] > 0


class TestPipeline:
    def _project(self, client, headers):
        return client.post(
            "/v1/projects",
            json={"name": "Founding ML", "role_title": "Senior ML Engineer"},
            headers=headers,
        ).json()

    def test_add_candidates_carries_the_search_rationale(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = self._project(client, headers)
        search = client.post(
            "/v1/search",
            json={"query": "machine learning engineer with pytorch", "limit": 1},
            headers=headers,
        ).json()

        response = client.post(
            f"/v1/projects/{project['id']}/candidates",
            json={
                "candidate_ids": [search["results"][0]["candidate"]["id"]],
                "search_id": search["search_id"],
            },
            headers=headers,
        )
        assert response.status_code == 200
        entry = response.json()[0]
        assert entry["stage"] == "sourced"
        assert entry["rationale"]["criteria"]
        assert entry["score"] > 0

    def test_stage_transitions(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = self._project(client, headers)
        candidate_id = client.get("/v1/candidates", headers=headers).json()[0]["id"]
        entry = client.post(
            f"/v1/projects/{project['id']}/candidates",
            json={"candidate_ids": [candidate_id]},
            headers=headers,
        ).json()[0]

        updated = client.patch(
            f"/v1/projects/{project['id']}/pipeline/{entry['id']}",
            json={"stage": "interviewing", "notes": "great screen"},
            headers=headers,
        ).json()
        assert updated["stage"] == "interviewing"
        assert updated["notes"] == "great screen"

        bad = client.patch(
            f"/v1/projects/{project['id']}/pipeline/{entry['id']}",
            json={"stage": "not-a-stage"},
            headers=headers,
        )
        assert bad.status_code == 422

    def test_adding_the_same_candidate_twice_is_a_no_op(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = self._project(client, headers)
        candidate_id = client.get("/v1/candidates", headers=headers).json()[0]["id"]
        payload = {"candidate_ids": [candidate_id]}
        client.post(f"/v1/projects/{project['id']}/candidates", json=payload, headers=headers)
        client.post(f"/v1/projects/{project['id']}/candidates", json=payload, headers=headers)
        pipeline = client.get(f"/v1/projects/{project['id']}/pipeline", headers=headers).json()
        assert len(pipeline) == 1


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
                        "subject_template": "Re: Hi",
                        "body_template": "Following up.",
                    },
                ],
            },
            headers=headers,
        ).json()

    def test_create_preview_enroll_and_run(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        sequence = self._sequence(client, headers)
        assert len(sequence["steps"]) == 2

        candidate = client.get("/v1/candidates?q=ada", headers=headers).json()[0]
        preview = client.post(
            f"/v1/sequences/{sequence['id']}/preview",
            json={"candidate_id": candidate["id"]},
            headers=headers,
        ).json()
        assert preview["subject"] == "Hi Ada"
        assert "Modal" in preview["body"]

        enrolled = client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        ).json()
        assert enrolled[0]["status"] == "active"

        run = client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers).json()
        assert run["sent"] == 1
        assert len(mailbox.outbox) == 1

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

    def test_unmatched_reply_returns_404(self, client, loaded_org):
        response = client.post(
            "/v1/webhooks/inbound-email",
            json={"from": "stranger@example.com", "subject": "hi", "body": "hello"},
            headers=loaded_org["headers"],
        )
        assert response.status_code == 404

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
    def test_postal_address_round_trips(self, client, account):
        headers = account["headers"]
        body = client.patch(
            "/v1/org",
            json={"postal_address": "1 Market St, San Francisco, CA 94105"},
            headers=headers,
        ).json()
        assert "1 Market St" in body["postal_address"]
        assert client.get("/v1/org", headers=headers).json()["postal_address"] == body["postal_address"]

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
    def test_agent_run_fills_the_pipeline(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = client.post(
            "/v1/projects", json={"name": "ML hires"}, headers=headers
        ).json()

        agent = client.post(
            "/v1/agents",
            json={
                "name": "SF ML engineers",
                "project_id": project["id"],
                "query": "machine learning engineer with pytorch in San Francisco",
                "min_score": 0.3,
                "max_per_run": 2,
            },
            headers=headers,
        )
        assert agent.status_code == 201

        run = client.post(f"/v1/agents/{agent.json()['id']}/run", headers=headers).json()
        assert run["added"] >= 1
        pipeline = client.get(f"/v1/projects/{project['id']}/pipeline", headers=headers).json()
        assert len(pipeline) == run["added"]
        assert pipeline[0]["rationale"]["criteria"]

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

    def test_agent_requires_a_real_project(self, client, account):
        response = client.post(
            "/v1/agents",
            json={"name": "x", "project_id": "proj_nope", "query": "engineers"},
            headers=account["headers"],
        )
        assert response.status_code == 404


class TestAnalytics:
    def test_funnel_reports_stages_and_outreach(self, client, loaded_org, mailbox):
        headers = loaded_org["headers"]
        project = client.post("/v1/projects", json={"name": "ML"}, headers=headers).json()
        candidate = client.get("/v1/candidates?q=ada", headers=headers).json()[0]
        client.post(
            f"/v1/projects/{project['id']}/candidates",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )

        sequence = client.post(
            "/v1/sequences",
            json={
                "name": "one touch",
                "project_id": project["id"],
                "personalize": False,
                "steps": [{"delay_days": 0, "subject_template": "Hi", "body_template": "Hello"}],
            },
            headers=headers,
        ).json()
        client.post(
            f"/v1/sequences/{sequence['id']}/enroll",
            json={"candidate_ids": [candidate["id"]], "project_id": project["id"]},
            headers=headers,
        )
        client.post(f"/v1/sequences/{sequence['id']}/run", headers=headers)

        funnel = client.get("/v1/analytics/funnel", headers=headers).json()
        assert funnel["outreach"]["sent"] == 1
        assert funnel["stages"]["contacted"] == 1
        assert funnel["reply_rate"] == 0.0

    def test_activity_feed_records_events(self, client, loaded_org):
        headers = loaded_org["headers"]
        project = client.post("/v1/projects", json={"name": "ML"}, headers=headers).json()
        candidate = client.get("/v1/candidates", headers=headers).json()[0]
        client.post(
            f"/v1/projects/{project['id']}/candidates",
            json={"candidate_ids": [candidate["id"]]},
            headers=headers,
        )
        feed = client.get("/v1/analytics/activity", headers=headers).json()
        assert any(event["type"] == "pipeline.added" for event in feed)


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
    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["llm"] is False  # disabled in tests

    def test_openapi_schema_builds(self, client):
        assert client.get("/openapi.json").status_code == 200
