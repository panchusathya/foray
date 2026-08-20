"""Projects (roles) and their candidate pipelines."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import (
    OUTREACH_DRIVEN_STAGES,
    PIPELINE_STAGES,
    Candidate,
    Project,
    ProjectCandidate,
    SearchResult,
    SourcingAgent,
    record_event,
)
from ..schemas import (
    AddToProjectRequest,
    CandidateSummary,
    PipelineEntryOut,
    ProjectIn,
    ProjectOut,
    StageUpdate,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/projects", tags=["projects"])


def _load_project(db: Session, project_id: str, org_id: str) -> Project:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.org_id == org_id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = Project(
        org_id=principal.org_id,
        created_by=principal.user_id,
        **payload.model_dump(),
    )
    db.add(project)
    db.flush()
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[ProjectOut]:
    rows = db.scalars(
        select(Project)
        .where(Project.org_id == principal.org_id)
        .order_by(Project.created_at.desc())
    ).all()
    return [ProjectOut.model_validate(row) for row in rows]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ProjectOut:
    return ProjectOut.model_validate(_load_project(db, project_id, principal.org_id))


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    payload: ProjectIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = _load_project(db, project_id, principal.org_id)
    for key, value in payload.model_dump().items():
        setattr(project, key, value)
    db.flush()
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    """Remove a project. Pipeline cards and sourcing agents go with it;
    sequences keep their copy but lose the project link (role_title becomes empty)."""
    project = _load_project(db, project_id, principal.org_id)
    for agent in db.scalars(
        select(SourcingAgent).where(SourcingAgent.project_id == project_id)
    ).all():
        db.delete(agent)
    db.delete(project)


@router.post("/{project_id}/candidates", response_model=list[PipelineEntryOut])
def add_candidates(
    project_id: str,
    payload: AddToProjectRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> list[PipelineEntryOut]:
    if payload.stage not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"stage must be one of {list(PIPELINE_STAGES)}",
        )
    project = _load_project(db, project_id, principal.org_id)

    # Carry the search rationale across so the pipeline card shows *why*.
    rationales: dict[str, dict] = {}
    scores: dict[str, float] = {}
    if payload.search_id:
        for row in db.scalars(
            select(SearchResult).where(
                SearchResult.search_id == payload.search_id,
                SearchResult.candidate_id.in_(payload.candidate_ids),
            )
        ):
            rationales[row.candidate_id] = row.evidence or {}
            scores[row.candidate_id] = row.final_score

    existing = {
        row.candidate_id: row
        for row in db.scalars(
            select(ProjectCandidate).where(
                ProjectCandidate.project_id == project.id,
                ProjectCandidate.candidate_id.in_(payload.candidate_ids),
            )
        )
    }

    entries: list[ProjectCandidate] = []
    for candidate_id in payload.candidate_ids:
        if candidate_id in existing:
            entries.append(existing[candidate_id])
            continue
        candidate = db.scalar(
            select(Candidate).where(
                Candidate.id == candidate_id, Candidate.org_id == principal.org_id
            )
        )
        if candidate is None:
            continue
        entry = ProjectCandidate(
            project_id=project.id,
            candidate_id=candidate_id,
            stage=payload.stage,
            score=scores.get(candidate_id, 0.0),
            rationale=rationales.get(candidate_id, {}),
            added_by=principal.user_id,
        )
        db.add(entry)
        entries.append(entry)
        record_event(
            db,
            org_id=principal.org_id,
            type="pipeline.added",
            subject_type="candidate",
            subject_id=candidate_id,
            actor_id=principal.user_id,
            payload={"project_id": project.id, "stage": payload.stage},
        )
    db.flush()
    return [_entry_out(db, entry) for entry in entries]


def _entry_out(db: Session, entry: ProjectCandidate) -> PipelineEntryOut:
    candidate = entry.candidate or db.get(Candidate, entry.candidate_id)
    return PipelineEntryOut(
        id=entry.id,
        stage=entry.stage,
        score=entry.score,
        notes=entry.notes,
        rationale=entry.rationale or {},
        added_at=entry.created_at,
        candidate=CandidateSummary.model_validate(candidate),
    )


@router.get("/{project_id}/pipeline", response_model=list[PipelineEntryOut])
def get_pipeline(
    project_id: str,
    stage: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[PipelineEntryOut]:
    _load_project(db, project_id, principal.org_id)
    stmt = (
        select(ProjectCandidate)
        .where(ProjectCandidate.project_id == project_id)
        .options(selectinload(ProjectCandidate.candidate))
        .order_by(ProjectCandidate.score.desc())
    )
    if stage:
        stmt = stmt.where(ProjectCandidate.stage == stage)
    return [_entry_out(db, entry) for entry in db.scalars(stmt)]


@router.patch("/{project_id}/pipeline/{entry_id}", response_model=PipelineEntryOut)
def update_stage(
    project_id: str,
    entry_id: str,
    payload: StageUpdate,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> PipelineEntryOut:
    if payload.stage not in PIPELINE_STAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"stage must be one of {list(PIPELINE_STAGES)}",
        )
    _load_project(db, project_id, principal.org_id)
    entry = db.scalar(
        select(ProjectCandidate).where(
            ProjectCandidate.id == entry_id, ProjectCandidate.project_id == project_id
        )
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline entry not found"
        )
    if (
        payload.stage in OUTREACH_DRIVEN_STAGES
        and payload.stage != entry.stage
        and not payload.override
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{payload.stage}' is set automatically when outreach sends or a reply "
                "lands. Pass override=true to record a manual touch (e.g. you reached out "
                "yourself)."
            ),
        )
    previous = entry.stage
    entry.stage = payload.stage
    if payload.notes is not None:
        entry.notes = payload.notes
    record_event(
        db,
        org_id=principal.org_id,
        type="pipeline.stage_changed",
        subject_type="candidate",
        subject_id=entry.candidate_id,
        actor_id=principal.user_id,
        payload={"from": previous, "to": payload.stage, "project_id": project_id},
    )
    db.flush()
    return _entry_out(db, entry)


@router.delete(
    "/{project_id}/pipeline/{entry_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_from_pipeline(
    project_id: str,
    entry_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    _load_project(db, project_id, principal.org_id)
    entry = db.scalar(
        select(ProjectCandidate).where(
            ProjectCandidate.id == entry_id, ProjectCandidate.project_id == project_id
        )
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline entry not found"
        )
    db.delete(entry)
