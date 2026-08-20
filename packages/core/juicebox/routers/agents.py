"""Sourcing agents — saved searches that keep a pipeline warm."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.sourcing import run_agent, run_due_agents
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import Project, SourcingAgent
from ..routers.search import _summary_from_doc
from ..schemas import AgentRunResult, SourcingAgentIn, SourcingAgentOut
from ..search.index import candidate_to_doc

router = APIRouter(route_class=CommittingRoute, prefix="/v1/agents", tags=["agents"])


def _load_agent(db: Session, agent_id: str, org_id: str) -> SourcingAgent:
    agent = db.scalar(
        select(SourcingAgent).where(
            SourcingAgent.id == agent_id, SourcingAgent.org_id == org_id
        )
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


@router.post("", response_model=SourcingAgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: SourcingAgentIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SourcingAgentOut:
    project = db.scalar(
        select(Project).where(
            Project.id == payload.project_id, Project.org_id == principal.org_id
        )
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    agent = SourcingAgent(
        org_id=principal.org_id,
        project_id=payload.project_id,
        name=payload.name,
        query=payload.query,
        overrides=(payload.overrides.model_dump(exclude_none=True) if payload.overrides else {}),
        min_score=payload.min_score,
        max_per_run=payload.max_per_run,
        cadence_minutes=payload.cadence_minutes,
        auto_enroll_sequence_id=payload.auto_enroll_sequence_id,
    )
    db.add(agent)
    db.flush()
    return SourcingAgentOut.model_validate(agent)


@router.get("", response_model=list[SourcingAgentOut])
def list_agents(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[SourcingAgentOut]:
    rows = db.scalars(
        select(SourcingAgent)
        .where(SourcingAgent.org_id == principal.org_id)
        .order_by(SourcingAgent.created_at.desc())
    ).all()
    return [SourcingAgentOut.model_validate(row) for row in rows]


@router.patch("/{agent_id}/status", response_model=SourcingAgentOut)
def set_status(
    agent_id: str,
    value: str = Query(pattern="^(active|paused)$"),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SourcingAgentOut:
    agent = _load_agent(db, agent_id, principal.org_id)
    agent.status = value
    db.flush()
    return SourcingAgentOut.model_validate(agent)


@router.post("/{agent_id}/run", response_model=AgentRunResult)
def run(
    agent_id: str,
    dry_run: bool = Query(default=False),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> AgentRunResult:
    agent = _load_agent(db, agent_id, principal.org_id)
    result = run_agent(db, agent, dry_run=dry_run)
    return AgentRunResult(
        agent_id=result.agent_id,
        evaluated=result.evaluated,
        added=result.added,
        enrolled=result.enrolled,
        candidates=[_summary_from_doc(candidate_to_doc(c)) for c in result.candidates],
    )


@router.post("/run-due", response_model=list[AgentRunResult])
def run_all_due(
    principal: Principal = Depends(require_write), db: Session = Depends(get_db)
) -> list[AgentRunResult]:
    runs = run_due_agents(db, org_id=principal.org_id)
    return [
        AgentRunResult(
            agent_id=r.agent_id,
            evaluated=r.evaluated,
            added=r.added,
            enrolled=r.enrolled,
            candidates=[
                _summary_from_doc(candidate_to_doc(c)) for c in r.candidates
            ],
        )
        for r in runs
    ]


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(
    agent_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    agent = _load_agent(db, agent_id, principal.org_id)
    db.delete(agent)
