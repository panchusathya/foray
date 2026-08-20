"""Job posting CRUD, Project sync, source_id dedupe, heuristic JD parse."""

from __future__ import annotations

from job_fixtures import LONG_JD

from juicebox.jobmatch.parse import parse_posting
from juicebox.jobmatch.spec import heuristic_job_spec, job_spec
from juicebox.models import JobPosting


def _posting(client, account, **fields):
    payload = {
        "title": "Senior Machine Learning Engineer",
        "company": "Acme",
        "location": "San Francisco",
        "jd_text": "Production ML ranking. Python required.",
        **fields,
    }
    response = client.post("/v1/job-postings", json=payload, headers=account["headers"])
    assert response.status_code == 201, response.text
    return response.json()


class TestJobPostingCrud:
    def test_create_returns_pending_parse(self, client, account):
        body = _posting(client, account)
        assert body["status"] == "pending_parse"
        assert body["parsed_spec"] in ({}, None) or body["parser"] == ""
        assert body["source"] == "manual"

    def test_get_and_list_and_patch(self, client, account):
        created = _posting(client, account, title="Staff Backend Engineer")
        got = client.get(f"/v1/job-postings/{created['id']}", headers=account["headers"])
        assert got.status_code == 200
        listed = client.get("/v1/job-postings?q=Backend", headers=account["headers"]).json()
        assert listed["total"] == 1
        patched = client.patch(
            f"/v1/job-postings/{created['id']}",
            json={"status": "closed"},
            headers=account["headers"],
        )
        assert patched.json()["status"] == "closed"

    def test_org_scoping(self, client, account):
        created = _posting(client, account)
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival Jobs",
                "email": "rival-jobs@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        headers = {"Authorization": f"Bearer {other['access_token']}"}
        assert client.get("/v1/job-postings", headers=headers).json()["items"] == []
        assert (
            client.get(f"/v1/job-postings/{created['id']}", headers=headers).status_code
            == 404
        )


class TestSourceIdDedupe:
    def test_same_url_across_sources_is_one_row(self, client, account):
        url = "https://jobs.example.com/ml-engineer/"
        first = _posting(client, account, source_url=url, title="ML Engineer")
        imported = client.post(
            "/v1/job-postings/import",
            json={
                "source": "csv",
                "postings": [
                    {
                        "title": "Machine Learning Engineer",
                        "company": "Acme",
                        "source_url": url,
                        "jd_text": "Updated copy",
                    }
                ],
            },
            headers=account["headers"],
        ).json()
        assert imported["created"] == 0
        assert imported["updated"] == 1
        assert imported["ids"] == [first["id"]]
        listed = client.get("/v1/job-postings", headers=account["headers"]).json()
        assert listed["total"] == 1
        assert listed["items"][0]["jd_text"] == "Updated copy"


class TestProjectSync:
    def test_create_update_delete_round_trip(self, client, account):
        created = client.post(
            "/v1/projects",
            json={
                "name": "Founding ML",
                "role_title": "Senior Machine Learning Engineer",
                "description": "Own retrieval.",
                "location": "San Francisco",
                "company": "Juicebox",
            },
            headers=account["headers"],
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        listed = client.get("/v1/job-postings", headers=account["headers"]).json()
        assert listed["total"] == 1
        posting = listed["items"][0]
        assert posting["source"] == "project"
        assert posting["company"] == "Juicebox"
        assert posting["title"] == "Senior Machine Learning Engineer"
        assert posting["project_id"] == project_id

        client.patch(
            f"/v1/projects/{project_id}",
            json={
                "name": "Founding ML",
                "role_title": "Staff Machine Learning Engineer",
                "description": "Own retrieval and ranking.",
                "location": "Remote",
                "company": "Juicebox",
            },
            headers=account["headers"],
        )
        got = client.get(f"/v1/job-postings/{posting['id']}", headers=account["headers"]).json()
        assert got["title"].startswith("Staff")
        assert got["location"] == "Remote"

        deleted = client.delete(f"/v1/projects/{project_id}", headers=account["headers"])
        assert deleted.status_code == 204
        archived = client.get(f"/v1/job-postings/{posting['id']}", headers=account["headers"]).json()
        assert archived["status"] == "archived"
        assert archived["project_id"] is None


class TestHeuristicSpec:
    def test_long_jd_caps_must_haves(self):
        posting = JobPosting(
            title="Senior Machine Learning Engineer",
            title_normalized="machine learning engineer",
            location="San Francisco",
            jd_text=LONG_JD,
        )
        spec = heuristic_job_spec(posting)
        assert len(spec.must_have_skills) <= 5
        assert "python" not in spec.must_have_skills or len(spec.must_have_skills) <= 5
        # Boilerplate stack from the company blurb must not all become must-haves.
        assert "excel" not in spec.must_have_skills
        assert "salesforce" not in spec.must_have_skills
        assert spec.search_text

    def test_job_spec_offline_uses_heuristic(self):
        posting = JobPosting(
            title="Senior Machine Learning Engineer",
            location="San Francisco",
            jd_text=LONG_JD,
        )
        spec, parser = job_spec(posting)
        assert parser == "heuristic"
        assert len(spec.must_have_skills) <= 5


class TestParseDoesNotReopen:
    def test_closed_posting_stays_closed(self, session, account):
        posting = JobPosting(
            org_id=account["org_id"],
            source="manual",
            source_id="closed-1",
            title="Senior Machine Learning Engineer",
            location="San Francisco",
            jd_text="Python ranking systems.",
            status="pending_parse",
        )
        session.add(posting)
        session.commit()
        parse_posting(session, posting)
        assert posting.status == "open"
        posting.status = "closed"
        posting.jd_text = "Python ranking systems. Kubernetes a plus."
        session.commit()
        parse_posting(session, posting)
        assert posting.status == "closed"
        assert posting.parsed_spec
        assert posting.spec_version


class TestMatchEndpoints:
    def test_ranked_matches_serialize(self, client, account, session, sample_candidates):
        from juicebox.jobmatch.engine import match_posting
        from juicebox.repository import upsert_candidates

        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = JobPosting(
            org_id=account["org_id"],
            source="manual",
            source_id="api-matches",
            title="Senior Machine Learning Engineer",
            company="Acme",
            location="San Francisco",
            jd_text="Python and machine learning in production.",
            status="pending_parse",
        )
        posting.company_normalized = "acme"
        session.add(posting)
        session.flush()
        parse_posting(session, posting)
        match_posting(session, posting)
        session.commit()
        posting_id = posting.id

        listed = client.get(
            f"/v1/job-postings/{posting_id}/matches", headers=account["headers"]
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert isinstance(body, list)
        assert body
        assert body[0]["candidate"]["full_name"]
        assert body[0]["criteria"]

        by_candidate = client.get(
            f"/v1/job-matches?candidate_id={body[0]['candidate_id']}",
            headers=account["headers"],
        )
        assert by_candidate.status_code == 200, by_candidate.text
        assert by_candidate.json()[0]["posting"]["id"] == posting_id

        queue = client.get("/v1/job-matches/rematch-queue", headers=account["headers"])
        assert queue.status_code == 200, queue.text
        assert queue.json() == []
