"""Sequences, enrolments, sending and inbound replies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..deps import Principal, get_db, get_principal, require_write
from ..models import (
    Candidate,
    Enrollment,
    MailboxConnection,
    Message,
    Project,
    ReplyDraft,
    Sequence,
    SequenceStep,
)
from ..outreach.drafts import draft_sequence
from ..outreach.sequencer import (
    enroll_candidates,
    find_message_for_reply,
    preview_step,
    record_reply,
    run_due,
)
from ..schemas import (
    EnrollmentOut,
    EnrollRequest,
    GenerateSequenceRequest,
    GenerateSequenceResponse,
    InboundEmail,
    MessageOut,
    PreviewRequest,
    PreviewResponse,
    ReplyDraftOut,
    RunSequenceResponse,
    SequenceIn,
    SequenceOut,
    SequenceStepIn,
    SequenceUpdate,
)

router = APIRouter(prefix="/v1/sequences", tags=["outreach"])


def _load_sequence(db: Session, sequence_id: str, org_id: str) -> Sequence:
    sequence = db.scalar(
        select(Sequence)
        .where(Sequence.id == sequence_id, Sequence.org_id == org_id)
        .options(selectinload(Sequence.steps))
    )
    if sequence is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sequence not found")
    return sequence


def _step_from_payload(sequence_id: str, order: int, step: SequenceStepIn) -> SequenceStep:
    return SequenceStep(
        sequence_id=sequence_id,
        step_order=order,
        channel=step.channel,
        delay_days=step.delay_days,
        delay_unit=step.delay_unit,
        send_at_time=step.send_at_time,
        send_timezone=step.send_timezone,
        subject_template=step.subject_template,
        body_template=step.body_template,
    )


def _has_active_enrollments(db: Session, sequence_id: str) -> bool:
    return (
        db.scalar(
            select(Enrollment.id)
            .where(Enrollment.sequence_id == sequence_id, Enrollment.status == "active")
            .limit(1)
        )
        is not None
    )


@router.post("", response_model=SequenceOut, status_code=status.HTTP_201_CREATED)
def create_sequence(
    payload: SequenceIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    if payload.mailbox_id:
        mailbox = db.get(MailboxConnection, payload.mailbox_id)
        if mailbox is None or mailbox.org_id != principal.org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox connection not found"
            )
    sequence = Sequence(
        org_id=principal.org_id,
        project_id=payload.project_id,
        name=payload.name,
        personalize=payload.personalize,
        stop_on_reply=payload.stop_on_reply,
        from_email=payload.from_email,
        from_name=payload.from_name,
        mailbox_id=payload.mailbox_id,
    )
    db.add(sequence)
    db.flush()
    for order, step in enumerate(payload.steps):
        db.add(_step_from_payload(sequence.id, order, step))
    db.flush()
    db.refresh(sequence)
    return SequenceOut.model_validate(sequence)


@router.post("/generate", response_model=GenerateSequenceResponse)
def generate_sequence(
    payload: GenerateSequenceRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> GenerateSequenceResponse:
    """Draft a sequence from a brief. Returns an editable draft; persists nothing."""
    project = None
    if payload.project_id:
        project = db.scalar(
            select(Project).where(
                Project.id == payload.project_id, Project.org_id == principal.org_id
            )
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    draft, generated_by = draft_sequence(
        brief=payload.prompt, project=project, num_steps=payload.num_steps
    )
    draft["project_id"] = payload.project_id
    return GenerateSequenceResponse(
        draft=SequenceIn.model_validate(draft), generated_by=generated_by
    )


@router.get("", response_model=list[SequenceOut])
def list_sequences(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[SequenceOut]:
    rows = db.scalars(
        select(Sequence)
        .where(Sequence.org_id == principal.org_id)
        .options(selectinload(Sequence.steps))
        .order_by(Sequence.created_at.desc())
    ).all()
    return [SequenceOut.model_validate(row) for row in rows]


@router.get("/{sequence_id}", response_model=SequenceOut)
def get_sequence(
    sequence_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SequenceOut:
    return SequenceOut.model_validate(_load_sequence(db, sequence_id, principal.org_id))


@router.patch("/{sequence_id}", response_model=SequenceOut)
def update_sequence(
    sequence_id: str,
    payload: SequenceUpdate,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    updates = payload.model_dump(exclude_unset=True)
    if "project_id" in updates and updates["project_id"] is not None:
        project = db.scalar(
            select(Project).where(
                Project.id == updates["project_id"], Project.org_id == principal.org_id
            )
        )
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if updates.get("mailbox_id") is not None:
        mailbox = db.get(MailboxConnection, updates["mailbox_id"])
        if mailbox is None or mailbox.org_id != principal.org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox connection not found"
            )
    for attr, value in updates.items():
        setattr(sequence, attr, value)
    db.flush()
    return SequenceOut.model_validate(sequence)


@router.put("/{sequence_id}/steps", response_model=SequenceOut)
def replace_steps(
    sequence_id: str,
    payload: list[SequenceStepIn],
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    """Replace the full step list. Steps index into live enrolments via
    ``current_step``, so editing is only allowed while nothing is mid-flight."""
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A sequence needs at least one step",
        )
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    if sequence.status == "active" and _has_active_enrollments(db, sequence.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pause the sequence before editing steps — enrolments are mid-flight",
        )
    for step in list(sequence.steps):
        db.delete(step)
    db.flush()
    for order, step in enumerate(payload):
        db.add(_step_from_payload(sequence.id, order, step))
    db.flush()
    db.refresh(sequence)
    return SequenceOut.model_validate(sequence)


@router.post("/{sequence_id}/clone", response_model=SequenceOut, status_code=status.HTTP_201_CREATED)
def clone_sequence(
    sequence_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    """Copy a sequence and its steps. The copy starts paused."""
    source = _load_sequence(db, sequence_id, principal.org_id)
    copy = Sequence(
        org_id=source.org_id,
        project_id=source.project_id,
        name=f"{source.name} (copy)"[:200],
        status="paused",
        personalize=source.personalize,
        stop_on_reply=source.stop_on_reply,
        from_email=source.from_email,
        from_name=source.from_name,
    )
    db.add(copy)
    db.flush()
    for step in sorted(source.steps, key=lambda s: s.step_order):
        db.add(
            _step_from_payload(
                copy.id,
                step.step_order,
                SequenceStepIn(
                    channel=step.channel,
                    delay_days=step.delay_days,
                    delay_unit=step.delay_unit,
                    send_at_time=step.send_at_time,
                    send_timezone=step.send_timezone,
                    subject_template=step.subject_template,
                    body_template=step.body_template,
                ),
            )
        )
    db.flush()
    db.refresh(copy)
    return SequenceOut.model_validate(copy)


@router.patch("/{sequence_id}/status", response_model=SequenceOut)
def set_status(
    sequence_id: str,
    value: str = Query(pattern="^(active|paused|archived)$"),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    sequence.status = value
    db.flush()
    return SequenceOut.model_validate(sequence)


@router.post("/{sequence_id}/preview", response_model=PreviewResponse)
def preview(
    sequence_id: str,
    payload: PreviewRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> PreviewResponse:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == payload.candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    subject, body, context = preview_step(
        db, sequence=sequence, candidate=candidate, step_order=payload.step_order
    )
    return PreviewResponse(subject=subject, body=body, personalization=context)


@router.post("/{sequence_id}/enroll", response_model=list[EnrollmentOut])
def enroll(
    sequence_id: str,
    payload: EnrollRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> list[EnrollmentOut]:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    enrollments = enroll_candidates(
        db,
        sequence=sequence,
        candidate_ids=payload.candidate_ids,
        project_id=payload.project_id,
        start_immediately=payload.start_immediately,
    )
    return [EnrollmentOut.model_validate(e) for e in enrollments]


@router.get("/{sequence_id}/enrollments", response_model=list[EnrollmentOut])
def list_enrollments(
    sequence_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[EnrollmentOut]:
    _load_sequence(db, sequence_id, principal.org_id)
    rows = db.scalars(
        select(Enrollment)
        .where(Enrollment.sequence_id == sequence_id)
        .order_by(Enrollment.created_at.desc())
    ).all()
    return [EnrollmentOut.model_validate(row) for row in rows]


@router.post("/{sequence_id}/run", response_model=RunSequenceResponse)
def run_sequence(
    sequence_id: str,
    dry_run: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=1000),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> RunSequenceResponse:
    _load_sequence(db, sequence_id, principal.org_id)
    summary = run_due(
        db,
        org_id=principal.org_id,
        sequence_id=sequence_id,
        limit=limit,
        dry_run=dry_run,
    )
    return RunSequenceResponse(
        processed=summary.processed,
        sent=summary.sent,
        failed=summary.failed,
        completed=summary.completed,
        skipped=summary.skipped,
        messages=[MessageOut.model_validate(m) for m in summary.messages],
    )


# --------------------------------------------------------------------------
# messages + inbound
# --------------------------------------------------------------------------

messages_router = APIRouter(prefix="/v1/messages", tags=["outreach"])


@messages_router.get("", response_model=list[MessageOut])
def list_messages(
    candidate_id: str | None = None,
    direction: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MessageOut]:
    stmt = select(Message).where(Message.org_id == principal.org_id)
    if candidate_id:
        stmt = stmt.where(Message.candidate_id == candidate_id)
    if direction:
        stmt = stmt.where(Message.direction == direction)
    rows = db.scalars(stmt.order_by(Message.created_at.desc()).limit(limit)).all()
    return [MessageOut.model_validate(row) for row in rows]


reply_drafts_router = APIRouter(prefix="/v1/reply-drafts", tags=["outreach"])


@reply_drafts_router.get("", response_model=list[ReplyDraftOut])
def list_reply_drafts(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReplyDraftOut]:
    stmt = select(ReplyDraft).where(ReplyDraft.org_id == principal.org_id)
    if status_filter:
        stmt = stmt.where(ReplyDraft.status == status_filter)
    rows = db.scalars(stmt.order_by(ReplyDraft.created_at.desc()).limit(limit)).all()
    return [ReplyDraftOut.model_validate(row) for row in rows]


webhooks_router = APIRouter(prefix="/v1/webhooks", tags=["outreach"])


@webhooks_router.post("/inbound-email")
def inbound_email(
    payload: InboundEmail,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    """Point your ESP's inbound-parse webhook here (authenticate with an API key)."""
    sender = (payload.candidate_email or payload.from_address or "").strip().lower()
    message = find_message_for_reply(
        db,
        org_id=principal.org_id,
        candidate_email=sender,
        provider_message_id=payload.in_reply_to,
    )
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No outbound message matched this reply",
        )
    verdict = record_reply(db, message=message, subject=payload.subject, body=payload.body)
    return {
        "message_id": message.id,
        "candidate_id": message.candidate_id,
        "intent": verdict.intent,
        "confidence": verdict.confidence,
        "stopped_sequence": verdict.should_stop_sequence,
        "classified_by": verdict.classified_by,
        "summary": verdict.summary,
    }
