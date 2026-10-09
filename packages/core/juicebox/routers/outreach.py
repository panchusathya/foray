"""Sequences, enrolments, sending and inbound replies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..llm.client import LLMUnavailable
from ..models import (
    Candidate,
    Enrollment,
    MailboxConnection,
    Message,
    Project,
    ReplyDraft,
    Sequence,
    SequenceStep,
    utcnow,
)
from ..outreach import templates
from ..outreach.drafts import draft_sequence
from ..outreach.outbox import (
    cancel_in_flight,
    cancelled_count,
    list_in_flight,
    reenroll_in_flight,
)
from ..outreach.personalize import personalize, sender_context
from ..outreach.sender import preview_connection
from ..outreach.sequencer import (
    enroll_candidates,
    find_message_for_reply,
    move_in_line,
    preview_step,
    preview_step_view,
    process_enrollment,
    recent_human_replies,
    record_reply,
    reply_subject,
    run_due,
    suggested_date_context,
)
from ..outreach.spamcheck import personalization_depth, score_email, score_template
from ..schemas import (
    CloneSequenceRequest,
    EnrollmentOut,
    EnrollRequest,
    EnrollResponse,
    EnrollSkipOut,
    GenerateSequenceRequest,
    GenerateSequenceResponse,
    InboundEmail,
    InFlightCancelIn,
    InFlightCancelOut,
    InFlightList,
    InFlightMoveIn,
    InFlightMoveOut,
    InFlightReenrollIn,
    InFlightReenrollOut,
    InFlightRow,
    MessageOut,
    PreviewRequest,
    PreviewResponse,
    ReplyDraftOut,
    RunSequenceResponse,
    SequenceIn,
    SequenceOut,
    SequenceStepIn,
    SequenceUpdate,
    SpamCheckRequest,
    SpamCheckResponse,
    SpamReportOut,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/sequences", tags=["outreach"])

# Upper bound on synchronous sends inside one enroll request. Anything past
# this is due-now anyway, and the worker tick delivers it within a minute —
# the cap keeps the request comfortably inside the proxy timeout.
MAX_SEND_NOW = 25


def _load_sequence(db: Session, sequence_id: str, org_id: str) -> Sequence:
    sequence = db.scalar(
        select(Sequence)
        .where(Sequence.id == sequence_id, Sequence.org_id == org_id)
        .options(selectinload(Sequence.steps))
    )
    if sequence is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sequence not found")
    return sequence


def _load_project(db: Session, project_id: str | None, org_id: str) -> Project | None:
    """Resolve a project id inside the caller's org, or 404. Falsy id -> None.

    The FK only asks that the project row *exist*, so an id from another org
    would be pinned to a sequence or an enrolment and then read back out
    through /preview, which renders that org's role_title, project_name and
    location. Every route that accepts a project_id goes through here rather
    than repeating the query -- there were four copies, and clone had none.
    """
    if not project_id:
        return None
    project = db.scalar(select(Project).where(Project.id == project_id, Project.org_id == org_id))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


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


def _resolve_sender_mode(requested: str | None, mailbox_id: str | None) -> str:
    """What a payload means when it names a mailbox but not a mode.

    The column defaults to "rotate", so a client that sets mailbox_id and says
    nothing else would have its choice silently ignored by the pool. Naming a
    mailbox is taken as pinning to it; an explicit sender_mode always wins.
    """
    if requested:
        return requested
    return "fixed" if mailbox_id else "rotate"


def _checked_mailbox_ids(db: Session, org_id: str, mailbox_ids: list[str]) -> list[str]:
    """Validate a rotation subset, preserving order and dropping duplicates.

    Checked against the org for the same reason ``mailbox_id`` is: the column
    is JSON and carries no foreign key, so an id from another org would sit
    there unnoticed until a send tried to use it.
    """
    checked: list[str] = []
    for mailbox_id in mailbox_ids:
        if mailbox_id in checked:
            continue
        mailbox = db.get(MailboxConnection, mailbox_id)
        if mailbox is None or mailbox.org_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Mailbox connection not found: {mailbox_id}",
            )
        checked.append(mailbox_id)
    return checked


@router.post("", response_model=SequenceOut, status_code=status.HTTP_201_CREATED)
def create_sequence(
    payload: SequenceIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    _load_project(db, payload.project_id, principal.org_id)
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
        track_opens=payload.track_opens,
        track_clicks=payload.track_clicks,
        from_email=payload.from_email,
        from_name=payload.from_name,
        sender_mode=_resolve_sender_mode(payload.sender_mode, payload.mailbox_id),
        mailbox_id=payload.mailbox_id,
        mailbox_ids=_checked_mailbox_ids(db, principal.org_id, payload.mailbox_ids),
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
    project = _load_project(db, payload.project_id, principal.org_id)
    draft, generated_by = draft_sequence(
        brief=payload.prompt, project=project, num_steps=payload.num_steps
    )
    draft["project_id"] = payload.project_id
    return GenerateSequenceResponse(
        draft=SequenceIn.model_validate(draft), generated_by=generated_by
    )


@router.post("/spam-check", response_model=SpamCheckResponse)
def spam_check(
    payload: SpamCheckRequest,
    principal: Principal = Depends(get_principal),
) -> SpamCheckResponse:
    """Spam-risk of each step as written, so the editor can score unsaved copy.

    Pure: no sequence, candidate or database is read, so it runs on every
    keystroke pause. Variables are filled with sample values.
    """
    del principal  # authentication only
    return SpamCheckResponse(
        results=[
            SpamReportOut.model_validate(
                score_template(
                    step.subject_template,
                    step.body_template,
                    is_follow_up=index > 0,
                    open_tracking=payload.track_opens,
                    click_tracking=payload.track_clicks,
                ).to_dict()
            )
            for index, step in enumerate(payload.steps)
        ]
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
    if "project_id" in updates:
        _load_project(db, updates["project_id"], principal.org_id)
    if updates.get("mailbox_id") is not None:
        mailbox = db.get(MailboxConnection, updates["mailbox_id"])
        if mailbox is None or mailbox.org_id != principal.org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox connection not found"
            )
    if updates.get("mailbox_ids") is not None:
        updates["mailbox_ids"] = _checked_mailbox_ids(
            db, principal.org_id, updates["mailbox_ids"]
        )
    elif "mailbox_ids" in updates:
        # Explicit null reads the same as an empty list: back to the whole pool.
        updates["mailbox_ids"] = []
    if "mailbox_id" in updates and "sender_mode" not in updates:
        # Same reasoning as on create: setting a mailbox means pinning to it,
        # clearing it means going back to the pool.
        updates["sender_mode"] = "fixed" if updates["mailbox_id"] else "rotate"
    if updates.get("sender_mode") == "fixed" and not (
        updates.get("mailbox_id") or sequence.mailbox_id
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sender_mode 'fixed' needs a mailbox_id to pin to",
        )
    for attr, value in updates.items():
        setattr(sequence, attr, value)
    db.flush()
    return SequenceOut.model_validate(sequence)


@router.delete("/{sequence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sequence(
    sequence_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    """Remove a sequence and its enrollments/messages. Agents that auto-enroll
    into it keep running but lose the auto-enroll link."""
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    for enrollment in db.scalars(
        select(Enrollment).where(Enrollment.sequence_id == sequence_id)
    ).all():
        db.delete(enrollment)
    db.delete(sequence)


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
    payload: CloneSequenceRequest | None = None,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> SequenceOut:
    """Copy a sequence and its steps into a project. The copy starts paused.

    The project is the caller's to choose, and defaults to the source's. It
    used to be the source's unconditionally, with no way to say otherwise --
    so cloning another project's sequence as a starting point silently filed
    the copy under *that* project, where the Sequences list (which filters on
    project) could not show it to the recruiter who made it.
    """
    source = _load_sequence(db, sequence_id, principal.org_id)
    # None means "keep the source's", not "clear it": a copy with no project
    # is filtered out of every project view and reads as lost. Detaching one
    # is a deliberate PATCH.
    project_id = source.project_id
    name = f"{source.name} (copy)"
    if payload is not None:
        if payload.project_id:
            _load_project(db, payload.project_id, principal.org_id)
            project_id = payload.project_id
        if payload.name:
            name = payload.name
    copy = Sequence(
        org_id=source.org_id,
        project_id=project_id,
        name=name[:200],
        status="paused",
        personalize=source.personalize,
        stop_on_reply=source.stop_on_reply,
        track_opens=source.track_opens,
        track_clicks=source.track_clicks,
        from_email=source.from_email,
        from_name=source.from_name,
        # Without these the copy falls back to the pool, so a cloned sequence
        # would quietly send from a different address than the one it was
        # cloned from.
        sender_mode=source.sender_mode,
        mailbox_id=source.mailbox_id,
        mailbox_ids=list(source.mailbox_ids or []),
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
    enrollment = None
    if payload.enrollment_id:
        enrollment = db.scalar(
            select(Enrollment).where(
                Enrollment.id == payload.enrollment_id,
                Enrollment.sequence_id == sequence.id,
            )
        )
    connection = None
    if payload.mailbox_id:
        connection = db.get(MailboxConnection, payload.mailbox_id)
        if connection is None or connection.org_id != principal.org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found")
    draft = None
    if payload.subject_template is not None or payload.body_template is not None:
        draft = {"subject_template": payload.subject_template, "body_template": payload.body_template}
    ai_status = "off"
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    saved = next((s for s in steps if s.step_order == payload.step_order), steps[0] if steps else None)
    # Everything below reads the step as the editor has it, unsaved text and all.
    step = preview_step_view(saved, draft) if saved is not None else None
    has_directives = bool(
        step
        and templates.used_directives(f"{step.subject_template or ''}\n{step.body_template or ''}")
    )
    # Read off the step, not off the rendered body: a rendered value may legally
    # contain braces, and the templates are the only place a recruiter can fix.
    warnings = (
        templates.malformed_placeholders(step.subject_template or "")
        + templates.malformed_placeholders(step.body_template or "")
        if step
        else []
    )
    def marked() -> tuple[str, str, dict]:
        """The step rendered with no model at all: directives as visible
        markers, model-written variables at their plain fallbacks."""
        context = personalize(candidate, None, use_llm=False)
        # Rebuilt from scratch, so re-add the sender and date variables or the
        # preview signs off with nothing.
        context = {
            **context,
            **sender_context(
                connection or preview_connection(db, sequence, enrollment),
                sequence,
                fallback_name=get_settings().outreach_from_name,
            ),
            **suggested_date_context(db, enrollment=enrollment, step=step, now=utcnow()),
        }
        # Follow-ups take the first step's line here too, or the preview would
        # show a blank subject for every step whose own box the editor no
        # longer offers.
        is_follow_up = bool(steps) and step.step_order != steps[0].step_order
        subject_source = steps[0] if is_follow_up else step
        subject = templates.render(
            templates.mark_directives(subject_source.subject_template or ""), context
        ).strip()
        if is_follow_up:
            subject = reply_subject(subject)
        body = templates.render(templates.mark_directives(step.body_template or ""), context).strip()
        return subject, body, context

    if payload.fast:
        # The editor's own refreshes: never a model call, so typing and
        # clicking around cost nothing. The Preview button runs the full path.
        subject, body, context = marked()
        ai_status = "skipped" if has_directives else "off"
    else:
        try:
            subject, body, context = preview_step(
                db,
                sequence=sequence,
                candidate=candidate,
                step_order=payload.step_order,
                enrollment=enrollment,
                connection=connection,
                draft=draft,
            )
            if has_directives:
                ai_status = "resolved"
        except LLMUnavailable:
            # A preview must inform, not fail: show the directive as a visible
            # marker so the recruiter sees where AI text will go once a model is
            # available. Sending still fails closed — this leniency is preview-only.
            subject, body, context = marked()
            ai_status = "unavailable"
    # The context carries whichever mailbox preview_step rendered as, so the
    # response can name it rather than leaving the recruiter to trust that the
    # signature in front of them is the one the candidate will get.
    return PreviewResponse(
        subject=subject,
        body=templates.sanitize_preview(body),
        personalization=context,
        ai_status=ai_status,
        sender_email=context.get("sender_email", ""),
        sender_name=context.get("sender_name", ""),
        warnings=list(dict.fromkeys(warnings)),
        spam=SpamReportOut.model_validate(
            score_email(
                subject=subject,
                body=body,
                is_follow_up=bool(step and step.step_order > 0),
                open_tracking=sequence.track_opens,
                click_tracking=sequence.track_clicks,
                personalization=personalization_depth(
                    f"{step.subject_template or ''}\n{step.body_template or ''}"
                )
                if step
                else None,
            ).to_dict()
        ),
    )


@router.post("/{sequence_id}/enroll", response_model=EnrollResponse)
def enroll(
    sequence_id: str,
    payload: EnrollRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> EnrollResponse:
    sequence = _load_sequence(db, sequence_id, principal.org_id)
    _load_project(db, payload.project_id, principal.org_id)

    # People who answered us by hand recently are in a conversation, not a
    # list. They are reported back rather than silently dropped, and `force`
    # is the recruiter saying "I know, enrol them anyway".
    quiet_days = get_settings().outreach_reply_quiet_days
    now = utcnow()
    blocked = (
        {}
        if payload.force
        else recent_human_replies(
            db,
            org_id=principal.org_id,
            candidate_ids=payload.candidate_ids,
            now=now,
            days=quiet_days,
        )
    )
    skipped: list[EnrollSkipOut] = []
    if blocked:
        names = {
            row.id: row.full_name
            for row in db.scalars(select(Candidate).where(Candidate.id.in_(list(blocked))))
        }
        skipped = [
            EnrollSkipOut(
                candidate_id=candidate_id,
                full_name=names.get(candidate_id, ""),
                reason="replied_recently",
                replied_at=hit.sent_at,
                reply_intent=hit.reply_intent,
                quiet_days=quiet_days,
            )
            for candidate_id, hit in blocked.items()
        ]
    eligible = [cid for cid in payload.candidate_ids if cid not in blocked]
    enrollments = (
        enroll_candidates(
            db,
            sequence=sequence,
            candidate_ids=eligible,
            project_id=payload.project_id,
            start_immediately=payload.start_immediately,
            send_now=payload.send_now,
            now=now,
            skip_recent_repliers=False,
        )
        if eligible
        else []
    )
    if payload.send_now and sequence.status == "active":
        # Send here, in this request, to exactly the people just enrolled —
        # bounded work, one round trip, and the response rows reflect the
        # post-send state (current_step advanced, next_send_at = the follow-up).
        # The UI used to fire a second unbounded /run covering the WHOLE
        # sequence, which could outlive the proxy timeout and leave the button
        # stuck on "Enrolling…" while mail actually went out.
        for enrollment in enrollments[:MAX_SEND_NOW]:
            if enrollment.status == "active" and enrollment.current_step == 0:
                process_enrollment(db, enrollment, now=now)
        db.flush()
    return EnrollResponse(
        enrollments=[EnrollmentOut.model_validate(e) for e in enrollments],
        skipped=skipped,
    )


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
        .options(selectinload(Enrollment.candidate))
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
# in-flight / outbox
# --------------------------------------------------------------------------

in_flight_router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/outreach", tags=["outreach"]
)


@in_flight_router.get("/in-flight", response_model=InFlightList)
def list_in_flight_rows(
    state: str | None = Query(default=None, pattern="^(sending|due|held|scheduled|cancelled)$"),
    # Repeat it to view several sequences at once; omit it for all of them.
    sequence_id: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    # 0-based: the step each row is about to send.
    step: int | None = Query(default=None, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> InFlightList:
    total, rows, step_counts = list_in_flight(
        db,
        org_id=principal.org_id,
        state=state,
        sequence_id=sequence_id,
        limit=limit,
        offset=offset,
        step=step,
    )
    return InFlightList(
        total=total,
        rows=[InFlightRow.model_validate(row) for row in rows],
        cancelled_total=cancelled_count(db, org_id=principal.org_id, sequence_id=sequence_id),
        step_counts=step_counts,
    )


@in_flight_router.post("/in-flight/move", response_model=InFlightMoveOut)
def move_in_flight_rows(
    payload: InFlightMoveIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> InFlightMoveOut:
    """Change who goes out next: move rows up, down, to the top or bottom."""
    moved = move_in_line(
        db,
        principal.org_id,
        enrollment_ids=payload.enrollment_ids,
        to=payload.to,
        position=payload.position,
        now=utcnow(),
    )
    return InFlightMoveOut(moved=moved)


@in_flight_router.post("/in-flight/cancel", response_model=InFlightCancelOut)
def cancel_in_flight_rows(
    payload: InFlightCancelIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> InFlightCancelOut:
    result = cancel_in_flight(
        db,
        org_id=principal.org_id,
        enrollment_ids=payload.enrollment_ids,
        action=payload.action,
    )
    return InFlightCancelOut(
        skipped=result.skipped,
        stopped=result.stopped,
        already_sending=result.already_sending,
        not_found=result.not_found,
    )


@in_flight_router.post("/in-flight/reenroll", response_model=InFlightReenrollOut)
def reenroll_in_flight_rows(
    payload: InFlightReenrollIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> InFlightReenrollOut:
    """Undo a stop: put cancelled rows back at their stopped step, at a new time."""
    if payload.when == "at" and payload.send_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="send_at is required when when is 'at'",
        )
    now = utcnow()
    result = reenroll_in_flight(
        db,
        org_id=principal.org_id,
        enrollment_ids=payload.enrollment_ids,
        when=payload.when,
        send_at=payload.send_at,
        force=payload.force,
        now=now,
    )
    sent = 0
    if payload.when == "now":
        # Same bargain as /enroll with send_now: deliver here, bounded, so the
        # response reflects the post-send state and the row leaves the outbox
        # before the page reloads. A held or capped send simply stays due and
        # the worker picks it up.
        for enrollment in result.enrollments[:MAX_SEND_NOW]:
            outcome, message = process_enrollment(db, enrollment, now=now)
            if (
                message is not None
                and message.status != "failed"
                and outcome in ("sent", "completed")
            ):
                sent += 1
        db.flush()
    quiet_days = get_settings().outreach_reply_quiet_days
    due_times = [
        enrollment.next_send_at
        for enrollment in result.enrollments
        if enrollment.next_send_at is not None
    ]
    return InFlightReenrollOut(
        resumed=result.resumed,
        completed=result.completed,
        sent=sent,
        not_found=result.not_found,
        skipped=[
            EnrollSkipOut(
                candidate_id=blocked.candidate_id,
                full_name=blocked.full_name,
                reason="replied_recently",
                replied_at=blocked.replied_at,
                reply_intent=blocked.reply_intent,
                quiet_days=quiet_days,
            )
            for blocked in result.replied
        ],
        next_send_at=min(due_times) if due_times else None,
    )


# --------------------------------------------------------------------------
# messages + inbound
# --------------------------------------------------------------------------

messages_router = APIRouter(route_class=CommittingRoute, prefix="/v1/messages", tags=["outreach"])


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


reply_drafts_router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/reply-drafts", tags=["outreach"]
)


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


webhooks_router = APIRouter(route_class=CommittingRoute, prefix="/v1/webhooks", tags=["outreach"])


def _inbound_email_identity(payload: InboundEmail, sender: str) -> tuple[str, str]:
    """``(provider_native_id, internet_message_id)`` for one webhook delivery.

    ESPs redeliver on any non-2xx and on their own timeouts, so the endpoint
    must be idempotent. The RFC Message-ID is the mail's real identity; when
    the parse payload does not carry headers, a hash of the delivery itself
    still collapses the retry case, which is the case that actually happens.
    """
    import hashlib

    rfc_id = (payload.message_id or "").strip()
    if rfc_id:
        return rfc_id, rfc_id
    digest = hashlib.sha256(
        "\x1f".join(
            [sender, payload.in_reply_to or "", payload.subject or "", payload.body or ""]
        ).encode("utf-8", errors="replace")
    ).hexdigest()
    return f"esp-{digest}", ""


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

    native_id, rfc_id = _inbound_email_identity(payload, sender)
    already = db.scalar(
        select(Message).where(
            Message.org_id == principal.org_id,
            Message.direction == "inbound",
            or_(
                Message.provider_native_id == native_id,
                # The same physical mail can arrive via mailbox sync first;
                # sync stores the RFC id on internet_message_id.
                Message.internet_message_id == rfc_id if rfc_id else false(),
            ),
        )
    )
    if already is not None:
        # A redelivery, not a new reply: nothing is stored, classified, or
        # suppressed a second time. Answer 200 so the ESP stops retrying.
        return {
            "message_id": message.id,
            "candidate_id": message.candidate_id,
            "intent": already.reply_intent or "",
            "duplicate": True,
        }

    verdict = record_reply(
        db,
        message=message,
        subject=payload.subject,
        body=payload.body,
        provider_native_id=native_id,
        internet_message_id=rfc_id,
    )
    return {
        "message_id": message.id,
        "candidate_id": message.candidate_id,
        "intent": verdict.intent,
        "confidence": verdict.confidence,
        "stopped_sequence": verdict.should_stop_sequence,
        "classified_by": verdict.classified_by,
        "summary": verdict.summary,
    }
