"""Job posting CRUD, Project sync, source_id dedupe, heuristic JD parse."""

from __future__ import annotations

from types import SimpleNamespace

from job_fixtures import LONG_JD

from juicebox.jobmatch.parse import parse_posting
from juicebox.jobmatch.spec import heuristic_job_spec
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


class TestPostingIdentity:
    """Company and location were structurally always blank.

    The Exa ingest read `company` from `author` (empty on every job board) and
    `location` from a field the search API does not return at all, and the spec
    parser only *reads* those columns -- so nothing downstream could recover
    them. A job card with no employer and no location is not a card.
    """

    def test_the_exa_ingest_no_longer_invents_a_company(self):
        from juicebox.jobmatch.discover import _exa_rows

        rows = _exa_rows(
            [
                {
                    "url": "https://jobs.example.com/1",
                    "title": "Founding Engineer at Ventura | Y Combinator",
                    "author": "Y Combinator",
                    "text": "Ventura is hiring a founding engineer in New York.",
                }
            ]
        )
        assert rows[0]["company"] == "", "Y Combinator is the board, not the employer"
        assert rows[0]["location"] == ""

    def test_identity_fills_blanks_and_leaves_real_values_alone(self, session, account):
        """A company somebody typed in outranks one a model read off a page."""
        from juicebox.jobmatch import parse as parse_mod
        from juicebox.models import JobPosting

        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="idt-1",
            title="Founding Engineer at Ventura | Y Combinator",
            company="Typed By A Human",
            location="",
            jd_text="Ventura is hiring a founding engineer in New York.",
            status="pending_parse",
        )

        class FakeLLM:
            available = True

            def json_fast(self, **kwargs):
                return SimpleNamespace(
                    data={
                        "is_posting": True,
                        "title": "Founding Engineer",
                        "company": "Ventura",
                        "location": "New York",
                    }
                )

        import juicebox.llm.client as client_mod

        original = client_mod.get_llm
        client_mod.get_llm = lambda: FakeLLM()
        try:
            rejected = parse_mod.resolve_identity(posting)
        finally:
            client_mod.get_llm = original

        assert rejected == ""
        assert posting.company == "Typed By A Human", "never overwrite a real value"
        assert posting.location == "New York", "but do fill a blank"
        assert posting.title == "Founding Engineer", "page furniture is replaced"

    def test_an_index_page_is_rejected(self, session, account):
        from juicebox.jobmatch import parse as parse_mod
        from juicebox.models import JobPosting

        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="idt-2",
            title="Software Engineer jobs at Y Combinator startups",
            jd_text="Browse hundreds of roles across YC companies.",
            status="pending_parse",
        )

        class FakeLLM:
            available = True

            def json_fast(self, **kwargs):
                return SimpleNamespace(
                    data={"is_posting": False, "title": "", "company": "", "location": ""}
                )

        import juicebox.llm.client as client_mod

        original = client_mod.get_llm
        client_mod.get_llm = lambda: FakeLLM()
        try:
            rejected = parse_mod.resolve_identity(posting)
        finally:
            client_mod.get_llm = original
        assert rejected, "a search-results page is not a job a person can apply to"


class TestCandidateDiscovery:
    """Nothing ever went looking for jobs on a candidate's behalf.

    Scoring against the book we happen to hold is half the job. The first live
    thread finished onboarding with a complete spec and the database held 59
    postings, none of them matched to anybody.
    """

    def _pref(self, **spec):
        class P:
            pass

        p = P()
        p.spec = spec
        return p

    def test_no_title_means_no_search(self):
        """"jobs" as a query spends money to return noise."""
        from juicebox.jobmatch.discover_candidate import queries_for

        assert queries_for(self._pref(job_titles=[], locations=["nyc"])) == []
        assert queries_for(self._pref()) == []

    def test_queries_are_capped(self):
        from juicebox.jobmatch.discover_candidate import MAX_QUERIES, queries_for

        many = queries_for(self._pref(job_titles=[f"role {i}" for i in range(10)]))
        assert len(many) == MAX_QUERIES


class TestScrapedTitlesAreReadable:
    """Page titles arrive HTML-escaped: "Asset &amp; Wealth Management".

    Decoded at ingest rather than in each renderer -- the title reaches a text
    card, a poll option and a generated resume, and every one of those would
    otherwise have to remember to do it.
    """

    def test_entities_are_decoded_on_ingest(self, session, account):
        from juicebox.jobmatch.scrape import ingest_exa_results
        from juicebox.models import JobPosting

        ingest_exa_results(
            session,
            account["org_id"],
            [
                {
                    "url": "https://jobs.example.com/2",
                    "title": "R&amp;D Strategic Finance",
                    "text": "Body.",
                }
            ],
        )
        session.flush()
        posting = session.query(JobPosting).filter(
            JobPosting.source_url == "https://jobs.example.com/2"
        ).one()
        assert posting.title == "R&D Strategic Finance"
