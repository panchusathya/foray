"""score_pair, match upsert, watermarks, rematch queue."""

from __future__ import annotations

from datetime import timedelta

from juicebox.enrich import taxonomy as tx
from juicebox.jobmatch.engine import (
    dispatch_match_tick,
    match_candidate,
    match_posting,
    score_pair,
)
from juicebox.jobmatch.parse import parse_posting
from juicebox.jobmatch.queue import rematch_queue
from juicebox.models import (
    Candidate,
    Enrollment,
    JobMatch,
    JobMatchWatermark,
    JobPosting,
    Message,
    Project,
    Sequence,
    utcnow,
)
from juicebox.outreach.suppressions import suppress
from juicebox.repository import upsert_candidates
from juicebox.search.parser import ParsedQuery


def _parsed_posting(session, org_id, **fields) -> JobPosting:
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=fields.pop("source_id", "p1"),
        title=fields.pop("title", "Senior Machine Learning Engineer"),
        company=fields.pop("company", "Acme"),
        location=fields.pop("location", "San Francisco"),
        jd_text=fields.pop("jd_text", "Python and machine learning in production."),
        status="pending_parse",
        **fields,
    )
    posting.company_normalized = tx.normalize_company(posting.company)
    session.add(posting)
    session.flush()
    parse_posting(session, posting)
    session.commit()
    return posting


class TestScorePair:
    def test_both_directions_write_the_same_row(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(session, account["org_id"], source_id="pair")
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        match_posting(session, posting)
        session.commit()
        first = session.query(JobMatch).filter_by(
            job_posting_id=posting.id, candidate_id=ada.id
        ).one_or_none()
        assert first is not None
        match_id = first.id
        match_candidate(session, ada)
        session.commit()
        again = session.query(JobMatch).filter_by(
            job_posting_id=posting.id, candidate_id=ada.id
        ).one()
        assert again.id == match_id

    def test_incumbent_is_path_not_match(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="modal-job",
            company="Modal",
            title="Staff Backend Engineer",
            jd_text="Infrastructure and Python services.",
        )
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        match_posting(session, posting)
        session.commit()
        assert any(p["candidate_id"] == ada.id for p in posting.referral_paths)
        assert (
            session.query(JobMatch)
            .filter_by(job_posting_id=posting.id, candidate_id=ada.id)
            .one_or_none()
            is None
        )

    def test_recompute_preserves_human_status(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(session, account["org_id"], source_id="sticky")
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        match_posting(session, posting)
        session.commit()
        row = session.query(JobMatch).filter_by(
            job_posting_id=posting.id, candidate_id=ada.id
        ).one()
        row.status = "shortlisted"
        session.commit()
        match_posting(session, posting)
        session.commit()
        row = session.query(JobMatch).filter_by(id=row.id).one()
        assert row.status == "shortlisted"

        row.status = "dismissed"
        row.dismissed_reason = "not a fit"
        session.commit()
        match_posting(session, posting)
        session.commit()
        row = session.query(JobMatch).filter_by(id=row.id).one()
        assert row.status == "dismissed"
        assert row.dismissed_reason == "not a fit"

    def test_watermark_stops_reenqueue(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="floor",
            title="Intern",
            jd_text="Must be an intern in Antarctica studying underwater basket weaving.",
        )
        match_posting(session, posting)
        session.commit()
        assert session.query(JobMatch).filter_by(job_posting_id=posting.id).count() == 0
        marks = session.query(JobMatchWatermark).filter_by(org_id=account["org_id"]).all()
        assert marks
        from juicebox.jobs import KIND_JOBS_MATCH_ONE
        from juicebox.models import Job

        before = session.query(Job).filter_by(kind=KIND_JOBS_MATCH_ONE).count()
        dispatch_match_tick(session, account["org_id"])
        session.commit()
        after = session.query(Job).filter_by(kind=KIND_JOBS_MATCH_ONE).count()
        # Posting is freshly matched; candidates all have watermarks. Nothing new.
        assert after == before

    def test_spec_version_change_reques_recompute(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(session, account["org_id"], source_id="stale-spec")
        match_posting(session, posting)
        session.commit()
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        row = session.query(JobMatch).filter_by(
            job_posting_id=posting.id, candidate_id=ada.id
        ).one()
        old_version = row.spec_version
        posting.jd_text = posting.jd_text + " PyTorch required."
        parse_posting(session, posting)
        session.commit()
        assert posting.spec_version != old_version
        match_posting(session, posting)
        session.commit()
        row = session.query(JobMatch).filter_by(id=row.id).one()
        assert row.spec_version == posting.spec_version

    def test_mismatched_embedding_models_drop_vector_term(self):
        spec = ParsedQuery(job_titles=["machine learning engineer"], search_text="ml")
        from juicebox.search.index import CandidateDoc

        doc = CandidateDoc(
            id="x",
            full_name="Ada",
            headline="",
            summary="",
            current_title="Senior Machine Learning Engineer",
            current_title_normalized="machine learning engineer",
            current_company="Modal",
            current_company_normalized="modal",
            seniority="senior",
            years_experience=7,
            skills={"python", "pytorch"},
            industries=set(),
            location_parts=["san francisco"],
            location_city="San Francisco",
            location_region="CA",
            location_country="United States",
            open_to_remote=False,
            emails=[],
            linkedin_url="",
            doc_text="python pytorch",
            haystack="python pytorch",
        )
        pair = score_pair(
            doc,
            spec,
            cand_embedding=[1.0, 0.0],
            cand_embedding_model="hashing-512",
            posting_embedding=[1.0, 0.0],
            posting_embedding_model="voyage-voyage-3",
        )
        assert pair.vector_score == 0.0
        assert pair.score == pair.heuristic_score


class TestRematchQueue:
    def _setup_decline(self, session, account, sample_candidates, *, intent, email=None):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        project = Project(
            org_id=account["org_id"],
            name="Declined Role",
            role_title="Staff Backend",
            company="Acme",
        )
        session.add(project)
        session.flush()
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="other-role",
            title="Senior Machine Learning Engineer",
            company="OtherCo",
            jd_text="Python and machine learning in production in San Francisco.",
        )
        declined = _parsed_posting(
            session,
            account["org_id"],
            source_id="declined-role",
            title="Staff Backend Engineer",
            company="Acme",
            jd_text="Go and distributed systems.",
        )
        declined.project_id = project.id
        session.flush()
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        match_posting(session, posting)
        match_posting(session, declined)
        sequence = Sequence(org_id=account["org_id"], project_id=project.id, name="seq")
        session.add(sequence)
        session.flush()
        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=ada.id,
            project_id=project.id,
            status="replied",
            stopped_reason=intent,
        )
        session.add(enrollment)
        session.flush()
        session.add(
            Message(
                org_id=account["org_id"],
                enrollment_id=enrollment.id,
                candidate_id=ada.id,
                direction="inbound",
                status="received",
                reply_intent=intent,
                sent_at=utcnow() - timedelta(days=2),
                to_address=email or "ada@example.com",
            )
        )
        session.commit()
        return ada, posting, declined, project

    def test_not_interested_appears_without_declined_role(
        self, session, account, sample_candidates
    ):
        ada, posting, declined, _project = self._setup_decline(
            session, account, sample_candidates, intent="not_interested"
        )
        rows = rematch_queue(session, account["org_id"])
        assert rows
        row = next(r for r in rows if r["candidate"].id == ada.id)
        ids = [m.job_posting_id for m in row["matches"]]
        assert declined.id not in ids
        assert posting.id in ids

    def test_unsubscribe_is_absent(self, session, account, sample_candidates):
        ada, *_ = self._setup_decline(
            session, account, sample_candidates, intent="not_interested"
        )
        suppress(
            session,
            org_id=account["org_id"],
            email="ada@example.com",
            reason="unsubscribe",
        )
        session.commit()
        rows = rematch_queue(session, account["org_id"])
        assert all(r["candidate"].id != ada.id for r in rows)

    def test_active_enrollment_is_absent(self, session, account, sample_candidates):
        ada, *_ = self._setup_decline(
            session, account, sample_candidates, intent="not_interested"
        )
        other = Sequence(org_id=account["org_id"], name="other")
        session.add(other)
        session.flush()
        session.add(
            Enrollment(
                sequence_id=other.id,
                candidate_id=ada.id,
                status="active",
            )
        )
        session.commit()
        rows = rematch_queue(session, account["org_id"])
        assert all(r["candidate"].id != ada.id for r in rows)

    def test_not_now_is_gated(self, session, account, sample_candidates):
        ada, *_ = self._setup_decline(
            session, account, sample_candidates, intent="not_now"
        )
        rows = rematch_queue(session, account["org_id"])
        assert all(r["candidate"].id != ada.id for r in rows)
        # Age the reply past the gate.
        msg = session.query(Message).filter_by(candidate_id=ada.id, direction="inbound").one()
        msg.sent_at = utcnow() - timedelta(days=100)
        session.commit()
        rows = rematch_queue(session, account["org_id"])
        assert any(r["candidate"].id == ada.id for r in rows)
