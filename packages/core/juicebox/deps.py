"""FastAPI dependencies: DB session and the authenticated principal."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, Response, status
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .db import get_session_factory
from .models import ApiKey, User, utcnow
from .security import TokenError, decode_access_token, hash_api_key


def get_db(request: Request) -> Iterator[Session]:
    session = get_session_factory()()
    # CommittingRoute commits through request.state before the response goes
    # out; the commit below is only a backstop for handlers that bypass it.
    request.state.db = session
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class CommittingRoute(APIRoute):
    """An APIRoute that commits the request's DB session before responding.

    FastAPI runs yield-dependency teardown only after the response has been
    sent, so a commit left to get_db() races any client that acts on a 2xx
    immediately — a freshly signed-up token could 401, a just-created row
    could be invisible to the next request. Committing here, after the
    handler returns but before a byte leaves the server, closes that window;
    a failed commit becomes a 500 instead of a silently lost write.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def commit_then_respond(request: Request) -> Response:
            response = await handler(request)
            session = getattr(request.state, "db", None)
            if session is not None:
                await run_in_threadpool(session.commit)
            return response

        return commit_then_respond


@dataclass
class Principal:
    org_id: str
    user_id: str | None
    role: str
    kind: str  # "user" | "api_key"

    @property
    def can_write(self) -> bool:
        return self.role in {"owner", "recruiter", "service"}


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_principal(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    if x_api_key:
        record = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key)))
        if record is None or record.revoked:
            raise _UNAUTHORIZED
        record.last_used_at = utcnow()
        return Principal(org_id=record.org_id, user_id=None, role="service", kind="api_key")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise _UNAUTHORIZED

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return Principal(org_id=user.org_id, user_id=user.id, role=user.role, kind="user")


def require_write(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.can_write:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Read-only principal")
    return principal
