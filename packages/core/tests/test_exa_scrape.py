"""Exa scrape: fake transport, dedupe, staleness, no-key skip."""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select

from juicebox.config import get_settings
from juicebox.jobmatch.exa import ExaClient, ExaUnavailable, set_exa
from juicebox.jobmatch.scrape import (
    ensure_auto_queries,
    handle_scrape_one,
    ingest_exa_results,
    is_due,
    mark_stale_postings,
    run_scrape_query,
)
from juicebox.models import Job, JobPosting, JobScrapeQuery, Project, utcnow


def _fake_results():
    return [
        {
            "id": "exa-1",
            "url": "https://jobs.example.com/ml",
            "title": "Senior Machine Learning Engineer",
            "text": "Python and ranking. $5,000 referral bonus for a successful referral.",
            "author": "Acme",
        },
        {
            "id": "exa-2",
            "url": "https://jobs.example.com/backend",
            "title": "Staff Backend Engineer",
            "text": "Go and Postgres.",
            "author": "Ramp",
        },
    ]


def _transport(_method, url, payload):
    if "contents" in url:
        return {"results": []}
    return {"results": _fake_results()}


class TestExaClient:
    def test_no_key_raises(self):
        client = ExaClient(api_key="")
        assert client.available is False
        try:
            client.search_job_postings("ml engineer")
            raise AssertionError("expected ExaUnavailable")
        except ExaUnavailable:
            pass


class TestIngest:
    def test_dedupe_on_source_id(self, session, account):
        first = ingest_exa_results(session, account["org_id"], _fake_results())
        session.commit()
        assert first["created"] == 2
        second = ingest_exa_results(session, account["org_id"], _fake_results())
        session.commit()
        assert second["created"] == 0
        assert second["updated"] == 2
        assert session.query(JobPosting).count() == 2

    def test_staleness_transition(self, session, account):
        ingest_exa_results(session, account["org_id"], _fake_results())
        session.commit()
        posting = session.query(JobPosting).first()
        posting.status = "open"
        posting.source = "exa"
        posting.last_seen_at = utcnow() - timedelta(days=30)
        session.commit()
        n = mark_stale_postings(session, account["org_id"])
        assert n >= 1
        session.refresh(posting)
        assert posting.status == "stale"

    def test_no_key_skips_cleanly(self, session, account):
        query = JobScrapeQuery(
            org_id=account["org_id"],
            query="machine learning engineer job posting",
            cadence_minutes=1440,
            status="active",
        )
        session.add(query)
        session.commit()
        result = run_scrape_query(session, query)
        assert result["skipped"] == "no_exa_key"
        job = Job(
            org_id=account["org_id"],
            kind="jobs.scrape.one",
            payload={"query_id": query.id},
            idempotency_key="t",
        )
        session.add(job)
        session.flush()
        skipped = handle_scrape_one(session, job, utcnow())
        assert skipped["skipped"] == "no_exa_key"

    def test_fake_transport_ingests(self, session, account):
        set_exa(ExaClient(api_key="test", transport=_transport))
        query = JobScrapeQuery(
            org_id=account["org_id"],
            query="ml job posting",
            cadence_minutes=1440,
            status="active",
        )
        session.add(query)
        session.commit()
        result = run_scrape_query(session, query)
        assert result["created"] == 2
        assert session.query(JobPosting).count() == 2


class TestBilledCallsAreNotRepeated:
    """Every Exa call costs money, so a failure must not become a loop."""

    def test_a_failed_search_still_stamps_last_run_at(self, session, account):
        def failing(_method, _url, _payload):
            raise httpx.ReadTimeout("exa took too long")

        set_exa(ExaClient(api_key="k", transport=failing))
        query = JobScrapeQuery(
            org_id=account["org_id"], query="ml engineer job posting",
            cadence_minutes=1440, status="active", origin="auto",
        )
        session.add(query)
        session.flush()

        result = run_scrape_query(session, query, now=utcnow())

        # Unstamped means is_due() says "never ran" and the 60s tick re-bills
        # the same search forever, five retries at a time.
        assert query.last_run_at is not None
        assert result["skipped"] == "exa_request_failed"
        assert is_due(query, now=utcnow()) is False

    def test_a_contents_failure_keeps_the_search_we_paid_for(self):
        def half_broken(_method, url, _payload):
            if "contents" in url:
                raise httpx.HTTPStatusError(
                    "429", request=httpx.Request("POST", url),
                    response=httpx.Response(429),
                )
            return {"results": [{"id": "exa-1", "url": "u", "title": "ML Eng", "text": ""}]}

        client = ExaClient(api_key="k", transport=half_broken)
        # The /contents backfill is best-effort enrichment of an already-billed
        # search; its failure must not discard the results.
        assert len(client.search_job_postings("ml")) == 1

    def test_the_auto_query_cap_is_never_exceeded(self, session, account):
        org = account["org_id"]
        cap = get_settings().job_scrape_max_auto_queries
        for n in range(cap):
            session.add(
                JobScrapeQuery(org_id=org, query=f"existing {n} job posting",
                               cadence_minutes=1440, status="active", origin="auto")
            )
        # Fresh title families the org has no query for yet.
        for n in range(4):
            session.add(
                JobPosting(org_id=org, source="exa", source_id=f"sid-cap-{n}",
                           status="open", title=f"Unseen Role {n}",
                           title_normalized=f"unseen role {n}")
            )
        session.flush()

        ensure_auto_queries(session, org)
        session.flush()

        total = len([
            q for q in session.scalars(select(JobScrapeQuery)).all() if q.origin == "auto"
        ])
        assert total == cap, "checking the cap after the insert lets it drift up every tick"


class TestProjectLinkedPostingsAreProtected:
    def _linked(self, session, org):
        project = Project(org_id=org, name="Staff ML (Acme)", status="active")
        session.add(project)
        session.flush()
        posting = JobPosting(
            org_id=org, source="exa", source_id="sid-linked", status="open",
            title="Staff ML Engineer", title_normalized="staff ml engineer",
            jd_text="The full scraped description.", company="Acme",
            project_id=project.id, last_seen_at=utcnow(),
        )
        session.add(posting)
        session.flush()
        return posting

    def test_the_stale_sweep_leaves_a_live_mandate_alone(self, session, account):
        posting = self._linked(session, account["org_id"])
        posting.last_seen_at = utcnow() - timedelta(days=365)
        session.flush()

        mark_stale_postings(session, account["org_id"], now=utcnow())
        session.flush()

        assert posting.status == "open", "a posting on a live mandate is not market noise"

    def test_a_rescrape_does_not_overwrite_the_mandate_text(self, session, account):
        posting = self._linked(session, account["org_id"])
        ingest_exa_results(
            session, account["org_id"],
            [{
                "id": "exa-1", "url": "", "title": "SEO-spam Title",
                "text": "truncated page scrape", "author": "Acme",
            }],
            now=utcnow(),
        )
        session.flush()

        assert posting.jd_text == "The full scraped description."
        assert posting.title == "Staff ML Engineer"

    def test_an_over_long_title_is_cut_to_fit_the_column(self, session, account):
        ingest_exa_results(
            session, account["org_id"],
            [{"id": "x", "url": "https://e.com/x", "title": "L" * 400,
              "text": "body", "author": "C" * 400}],
            now=utcnow(),
        )
        session.flush()

        row = session.scalars(
            select(JobPosting).where(JobPosting.source_id != "sid-linked")
        ).first()
        # Postgres rejects an over-length value outright; SQLite silently
        # accepts it, so only prod would have seen this.
        assert len(row.title) <= 240
        assert len(row.company) <= 240
