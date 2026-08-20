"""Fee regex and warm-path matching."""

from __future__ import annotations

from job_fixtures import FEE_JD, NO_AGENCY_JD

from juicebox.jobmatch.fees import fee_signals
from juicebox.jobmatch.parse import parse_posting
from juicebox.jobmatch.referrals import is_incumbent, warm_paths
from juicebox.models import Candidate, Experience, JobPosting
from juicebox.repository import upsert_candidates


class TestFeeSignals:
    def test_referral_bonus_and_dollar_amount(self):
        bearing, terms, evidence = fee_signals(FEE_JD)
        assert bearing is True
        assert "referral" in terms
        assert evidence

    def test_no_agencies_is_not_a_fee(self):
        bearing, terms, evidence = fee_signals(NO_AGENCY_JD)
        assert bearing is False
        assert "no agencies" in evidence.lower()

    def test_bare_agency_is_not_a_signal(self):
        bearing, _, _ = fee_signals("We are not an agency. We ship software.")
        assert bearing is False


class TestWarmPaths:
    def test_incumbent_vs_alumni_and_blank_company(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        session.commit()
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()

        posting = JobPosting(
            org_id=account["org_id"],
            source="manual",
            source_id="modal-role",
            title="Staff Backend Engineer",
            company="Modal",
            company_normalized="modal",
            jd_text="Build infrastructure.",
            status="open",
        )
        session.add(posting)
        session.flush()

        paths = warm_paths(session, account["org_id"], posting)
        by_id = {p["candidate_id"]: p for p in paths}
        assert ada.id in by_id
        assert by_id[ada.id]["basis"] == "current"
        assert is_incumbent(ada, posting) is True

        # Alumni: Cleo used to be at Modal, now at Ramp.
        cleo = session.query(Candidate).filter_by(full_name="Cleo Rahman").one()
        session.add(
            Experience(
                candidate_id=cleo.id,
                position=1,
                company="Modal",
                title="Backend Engineer",
                is_current=False,
                end_date="2013-12",
            )
        )
        session.flush()
        paths = warm_paths(session, account["org_id"], posting)
        by_id = {p["candidate_id"]: p for p in paths}
        assert by_id[cleo.id]["basis"] == "alumni"
        assert is_incumbent(cleo, posting) is False

        blank = JobPosting(
            org_id=account["org_id"],
            source="project",
            source_id="blank-co",
            title="Something",
            company="",
            company_normalized="",
            status="open",
        )
        assert warm_paths(session, account["org_id"], blank) == []
        assert is_incumbent(ada, blank) is False

    def test_parse_fills_fee_and_paths(self, session, account, sample_candidates):
        upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates)
        posting = JobPosting(
            org_id=account["org_id"],
            source="manual",
            source_id="fee-role",
            title="Staff Backend Engineer",
            company="Modal",
            company_normalized="modal",
            jd_text=FEE_JD,
            status="pending_parse",
        )
        session.add(posting)
        session.commit()
        parse_posting(session, posting)
        assert posting.fee_bearing is True
        assert posting.has_warm_path is True
        assert any(p["basis"] == "current" for p in posting.referral_paths)
