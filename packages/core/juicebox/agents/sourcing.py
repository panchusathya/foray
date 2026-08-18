"""Sourcing agents: a saved search that runs on a cadence and fills a pipeline.

Each run re-executes the search, drops anyone already in the project, and adds
the top new matches above a score threshold — optionally enrolling them straight
into an outreach sequence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Candidate,
    ProjectCandidate,
    Sequence,
    SourcingAgent,
    record_event,
    utcnow,
)
from ..outreach.sequencer import enroll_candidates
from ..search.engine import get_engine

logger = logging.getLogger(__name__)


@dataclass
class AgentRun:
    agent_id: str
    evaluated: int = 0
    added: int = 0
    enrolled: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    skipped_reason: str = ""


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_due(agent: SourcingAgent, now: datetime | None = None) -> bool:
    if agent.status != "active":
        return False
    now = now or utcnow()
    last_run = _aware(agent.last_run_at)
    if last_run is None:
        return True
    return now - last_run >= timedelta(minutes=max(agent.cadence_minutes, 1))


def run_agent(
    db: Session,
    agent: SourcingAgent,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> AgentRun:
    now = now or utcnow()
    run = AgentRun(agent_id=agent.id)

    outcome = get_engine().search(
        db,
        org_id=agent.org_id,
        query=agent.query,
        limit=max(agent.max_per_run * 3, 20),
        overrides=agent.overrides or None,
        project_id=agent.project_id,
        persist=not dry_run,
        created_by=None,
    )
    run.evaluated = len(outcome.results)

    existing = {
        row
        for row in db.scalars(
            select(ProjectCandidate.candidate_id).where(
                ProjectCandidate.project_id == agent.project_id
            )
        )
    }

    added_ids: list[str] = []
    for hit in outcome.results:
        if len(added_ids) >= agent.max_per_run:
            break
        if hit.doc.id in existing:
            continue
        if hit.final_score < agent.min_score:
            continue
        candidate = db.get(Candidate, hit.doc.id)
        if candidate is None:
            continue
        run.candidates.append(candidate)
        added_ids.append(candidate.id)
        if dry_run:
            continue
        db.add(
            ProjectCandidate(
                project_id=agent.project_id,
                candidate_id=candidate.id,
                stage="sourced",
                score=hit.final_score,
                rationale=hit.evidence_payload(),
                added_by=agent.id,
            )
        )

    run.added = len(added_ids)

    if not dry_run:
        if added_ids and agent.auto_enroll_sequence_id:
            sequence = db.get(Sequence, agent.auto_enroll_sequence_id)
            if sequence is not None and sequence.org_id == agent.org_id:
                enrollments = enroll_candidates(
                    db,
                    sequence=sequence,
                    candidate_ids=added_ids,
                    project_id=agent.project_id,
                    start_immediately=True,
                    now=now,
                )
                run.enrolled = len(enrollments)

        agent.last_run_at = now
        agent.total_added += run.added
        record_event(
            db,
            org_id=agent.org_id,
            type="agent.run",
            subject_type="sourcing_agent",
            subject_id=agent.id,
            payload={
                "evaluated": run.evaluated,
                "added": run.added,
                "enrolled": run.enrolled,
            },
        )
        db.flush()

    return run


def run_due_agents(db: Session, *, org_id: str, now: datetime | None = None) -> list[AgentRun]:
    now = now or utcnow()
    agents = db.scalars(
        select(SourcingAgent).where(
            SourcingAgent.org_id == org_id, SourcingAgent.status == "active"
        )
    ).all()
    return [run_agent(db, agent, now=now) for agent in agents if is_due(agent, now)]
