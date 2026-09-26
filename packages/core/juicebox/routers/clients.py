"""Recruiter side of the client dashboards: ``/v1/clients/*``.

The same rows a client sees through ``/v1/client``, plus what only we may do:
create the company and its logins, share people with it, correct the
snapshot, and remove someone we shared by mistake. Stage moves and notes go
through the same ``client_dashboard`` functions the client's routes use, so a
move made here is attributed and recorded exactly like one made there.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import client_dashboard as dash
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import ClientAccount, ClientSubmissionNote, ClientUser, User
from ..schemas import (
    ClientAccountIn,
    ClientAccountOut,
    ClientAccountPatch,
    ClientBulkStageIn,
    ClientEntriesIn,
    ClientEntriesOut,
    ClientLoginIn,
    ClientLoginOut,
    ClientLoginPatch,
    ClientNoteIn,
    ClientNoteOut,
    RecruiterSubmissionDetail,
    RecruiterSubmissionOut,
    RecruiterSubmissionPatch,
)
from ..security import hash_password

router = APIRouter(route_class=CommittingRoute, prefix="/v1/clients", tags=["clients"])


def _actor(db: Session, principal: Principal) -> dash.Actor:
    name = "API"
    if principal.user_id:
        user = db.get(User, principal.user_id)
        if user is not None:
            name = user.name or user.email
    return dash.Actor(kind=dash.ACTOR_RECRUITER, id=principal.user_id, name=name)


def _unprocessable(exc: dash.ClientDashboardError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _account(db: Session, principal: Principal, account_id: str) -> ClientAccount:
    account = dash.load_account(db, org_id=principal.org_id, account_id=account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return account


def _submission(db: Session, principal: Principal, submission_id: str):
    row = dash.load_submission(db, org_id=principal.org_id, submission_id=submission_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return row


def _login_out(user: ClientUser) -> ClientLoginOut:
    return ClientLoginOut(
        id=user.id,
        username=user.username,
        name=user.name,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
    )


def _account_out(db: Session, account: ClientAccount) -> ClientAccountOut:
    logins = db.scalars(
        select(ClientUser)
        .where(ClientUser.client_account_id == account.id)
        .order_by(ClientUser.created_at)
    ).all()
    counts = dash.stage_counts(db, account_id=account.id)
    return ClientAccountOut(
        id=account.id,
        name=account.name,
        created_at=account.created_at,
        logins=[_login_out(u) for u in logins],
        stage_counts=counts,
        total=sum(counts.values()),
        client_activity_7d=dash.client_activity(db, account_id=account.id),
    )


# --------------------------------------------------------------------------
# accounts and logins
# --------------------------------------------------------------------------


@router.get("", response_model=list[ClientAccountOut])
def list_accounts(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[ClientAccountOut]:
    accounts = db.scalars(
        select(ClientAccount)
        .where(ClientAccount.org_id == principal.org_id)
        .order_by(ClientAccount.name)
    ).all()
    return [_account_out(db, a) for a in accounts]


@router.post("", response_model=ClientAccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: ClientAccountIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ClientAccountOut:
    name = payload.name.strip()
    if db.scalar(
        select(ClientAccount.id).where(
            ClientAccount.org_id == principal.org_id, ClientAccount.name == name
        )
    ):
        raise HTTPException(status_code=409, detail=f"{name} already has a dashboard")
    account = ClientAccount(org_id=principal.org_id, name=name)
    db.add(account)
    db.flush()
    return _account_out(db, account)


@router.get("/{account_id}", response_model=ClientAccountOut)
def get_account(
    account_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClientAccountOut:
    return _account_out(db, _account(db, principal, account_id))


@router.patch("/{account_id}", response_model=ClientAccountOut)
def update_account(
    account_id: str,
    payload: ClientAccountPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ClientAccountOut:
    """Rename a client. Logins still showing the old company name follow it.

    A login's display name defaults to the company's, and it is what the
    client's notes and stage moves are signed with. Leaving it behind would
    have "Flowgen" renamed to "Flowgen AI" on every screen except the byline
    of everything they write. A login somebody gave a person's name keeps it.
    Bylines already written are snapshots and stay as they were.
    """
    account = _account(db, principal, account_id)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="a client needs a name")
        clash = db.scalar(
            select(ClientAccount.id).where(
                ClientAccount.org_id == principal.org_id,
                ClientAccount.name == name,
                ClientAccount.id != account.id,
            )
        )
        if clash:
            raise HTTPException(status_code=409, detail=f"{name} already has a dashboard")
        for login in db.scalars(
            select(ClientUser).where(ClientUser.client_account_id == account.id)
        ).all():
            if login.name == account.name:
                login.name = name
        account.name = name
    db.flush()
    return _account_out(db, account)


@router.post(
    "/{account_id}/logins", response_model=ClientLoginOut, status_code=status.HTTP_201_CREATED
)
def create_login(
    account_id: str,
    payload: ClientLoginIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ClientLoginOut:
    account = _account(db, principal, account_id)
    try:
        user = dash.create_login(
            db,
            account=account,
            username=payload.username,
            password=payload.password,
            name=payload.name,
        )
    except dash.ClientDashboardError as exc:
        raise _unprocessable(exc) from exc
    except IntegrityError as exc:  # two creates racing on one username
        raise HTTPException(status_code=409, detail="username is taken") from exc
    return _login_out(user)


@router.patch("/logins/{login_id}", response_model=ClientLoginOut)
def update_login(
    login_id: str,
    payload: ClientLoginPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ClientLoginOut:
    user = db.get(ClientUser, login_id)
    if user is None or user.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login not found")
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.name is not None:
        user.name = payload.name.strip()
    if payload.is_active is not None:
        user.is_active = payload.is_active
    db.flush()
    return _login_out(user)


# --------------------------------------------------------------------------
# people shared
# --------------------------------------------------------------------------


@router.get("/{account_id}/submissions", response_model=list[RecruiterSubmissionOut])
def list_submissions(
    account_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[RecruiterSubmissionOut]:
    account = _account(db, principal, account_id)
    rows = dash.list_submissions(db, account_id=account.id, org_id=principal.org_id)
    return dash.client_rows(db, rows, _actor(db, principal), model=RecruiterSubmissionOut)


@router.post("/{account_id}/submissions", response_model=ClientEntriesOut)
def share(
    account_id: str,
    payload: ClientEntriesIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ClientEntriesOut:
    """Share people with the client, or update ones already shared.

    Idempotent per person: re-sending a list with stages moves the rows that
    changed and reports the rest as ``unchanged``.
    """
    account = _account(db, principal, account_id)
    if not payload.entries and not payload.project_id:
        raise HTTPException(status_code=422, detail="nothing to share")
    try:
        results = dash.share_entries(
            db, account=account, payload=payload, actor=_actor(db, principal)
        )
    except dash.ClientDashboardError as exc:
        raise _unprocessable(exc) from exc
    tally = {k: 0 for k in ("added", "updated", "unchanged", "skipped")}
    for result in results:
        tally[result.outcome] += 1
    return ClientEntriesOut(**tally, results=results)


@router.post("/{account_id}/submissions/stage", response_model=list[RecruiterSubmissionOut])
def bulk_stage(
    account_id: str,
    payload: ClientBulkStageIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> list[RecruiterSubmissionOut]:
    account = _account(db, principal, account_id)
    actor = _actor(db, principal)
    try:
        dash.validate_stage(payload.stage)
    except dash.ClientDashboardError as exc:
        raise _unprocessable(exc) from exc
    wanted = set(payload.ids)
    rows = [
        r
        for r in dash.list_submissions(db, account_id=account.id, org_id=principal.org_id)
        if r.id in wanted
    ]
    for row in rows:
        dash.move_stage(db, submission=row, stage=payload.stage, actor=actor)
    return dash.client_rows(db, rows, actor, model=RecruiterSubmissionOut)


@router.get("/submissions/{submission_id}", response_model=RecruiterSubmissionDetail)
def get_submission(
    submission_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> RecruiterSubmissionDetail:
    row = _submission(db, principal, submission_id)
    return dash.detail(db, row, _actor(db, principal), model=RecruiterSubmissionDetail)


@router.patch("/submissions/{submission_id}", response_model=RecruiterSubmissionDetail)
def update_submission(
    submission_id: str,
    payload: RecruiterSubmissionPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> RecruiterSubmissionDetail:
    row = _submission(db, principal, submission_id)
    actor = _actor(db, principal)
    fields = payload.model_dump(exclude_unset=True)
    stage = fields.pop("stage", None)
    for field, value in fields.items():
        if value is not None:
            setattr(row, field, value.strip())
    try:
        if stage:
            dash.move_stage(db, submission=row, stage=stage, actor=actor)
    except dash.ClientDashboardError as exc:
        raise _unprocessable(exc) from exc
    db.flush()
    return dash.detail(db, row, actor, model=RecruiterSubmissionDetail)


@router.delete("/submissions/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
def unshare(
    submission_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    """Take somebody off the client's dashboard, with their notes and history."""
    db.delete(_submission(db, principal, submission_id))


@router.post(
    "/submissions/{submission_id}/notes",
    response_model=ClientNoteOut,
    status_code=status.HTTP_201_CREATED,
)
def create_note(
    submission_id: str,
    payload: ClientNoteIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ClientNoteOut:
    row = _submission(db, principal, submission_id)
    actor = _actor(db, principal)
    try:
        note = dash.add_note(db, submission=row, body=payload.body, actor=actor)
    except dash.ClientDashboardError as exc:
        raise _unprocessable(exc) from exc
    return dash.note_out(note, actor)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    note_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    """Any recruiter may remove a note, the client's included.

    The client asked for a shared thread, and we own the dashboard: a note
    that should never have been written (a candidate's comp pasted into it,
    say) has to be removable by somebody who is not the client.
    """
    note = db.get(ClientSubmissionNote, note_id)
    if note is None or note.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    db.delete(note)
