"""The client dashboard's own API: ``/v1/client/*``.

Everything here takes a ``ClientPrincipal`` and nothing else, and every query
is scoped by the principal's ``client_account_id``. A client can read the
people shared with its company, move their stage and write notes -- and that
is the whole surface. ``get_principal`` refuses a client token, so the CRM,
sequences and projects are not merely hidden from this login, they 401.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import client_dashboard as dash
from ..config import get_settings
from ..deps import (
    CLIENT_TOKEN_TYPE,
    ClientPrincipal,
    CommittingRoute,
    get_client_principal,
    get_db,
)
from ..models import ClientAccount, ClientSubmissionNote, ClientUser, utcnow
from ..schemas import (
    ClientLoginRequest,
    ClientNoteIn,
    ClientNoteOut,
    ClientStageIn,
    ClientSubmissionDetail,
    ClientSubmissionOut,
    ClientTokenResponse,
    ClientUserOut,
)
from ..security import create_access_token, verify_password

router = APIRouter(route_class=CommittingRoute, prefix="/v1/client", tags=["client-dashboard"])

_BAD_LOGIN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
)


def _actor(principal: ClientPrincipal) -> dash.Actor:
    return dash.Actor(kind=dash.ACTOR_CLIENT, id=principal.client_user_id, name=principal.name)


def _user_out(user: ClientUser, account: ClientAccount) -> ClientUserOut:
    return ClientUserOut(
        id=user.id,
        username=user.username,
        name=user.name or user.username,
        client_account_id=account.id,
        client_name=account.name,
    )


@router.post("/auth/login", response_model=ClientTokenResponse)
def login(payload: ClientLoginRequest, db: Session = Depends(get_db)) -> ClientTokenResponse:
    user = db.scalar(
        select(ClientUser).where(ClientUser.username == dash.normalize_username(payload.username))
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        raise _BAD_LOGIN
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    account = db.get(ClientAccount, user.client_account_id)
    if account is None:
        raise _BAD_LOGIN
    user.last_login_at = utcnow()
    settings = get_settings()
    token = create_access_token(
        subject=user.id,
        org_id=user.org_id,
        role="client",
        extra={"typ": CLIENT_TOKEN_TYPE},
    )
    return ClientTokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_seconds,
        user=_user_out(user, account),
    )


@router.get("/me", response_model=ClientUserOut)
def me(
    principal: ClientPrincipal = Depends(get_client_principal), db: Session = Depends(get_db)
) -> ClientUserOut:
    user = db.get(ClientUser, principal.client_user_id)
    account = db.get(ClientAccount, principal.client_account_id)
    return _user_out(user, account)


@router.get("/submissions", response_model=list[ClientSubmissionOut])
def list_submissions(
    principal: ClientPrincipal = Depends(get_client_principal), db: Session = Depends(get_db)
) -> list[ClientSubmissionOut]:
    rows = dash.list_submissions(
        db, account_id=principal.client_account_id, org_id=principal.org_id
    )
    return dash.client_rows(db, rows, _actor(principal), model=ClientSubmissionOut)


def _load(db: Session, principal: ClientPrincipal, submission_id: str):
    row = dash.load_submission(
        db,
        org_id=principal.org_id,
        submission_id=submission_id,
        account_id=principal.client_account_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return row


@router.get("/submissions/{submission_id}", response_model=ClientSubmissionDetail)
def get_submission(
    submission_id: str,
    principal: ClientPrincipal = Depends(get_client_principal),
    db: Session = Depends(get_db),
) -> ClientSubmissionDetail:
    row = _load(db, principal, submission_id)
    return dash.detail(db, row, _actor(principal), model=ClientSubmissionDetail)


@router.patch("/submissions/{submission_id}", response_model=ClientSubmissionDetail)
def set_stage(
    submission_id: str,
    payload: ClientStageIn,
    principal: ClientPrincipal = Depends(get_client_principal),
    db: Session = Depends(get_db),
) -> ClientSubmissionDetail:
    row = _load(db, principal, submission_id)
    try:
        dash.move_stage(db, submission=row, stage=payload.stage, actor=_actor(principal))
    except dash.ClientDashboardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return dash.detail(db, row, _actor(principal), model=ClientSubmissionDetail)


@router.post(
    "/submissions/{submission_id}/notes",
    response_model=ClientNoteOut,
    status_code=status.HTTP_201_CREATED,
)
def create_note(
    submission_id: str,
    payload: ClientNoteIn,
    principal: ClientPrincipal = Depends(get_client_principal),
    db: Session = Depends(get_db),
) -> ClientNoteOut:
    row = _load(db, principal, submission_id)
    actor = _actor(principal)
    try:
        note = dash.add_note(db, submission=row, body=payload.body, actor=actor)
    except dash.ClientDashboardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return dash.note_out(note, actor)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    note_id: str,
    principal: ClientPrincipal = Depends(get_client_principal),
    db: Session = Depends(get_db),
) -> None:
    """A client may delete only what that login wrote -- never our notes."""
    note = db.get(ClientSubmissionNote, note_id)
    if (
        note is None
        or note.org_id != principal.org_id
        or note.author_kind != dash.ACTOR_CLIENT
        or note.author_id != principal.client_user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    _load(db, principal, note.submission_id)  # and it must be on this company's row
    db.delete(note)
