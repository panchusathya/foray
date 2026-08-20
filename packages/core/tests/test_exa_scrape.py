"""Exa scrape: fake transport, dedupe, staleness, no-key skip."""

from __future__ import annotations

from datetime import timedelta

from juicebox.jobmatch.exa import ExaClient, ExaUnavailable, set_exa
from juicebox.jobmatch.scrape import (
    handle_scrape_one,
    ingest_exa_results,
    mark_stale_postings,
    run_scrape_query,
)
from juicebox.models import Job, JobPosting, JobScrapeQuery, utcnow


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
