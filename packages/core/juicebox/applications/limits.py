"""Application and proposal budgets — CANDIDATE_AGENT_PLAN §4.3 in one place.

The binding constraint is not our cost. It is the candidate's reputation with
an employer, which we can spend and they cannot get back. Every budget returns
a ``LimitDecision`` so call sites act on a verdict rather than re-deriving the
arithmetic — the same reason ``schema.coverage()`` owns the intake gate.

| Limit                       | Value                                   |
|-----------------------------|-----------------------------------------|
| Per posting                 | exactly one application, ever (unique)  |
| Per company                 | 2 per rolling 30 days                   |
| Per candidate, submissions  | 5 per rolling 7 days                    |
| Per candidate, proposals    | 10 per rolling 7 days                   |
| Proposals per message       | 3                                       |
| Proposal messages per day   | 1                                       |
| Ignored-proposal breaker    | 3 consecutive unanswered → stop, wait   |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Application, JobPosting, JobProposal, utcnow

APPLICATIONS_PER_COMPANY_PER_30D = 2
SUBMISSIONS_PER_WEEK = 5
PROPOSALS_PER_WEEK = 10
MAX_PROPOSALS_PER_MESSAGE = 3
PROPOSAL_MESSAGES_PER_DAY = 1
IGNORED_PROPOSAL_BREAKER = 3

REASON_COMPANY_BUDGET = "company-budget"
REASON_SUBMISSION_BUDGET = "submission-budget"
REASON_PROPOSAL_BUDGET = "proposal-budget"
REASON_PROPOSAL_MESSAGE_BUDGET = "proposal-message-budget"
REASON_IGNORED_BREAKER = "ignored-proposal-breaker"


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: str = ""
    detail: str = ""

    def __bool__(self) -> bool:  # `if limits.may_submit(...)` reads naturally
        return self.allowed


_OK = LimitDecision(True)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def may_create_application(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    posting: JobPosting,
    now: datetime | None = None,
) -> LimitDecision:
    """The per-company budget. The per-posting budget is the unique constraint.

    Eight applications to one company in a week makes the candidate look
    indiscriminate, and ATS dedupe often flags them as one person spamming.
    """
    company = (posting.company_normalized or "").strip()
    if not company:
        return _OK
    now = now or utcnow()
    since = now - timedelta(days=30)
    count = (
        db.scalar(
            select(func.count())
            .select_from(Application)
            .join(JobPosting, JobPosting.id == Application.job_posting_id)
            .where(
                Application.org_id == org_id,
                Application.candidate_id == candidate_id,
                JobPosting.company_normalized == company,
                Application.created_at >= since,
            )
        )
        or 0
    )
    if count >= APPLICATIONS_PER_COMPANY_PER_30D:
        return LimitDecision(
            False,
            REASON_COMPANY_BUDGET,
            f"{count} applications to {posting.company or company} in the last 30 days",
        )
    return _OK


def may_submit(
    db: Session, *, application: Application, now: datetime | None = None
) -> LimitDecision:
    """Five submissions per rolling week. Above that we are not representing
    somebody, we are spraying."""
    now = now or utcnow()
    since = now - timedelta(days=7)
    count = (
        db.scalar(
            select(func.count())
            .select_from(Application)
            .where(
                Application.org_id == application.org_id,
                Application.candidate_id == application.candidate_id,
                Application.submitted_at >= since,
            )
        )
        or 0
    )
    if count >= SUBMISSIONS_PER_WEEK:
        return LimitDecision(
            False, REASON_SUBMISSION_BUDGET, f"{count} submissions in the last 7 days"
        )
    return _OK


def may_propose(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    count: int = 1,
    in_new_message: bool = True,
    now: datetime | None = None,
) -> LimitDecision:
    """May we put ``count`` more roles in front of this candidate?

    ``in_new_message=False`` is the portal feed case: the candidate is pulling
    cards themselves, so the one-proposal-message-a-day cap does not apply,
    but the weekly budget and the ignored breaker still do — a feed that
    manufactures forty proposals a day spends the same reputation a text does.
    """
    if count > MAX_PROPOSALS_PER_MESSAGE and in_new_message:
        return LimitDecision(
            False,
            REASON_PROPOSAL_BUDGET,
            f"{count} proposals in one message; the cap is {MAX_PROPOSALS_PER_MESSAGE}",
        )
    now = now or utcnow()

    week_ago = now - timedelta(days=7)
    proposed = (
        db.scalar(
            select(func.count())
            .select_from(JobProposal)
            .where(
                JobProposal.org_id == org_id,
                JobProposal.candidate_id == candidate_id,
                JobProposal.created_at >= week_ago,
            )
        )
        or 0
    )
    if proposed + count > PROPOSALS_PER_WEEK:
        return LimitDecision(
            False, REASON_PROPOSAL_BUDGET, f"{proposed} proposals in the last 7 days"
        )

    if in_new_message:
        day_ago = now - timedelta(hours=24)
        messages_today = (
            db.scalar(
                select(func.count(func.distinct(JobProposal.message_id)))
                .select_from(JobProposal)
                .where(
                    JobProposal.org_id == org_id,
                    JobProposal.candidate_id == candidate_id,
                    JobProposal.message_id.is_not(None),
                    JobProposal.created_at >= day_ago,
                )
            )
            or 0
        )
        if messages_today >= PROPOSAL_MESSAGES_PER_DAY:
            return LimitDecision(
                False,
                REASON_PROPOSAL_MESSAGE_BUDGET,
                "already sent a proposal message in the last 24 hours",
            )

    # The breaker: three consecutive proposals nobody answered. Silence is
    # information, and a fourth unanswered card is how threads die.
    recent = list(
        db.scalars(
            select(JobProposal)
            .where(
                JobProposal.org_id == org_id,
                JobProposal.candidate_id == candidate_id,
            )
            .order_by(JobProposal.created_at.desc())
            .limit(IGNORED_PROPOSAL_BREAKER)
        ).all()
    )
    if len(recent) >= IGNORED_PROPOSAL_BREAKER and all(
        p.status == "proposed" and p.responded_at is None for p in recent
    ):
        return LimitDecision(
            False,
            REASON_IGNORED_BREAKER,
            f"last {IGNORED_PROPOSAL_BREAKER} proposals went unanswered",
        )
    return _OK
