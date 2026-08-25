"""score_pair, match upsert, watermarks, rematch queue."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from juicebox.enrich import taxonomy as tx
from juicebox.jobmatch.engine import (
    dispatch_match_tick,
    match_candidate,
    match_posting,
    score_pair,
)
from juicebox.jobmatch.parse import parse_posting
from juicebox.jobmatch.preference import preference_term
from juicebox.jobmatch.queue import rematch_queue
from juicebox.models import (
    Candidate,
    CandidatePreference,
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


class TestHumanDecisionsOutliveTheHeuristics:
    """A shortlist or a dismissal has a reason attached; a score does not."""

    def _posting(self, session, org, **extra):
        posting = JobPosting(
            org_id=org, source="exa", source_id=extra.pop("source_id", "sid-human"),
            status="open", title="Staff ML Engineer",
            title_normalized="staff ml engineer", company="Acme",
            company_normalized="acme", **extra,
        )
        session.add(posting)
        session.flush()
        return posting

    def test_a_dismissed_match_is_not_deleted_by_the_incumbency_sweep(
        self, session, account
    ):
        from juicebox.jobmatch.engine import _drop_match

        org = account["org_id"]
        posting = self._posting(session, org)
        candidate = Candidate(
            org_id=org, source="manual", source_id="ada-human", full_name="Ada Lovelace",
        )
        session.add(candidate)
        session.flush()
        match = JobMatch(
            org_id=org, job_posting_id=posting.id, candidate_id=candidate.id,
            status="dismissed", dismissed_reason="not a fit", score=0.4,
        )
        session.add(match)
        session.flush()

        _drop_match(session, posting.id, candidate.id)
        session.flush()

        rows = session.scalars(select(JobMatch)).all()
        assert len(rows) == 1, "a human dismissal must not be erased by a rescore"
        assert rows[0].status == "dismissed"

    def test_a_new_match_is_still_dropped(self, session, account):
        from juicebox.jobmatch.engine import _drop_match

        org = account["org_id"]
        posting = self._posting(session, org, source_id="sid-human-2")
        candidate = Candidate(
            org_id=org, source="manual", source_id="cleo-human", full_name="Cleo Park",
        )
        session.add(candidate)
        session.flush()
        session.add(
            JobMatch(
                org_id=org, job_posting_id=posting.id, candidate_id=candidate.id,
                status="new", score=0.4,
            )
        )
        session.flush()

        _drop_match(session, posting.id, candidate.id)
        session.flush()

        assert session.scalars(select(JobMatch)).all() == []


class TestABorrowedPostingOutlivesTheMandate:
    def test_deleting_a_project_does_not_archive_a_market_posting(
        self, session, account
    ):
        from juicebox.jobmatch.sync import archive_project_posting

        org = account["org_id"]
        project = Project(org_id=org, name="Staff ML (Acme)", status="active")
        session.add(project)
        session.flush()
        posting = JobPosting(
            org_id=org, source="exa", source_id="sid-borrowed", status="open",
            title="Staff ML Engineer", title_normalized="staff ml engineer",
            project_id=project.id,
        )
        session.add(posting)
        session.flush()

        archive_project_posting(session, project.id)
        session.flush()

        assert posting.project_id is None
        assert posting.status == "open", "the open role outlives the client going quiet"

    def test_the_projects_own_posting_is_archived(self, session, account):
        from juicebox.jobmatch.sync import archive_project_posting

        org = account["org_id"]
        project = Project(org_id=org, name="Own Mandate", status="active")
        session.add(project)
        session.flush()
        posting = JobPosting(
            org_id=org, source="project", source_id=f"project:{project.id}",
            status="open", title="Own Mandate", project_id=project.id,
        )
        session.add(posting)
        session.flush()

        archive_project_posting(session, project.id)
        session.flush()

        assert posting.status == "archived"

    def test_a_project_edit_does_not_blank_the_scraped_description(
        self, session, account
    ):
        from juicebox.jobmatch.sync import upsert_project_posting

        org = account["org_id"]
        project = Project(org_id=org, name="Staff ML (Acme)", status="active")
        session.add(project)
        session.flush()
        posting = JobPosting(
            org_id=org, source="exa", source_id="sid-mirror", status="open",
            title="Staff ML Engineer", title_normalized="staff ml engineer",
            jd_text="Six kilobytes of real JD.", company="Acme",
            company_normalized="acme", location="Remote", project_id=project.id,
        )
        session.add(posting)
        session.flush()

        # A Project carries no description unless someone typed one, so the
        # mirror must not write its blank over the scraped JD.
        upsert_project_posting(session, project)
        session.flush()

        assert posting.jd_text == "Six kilobytes of real JD."
        assert posting.company == "Acme"
        assert posting.location == "Remote"


class TestPreferenceTerm:
    def test_no_preference_scores_like_today(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(session, account["org_id"], source_id="nopref")
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        spec = ParsedQuery.from_dict(posting.parsed_spec or {})
        from juicebox.search.index import candidate_to_doc

        doc = candidate_to_doc(ada)
        kwargs = dict(
            cand_embedding=ada.embedding,
            cand_embedding_model=ada.embedding_model or "",
            posting_embedding=posting.embedding,
            posting_embedding_model=posting.embedding_model or "",
        )
        today = score_pair(doc, spec, **kwargs)
        with_none = score_pair(doc, spec, preference=None, posting=posting, **kwargs)
        assert with_none.score == today.score
        assert with_none.criteria == today.criteria

    def test_excluded_company_drops_new_leaves_shortlisted(
        self, session, account, sample_candidates
    ):
        from juicebox.models import CandidatePreference

        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(session, account["org_id"], source_id="veto-co")
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        match_posting(session, posting)
        session.commit()
        row = session.query(JobMatch).filter_by(
            job_posting_id=posting.id, candidate_id=ada.id
        ).one()
        row.status = "shortlisted"
        session.commit()

        other = _parsed_posting(
            session,
            account["org_id"],
            source_id="veto-co-2",
            title="Senior Machine Learning Engineer",
            company="OtherCo",
            jd_text="Python and machine learning in production.",
        )
        match_posting(session, other)
        session.commit()
        assert (
            session.query(JobMatch)
            .filter_by(job_posting_id=other.id, candidate_id=ada.id)
            .one()
            .status
            == "new"
        )

        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=ada.id,
                spec={"excluded_companies": ["Acme", "OtherCo"]},
            )
        )
        session.commit()
        match_posting(session, posting)
        match_posting(session, other)
        session.commit()
        kept = session.query(JobMatch).filter_by(
            job_posting_id=posting.id, candidate_id=ada.id
        ).one()
        assert kept.status == "shortlisted"
        assert kept.score == 0.0
        assert (
            session.query(JobMatch)
            .filter_by(job_posting_id=other.id, candidate_id=ada.id)
            .one_or_none()
            is None
        )

    def _wants_400k(self, session, account, **spec):
        from juicebox.models import CandidatePreference

        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=ada.id,
                spec={"job_titles": ["machine learning engineer"]},
                target_total=400000,
                **spec,
            )
        )
        session.commit()
        return ada

    def _match_exists(self, session, posting, candidate_id):
        match_posting(session, posting)
        session.commit()
        return (
            session.query(JobMatch)
            .filter_by(job_posting_id=posting.id, candidate_id=candidate_id)
            .one_or_none()
            is not None
        )

    def test_comp_veto_reads_a_spec_the_jd_parser_actually_produces(
        self, session, account, sample_candidates
    ):
        """The band comes from the JD, not from a hand-planted key.

        The first version of this test wrote ``compensation_min`` into
        ``parsed_spec`` directly. ``ParsedQuery`` has no such field, so the
        veto passed its test and could never fire in production. Driving it
        through ``parse_posting`` is what makes the test worth anything.
        """
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="comp-capped",
            jd_text="Machine learning in production. Base salary 140,000 to 150,000 USD.",
        )
        assert posting.parsed_spec["max_compensation"] == 150000.0
        ada = self._wants_400k(session, account)
        assert not self._match_exists(session, posting, ada.id)

    def test_a_silent_posting_is_not_vetoed(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(session, account["org_id"], source_id="comp-silent")
        assert posting.parsed_spec["max_compensation"] is None
        ada = self._wants_400k(session, account)
        assert self._match_exists(session, posting, ada.id)

    def test_a_posting_stating_only_a_floor_is_not_vetoed(
        self, session, account, sample_candidates
    ):
        """"From 180k" has no ceiling to fail -- it could pay 450k."""
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="comp-floor-only",
            jd_text="Machine learning in production. Salary starting at 180,000 USD.",
        )
        assert posting.parsed_spec["min_compensation"] == 180000.0
        assert posting.parsed_spec["max_compensation"] is None
        ada = self._wants_400k(session, account)
        assert self._match_exists(session, posting, ada.id)

    def test_a_different_currency_is_not_compared(self, session, account, sample_candidates):
        """Converting would need a rate, and a guessed rate drops real roles."""
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="comp-inr",
            jd_text="Machine learning in production. Base salary 150,000 INR.",
        )
        assert posting.parsed_spec["compensation_currency"] == "INR"
        ada = self._wants_400k(session, account, comp_currency="USD")
        assert self._match_exists(session, posting, ada.id)

    def test_a_401k_match_is_not_a_stated_salary(self, session, account, sample_candidates):
        """The single most common false positive in a job description."""
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = _parsed_posting(
            session,
            account["org_id"],
            source_id="comp-401k",
            jd_text="Machine learning in production. Competitive salary, 401k match, PTO.",
        )
        assert posting.parsed_spec["max_compensation"] is None
        ada = self._wants_400k(session, account)
        assert self._match_exists(session, posting, ada.id)

    def test_upsert_preference_enqueues_a_rescore(self, session, account, sample_candidates):
        from juicebox.intake.extract import ExtractionResult
        from juicebox.intake.schema import coverage
        from juicebox.intake.service import upsert_preference
        from juicebox.jobs import KIND_JOBS_MATCH_ONE
        from juicebox.models import IntakeCall, Job

        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        call = IntakeCall(
            org_id=account["org_id"],
            candidate_id=ada.id,
            provider="fake",
            provider_call_id="pref-enqueue",
            transcript="done",
            status="completed",
        )
        session.add(call)
        session.flush()
        record = {
            "spec": {"job_titles": ["machine learning engineer"], "locations": ["San Francisco"]},
            "target_total": 250000,
        }
        result = ExtractionResult(
            record=record,
            extractor="test",
            coverage=coverage(record),
            review_needed=True,
            review_reason="test",
        )
        upsert_preference(session, call=call, result=result)
        session.commit()
        jobs = session.query(Job).filter_by(kind=KIND_JOBS_MATCH_ONE).all()
        assert any(job.payload.get("candidate_id") == ada.id for job in jobs)

    def test_tick_sees_preference_updated_at(self, session, account, sample_candidates):
        from juicebox.jobmatch.preference import JOB_MATCH_ENGINE_VERSION
        from juicebox.jobs import KIND_JOBS_MATCH_ONE
        from juicebox.models import CandidatePreference, Job

        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        _parsed_posting(session, account["org_id"], source_id="tick-pref")
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        match_candidate(session, ada)
        session.commit()
        before = session.query(Job).filter_by(kind=KIND_JOBS_MATCH_ONE).count()

        pref = CandidatePreference(
            org_id=account["org_id"],
            candidate_id=ada.id,
            spec={"job_titles": ["machine learning engineer"]},
        )
        session.add(pref)
        session.flush()
        # Preference is newer than the watermark; the candidate row is not.
        ada.updated_at = utcnow() - timedelta(days=2)
        pref.updated_at = utcnow() + timedelta(minutes=1)
        session.commit()

        dispatch_match_tick(session, account["org_id"])
        session.commit()
        after = session.query(Job).filter_by(kind=KIND_JOBS_MATCH_ONE).count()
        assert after > before

        mark = session.query(JobMatchWatermark).filter_by(candidate_id=ada.id).one()
        mark.engine_version = "old"
        mark.computed_at = utcnow() + timedelta(hours=1)
        ada.updated_at = utcnow() - timedelta(days=2)
        pref.updated_at = utcnow() - timedelta(days=2)
        session.commit()
        # Same stamp would collide with the job we just queued; the tick
        # still reports the enqueue so a lost job is retried.
        result = dispatch_match_tick(session, account["org_id"])
        session.commit()
        assert result["queued"] >= 1
        assert JOB_MATCH_ENGINE_VERSION


class TestTitleOutweighsTheRest:
    """The prod bug: a title bullseye and a title miss both scored 0.500.

    A thread asked for strategic finance in the Bay Area, and every real
    strat-fin role came out level with "Junior Software Investor" -- a flat
    mean let seniority pay back everything title lost, and the
    profile-similarity terms then broke the tie toward the career the
    candidate was trying to leave.
    """

    WANTED = {
        "job_titles": ["strategic finance"],
        "seniority": ["junior"],
        "locations": ["San Francisco Bay Area"],
        # Extraction copies the function in here too. It is not an industry.
        "industries": ["strategic finance"],
    }

    def _score(self, **have):
        pref = CandidatePreference(spec=dict(self.WANTED))
        score, criteria = preference_term(pref, ParsedQuery.from_dict(have))
        return score, {c["name"]: c["score"] for c in criteria}

    def test_the_role_they_asked_for_beats_the_one_they_are_leaving(self):
        wanted_role, _ = self._score(
            job_titles=["strategic finance", "finance", "fp"],
            seniority=["mid"],
            locations=["new york", "san francisco"],
            industries=["financial services", "technology"],
        )
        old_career, _ = self._score(
            job_titles=["software investor", "venture capitalist", "vc"],
            seniority=["mid", "junior"],
            locations=["san francisco"],
            industries=["venture capital", "software"],
        )
        assert wanted_role > old_career

    def test_an_adjacent_rung_is_not_a_hard_miss(self):
        _, criteria = self._score(
            job_titles=["strategic finance"], seniority=["mid"], locations=["san francisco"]
        )
        assert 0.0 < criteria["pref_seniority"] < 1.0

    def test_a_function_stated_as_an_industry_is_not_scored_twice(self):
        """"strategic finance" is the title, so it must not also be a miss."""
        _, criteria = self._score(
            job_titles=["strategic finance"],
            seniority=["mid"],
            locations=["san francisco"],
            industries=["financial services"],
        )
        assert "pref_industry" not in criteria
