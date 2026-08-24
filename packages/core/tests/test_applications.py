"""Applications: the status machine, the consent rule, the budgets, the bar.

The one invariant everything here defends: **nothing is asserted in a
candidate's name without a recorded, per-application yes.** The machine
refuses ``submitted`` without consent, the budgets stop volume from doing
what consent forbids one at a time, and the bar keeps unexplainable matches
from ever being shown.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

import pytest

from juicebox.applications import limits
from juicebox.applications.bar import passes_bar, proposal_reasons
from juicebox.applications.limits import (
    APPLICATIONS_PER_COMPANY_PER_30D,
    IGNORED_PROPOSAL_BREAKER,
    PROPOSALS_PER_WEEK,
    SUBMISSIONS_PER_WEEK,
)
from juicebox.applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    application_status_summary,
    build_package,
    create_application,
    current_documents,
    request_package,
)
from juicebox.applications.state import TransitionError, can_transition, transition
from juicebox.jobs import KIND_APP_PACKAGE
from juicebox.models import (
    APPLICATION_STATUSES,
    Candidate,
    JobMatch,
    JobPosting,
    JobProposal,
    Project,
    ProjectCandidate,
    utcnow,
)
from juicebox.presentation.jobcard import jobcard, render_text
from juicebox.repository import upsert_candidates


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    return session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()


def _posting(session, org_id, *, company="Anthropic", title="ML Engineer", suffix="1") -> JobPosting:
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"posting-{company.lower()}-{suffix}",
        title=title,
        company=company,
        company_normalized=company.lower(),
        jd_text="Build things.",
        status="open",
    )
    session.add(posting)
    session.flush()
    return posting


def _consented(application, session):
    application.approved_at = utcnow()
    session.flush()
    return application


class TestStateMachine:
    def test_the_happy_path(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        assert application.status == "draft"
        transition(session, application, to_status="prepared", actor="agent")
        assert application.prepared_at is not None
        transition(session, application, to_status="awaiting_consent", actor="agent")
        _consented(application, session)
        transition(session, application, to_status="submitted", actor="candidate")
        assert application.submitted_at is not None
        assert application.submitted_by == "candidate"
        transition(session, application, to_status="acknowledged", actor="recruiter")
        transition(session, application, to_status="interviewing", actor="recruiter")
        transition(session, application, to_status="offer", actor="recruiter")
        transition(session, application, to_status="rejected", actor="recruiter")
        assert application.closed_at is not None

    def test_no_consent_no_submission(self, session, account, candidate):
        """The rule that keeps us out of 'you applied to forty jobs as me'."""
        posting = _posting(session, account["org_id"])
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        transition(session, application, to_status="prepared", actor="agent")
        with pytest.raises(TransitionError, match="consent"):
            transition(session, application, to_status="submitted", actor="agent")
        # A consenting message id is the other valid form of the yes.
        application.consenting_message_id = None
        application.approved_at = utcnow()
        transition(session, application, to_status="submitted", actor="candidate")

    def test_terminal_states_are_frozen(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        transition(session, application, to_status="withdrawn", actor="candidate")
        for target in APPLICATION_STATUSES:
            if target == "withdrawn":
                continue
            assert not can_transition("withdrawn", target)

    def test_illegal_moves_refuse(self):
        assert not can_transition("draft", "submitted")
        assert not can_transition("submitted", "draft")
        assert not can_transition("rejected", "interviewing")

    def test_project_stage_syncs_forward_only(self, session, account, candidate):
        project = Project(org_id=account["org_id"], name="Mandate", role_title="MLE")
        session.add(project)
        session.flush()
        posting = _posting(session, account["org_id"])
        posting.project_id = project.id
        pc = ProjectCandidate(
            project_id=project.id, candidate_id=candidate.id, stage="offer"
        )
        session.add(pc)
        session.flush()

        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        assert application.project_id == project.id
        transition(session, application, to_status="prepared", actor="agent")
        _consented(application, session)
        transition(session, application, to_status="submitted", actor="candidate")
        # submitted implies screening -- but the recruiter already moved this
        # person to offer, and an echo must never pull a stage backwards.
        assert pc.stage == "offer"

    def test_match_status_is_never_mutated(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        match = JobMatch(
            org_id=account["org_id"],
            job_posting_id=posting.id,
            candidate_id=candidate.id,
            score=0.9,
            status="new",
        )
        session.add(match)
        session.flush()
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        # Provenance recorded, triage untouched: consent is a different axis.
        assert application.job_match_id == match.id
        assert match.status == "new"


class TestCreateGuards:
    def test_one_application_per_posting_ever(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        with pytest.raises(DuplicateApplicationError):
            create_application(
                db=session,
                org_id=account["org_id"],
                candidate_id=candidate.id,
                job_posting_id=posting.id,
            )

    def test_company_budget(self, session, account, candidate):
        org_id = account["org_id"]
        for index in range(APPLICATIONS_PER_COMPANY_PER_30D):
            posting = _posting(session, org_id, suffix=str(index))
            create_application(
                db=session,
                org_id=org_id,
                candidate_id=candidate.id,
                job_posting_id=posting.id,
            )
        blocked = _posting(session, org_id, suffix="over")
        with pytest.raises(ApplicationError, match="30 days"):
            create_application(
                db=session,
                org_id=org_id,
                candidate_id=candidate.id,
                job_posting_id=blocked.id,
            )
        # A different company is a different budget.
        other = _posting(session, org_id, company="Figma", suffix="other")
        create_application(
            db=session, org_id=org_id, candidate_id=candidate.id, job_posting_id=other.id
        )

    def test_api_duplicate_is_a_409(self, client, session, account, candidate):
        posting = _posting(session, account["org_id"])
        session.commit()
        body = {"candidate_id": candidate.id, "job_posting_id": posting.id}
        first = client.post("/v1/applications", json=body, headers=account["headers"])
        assert first.status_code == 201, first.text
        second = client.post("/v1/applications", json=body, headers=account["headers"])
        assert second.status_code == 409


class TestSubmissionBudget:
    def test_five_a_week_then_stop(self, session, account, candidate):
        org_id = account["org_id"]
        now = utcnow()
        for index in range(SUBMISSIONS_PER_WEEK):
            posting = _posting(session, org_id, company=f"Company{index}", suffix=str(index))
            application = create_application(
                db=session, org_id=org_id, candidate_id=candidate.id, job_posting_id=posting.id
            )
            application.submitted_at = now - timedelta(days=index)
            application.status = "submitted"
        session.flush()

        posting = _posting(session, org_id, company="OneMore", suffix="x")
        application = create_application(
            db=session, org_id=org_id, candidate_id=candidate.id, job_posting_id=posting.id
        )
        transition(session, application, to_status="prepared", actor="agent")
        _consented(application, session)
        with pytest.raises(TransitionError, match="budget"):
            transition(session, application, to_status="submitted", actor="candidate")


class TestProposalBudgets:
    def _proposal(self, session, org_id, candidate_id, posting_id, **kwargs):
        row = JobProposal(
            org_id=org_id, candidate_id=candidate_id, job_posting_id=posting_id, **kwargs
        )
        session.add(row)
        session.flush()
        return row

    def test_weekly_budget(self, session, account, candidate):
        org_id = account["org_id"]
        for index in range(PROPOSALS_PER_WEEK):
            posting = _posting(session, org_id, company=f"Co{index}", suffix=str(index))
            self._proposal(session, org_id, candidate.id, posting.id, status="accepted",
                           responded_at=utcnow())
        decision = limits.may_propose(db=session, org_id=org_id, candidate_id=candidate.id)
        assert not decision and decision.reason == limits.REASON_PROPOSAL_BUDGET

    def test_one_proposal_message_per_day(self, session, account, candidate, linq_message_row):
        org_id = account["org_id"]
        posting = _posting(session, org_id)
        self._proposal(
            session,
            org_id,
            candidate.id,
            posting.id,
            message_id=linq_message_row.id,
            status="accepted",
            responded_at=utcnow(),
        )
        decision = limits.may_propose(db=session, org_id=org_id, candidate_id=candidate.id)
        assert not decision and decision.reason == limits.REASON_PROPOSAL_MESSAGE_BUDGET
        # The portal feed pulls rather than pushes: no message budget there.
        decision = limits.may_propose(
            db=session, org_id=org_id, candidate_id=candidate.id, in_new_message=False
        )
        assert decision

    def test_ignored_breaker(self, session, account, candidate):
        org_id = account["org_id"]
        for index in range(IGNORED_PROPOSAL_BREAKER):
            posting = _posting(session, org_id, company=f"Quiet{index}", suffix=str(index))
            self._proposal(session, org_id, candidate.id, posting.id)
        decision = limits.may_propose(
            db=session, org_id=org_id, candidate_id=candidate.id, in_new_message=False
        )
        assert not decision and decision.reason == limits.REASON_IGNORED_BREAKER


@pytest.fixture
def linq_message_row(session, account):
    from juicebox.messaging.channels import LINQ
    from juicebox.messaging.threads import open_thread, record_inbound

    thread = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-limits",
        from_address="+14155550111",
        consent_messaging=True,
    )
    message, _ = record_inbound(session, thread=thread, body="hi", provider_native_id="lm-1")
    return message


class TestPackageBuild:
    def test_template_package_end_to_end(self, session, account, candidate):
        """No API key anywhere: the package still builds, honestly flat."""
        posting = _posting(session, account["org_id"])
        posting.parsed_spec = {"must_have_skills": ["kubernetes"], "job_titles": ["ml engineer"]}
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        result = build_package(session, application)
        assert application.status == "prepared"
        assert application.package_error == ""
        documents = current_documents(session, application)
        assert set(documents) == {"resume_generated", "cover_letter"}

        from juicebox import storage

        resume = documents["resume_generated"]
        assert resume.generated_by == "template"
        data = storage.read_bytes(resume.storage_path)
        assert data.startswith(b"%PDF-")
        # The deterministic skill reorder: the posting's must-have leads.
        skills = resume.structured["skills"]
        assert skills[0].lower() == "kubernetes"

        answers = {a["key"]: a for a in application.form_answers}
        assert answers["why_company"]["answer"] == ""
        assert answers["why_company"]["provenance"] == "none"
        assert result["answers"] == 4

    def test_request_package_collapses_double_clicks(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        first = request_package(session, application)
        second = request_package(session, application)
        assert first.id == second.id
        assert first.kind == KIND_APP_PACKAGE

    def test_status_summary_reads_like_a_text(self, session, account, candidate):
        posting = _posting(session, account["org_id"])
        application = create_application(
            db=session,
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
        )
        transition(session, application, to_status="prepared", actor="agent")
        summary = application_status_summary(
            session, org_id=account["org_id"], candidate_id=candidate.id
        )
        assert "ML Engineer at Anthropic" in summary
        assert "review" in summary
        assert "0." not in summary  # never a score


class TestBar:
    def _match(self, session, account, candidate, posting, *, score=0.8, criteria=None):
        match = JobMatch(
            org_id=account["org_id"],
            job_posting_id=posting.id,
            candidate_id=candidate.id,
            score=score,
            criteria=criteria or [],
            status="new",
        )
        session.add(match)
        session.flush()
        return match

    _STRONG: ClassVar[dict] = {"name": "title", "label": "Title", "kind": "title", "weight": 1.0,
               "status": "strong", "score": 0.95, "evidence": "Current title matches", "sources": []}
    _WEAK: ClassVar[dict] = {"name": "loc", "label": "Location", "kind": "location", "weight": 1.0,
             "status": "partial", "score": 0.5, "evidence": "", "sources": []}
    _VETO: ClassVar[dict] = {"name": "pref_excluded", "label": "Excluded company", "kind": "preference",
             "weight": 1.0, "status": "miss", "score": 0.0,
             "evidence": "They said never Acme", "sources": ["intake"], "veto": True}

    def test_floor_veto_explainability_freshness(self, session, account, candidate):
        posting = _posting(session, account["org_id"])

        ok = self._match(session, account, candidate, posting, criteria=[self._STRONG])
        assert passes_bar(ok, posting).ok

        low = self._match(
            session, account, candidate,
            _posting(session, account["org_id"], suffix="low"),
            score=0.2, criteria=[self._STRONG],
        )
        assert passes_bar(low, _posting(session, account["org_id"], suffix="low2")).why_not

        vetoed = self._match(
            session, account, candidate,
            _posting(session, account["org_id"], suffix="veto"),
            score=0.99, criteria=[self._STRONG, self._VETO],
        )
        decision = passes_bar(vetoed, session.get(JobPosting, vetoed.job_posting_id))
        assert not decision.ok and decision.why_not.startswith("veto")

        # Vector-only carry: nothing strong to explain with.
        unexplained = self._match(
            session, account, candidate,
            _posting(session, account["org_id"], suffix="vec"),
            score=0.9, criteria=[self._WEAK],
        )
        decision = passes_bar(unexplained, session.get(JobPosting, unexplained.job_posting_id))
        assert not decision.ok and decision.why_not == "unexplainable"

        closed = _posting(session, account["org_id"], suffix="closed")
        closed.status = "closed"
        stale_match = self._match(session, account, candidate, closed, criteria=[self._STRONG])
        assert passes_bar(stale_match, closed).why_not == "stale-or-closed"

    def test_reasons_never_include_a_score(self):
        reasons = proposal_reasons([self._STRONG, self._WEAK])
        assert reasons == ["Current title matches"]
        assert all("0.9" not in reason for reason in reasons)


class TestJobCard:
    def test_projection_and_text_renderer(self, session, account, candidate):
        posting = _posting(session, account["org_id"], company="Goldman Sachs", title="IB Associate")
        match = JobMatch(
            org_id=account["org_id"],
            job_posting_id=posting.id,
            candidate_id=candidate.id,
            score=0.91,
            criteria=[TestBar._STRONG],
            status="new",
        )
        session.add(match)
        session.flush()

        card = jobcard(posting, match)
        assert card["company"] == "Goldman Sachs"
        assert card["reasons"] == ["Current title matches"]
        assert card["recruiter_only"]["score"] == 0.91

        text = render_text([card], footer="More: https://apply.example/m/tok")
        assert "1. IB Associate — Goldman Sachs" in text
        assert "fits: Current title matches" in text
        assert "0.91" not in text  # the candidate never sees a score
        assert "apply to 1" in text

    def test_networking_suggestions_and_intro(self, session, account, sample_candidates):
        org_id = account["org_id"]
        upsert_candidates(session, org_id=org_id, payloads=sample_candidates)
        session.commit()
        ada = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
        bo = session.query(Candidate).filter_by(full_name="Bo Lindqvist").one()

        # Bo applies to a role at Modal; Ada works there -> a suggestion.
        posting = _posting(session, org_id, company="Modal", title="Backend Engineer")
        application = create_application(
            db=session, org_id=org_id, candidate_id=bo.id, job_posting_id=posting.id
        )
        from juicebox.applications.networking import draft_intro, refresh_networking

        entries = refresh_networking(session, application)
        ids = [entry["candidate_id"] for entry in entries]
        assert ada.id in ids
        assert bo.id not in ids  # never suggest the applicant to themself

        entry = draft_intro(session, application, ada.id)
        assert entry["generated_by"] == "template"
        assert "Backend Engineer" in entry["draft"]
        assert "Modal" in entry["draft"]
        # First person as the candidate, and no ask-for-referral in message one.
        assert "refer" not in entry["draft"].lower()

        with pytest.raises(ValueError):
            draft_intro(session, application, "cand_not_on_list")


class TestUiMirrorsTheMachine:
    """The transition table now exists twice: once in ``applications/state.py``
    and once in ``packages/web/src/lib/api.js`` so the UI can avoid offering a
    button the server will refuse.

    Two copies of a rule drift. The repo already guards this shape of
    duplication by reading the other file and comparing (see
    ``TestPersonaCopy`` reading ``deploy/retell_agents.py``); this does the
    same across the language boundary. The server stays authoritative — it
    answers 409 — but a UI that offers an impossible move reads as a bug.
    """

    @staticmethod
    def _js_block(name: str) -> str:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[3] / "packages" / "web" / "src" / "lib" / "api.js"
        ).read_text(encoding="utf-8")
        start = source.index(f"export const {name}")
        return source[start : source.index("};", start)]

    def test_the_status_list_matches(self):
        import re

        from juicebox.models import APPLICATION_STATUSES

        block = self._js_block("APPLICATION_STATUSES")
        found = tuple(re.findall(r"'([a-z_]+)'", block))
        assert found == APPLICATION_STATUSES

    def test_the_transition_table_matches(self):
        import re

        from juicebox.applications.state import TRANSITIONS

        block = self._js_block("APPLICATION_TRANSITIONS")
        found = {
            key: set(re.findall(r"'([a-z_]+)'", targets))
            for key, targets in re.findall(r"^\s*([a-z_]+): \[(.*)\],$", block, re.M)
        }
        assert found == {key: set(value) for key, value in TRANSITIONS.items()}

    def test_the_status_labels_cover_every_status(self):
        import re

        from juicebox.models import APPLICATION_STATUSES

        block = self._js_block("APPLICATION_STATUS_LABELS")
        labelled = set(re.findall(r"^\s*([a-z_]+):", block, re.M))
        assert labelled == set(APPLICATION_STATUSES)
