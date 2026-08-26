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
from .models import ApiKey, Candidate, User, utcnow
from .security import TokenError, decode_access_token, hash_api_key

# Payload `typ` values that separate candidate tokens from recruiter tokens.
# Recruiter/user tokens carry no `typ` (they predate the claim); candidate
# tokens always do, and each principal reader checks the claim EXPLICITLY in
# both directions -- the two dependency functions below are the only wall
# between the two surfaces, because everything signs with one SECRET_KEY.
CANDIDATE_TOKEN_TYPES = frozenset({"candidate", "portal-link"})

#: The role claim on the OAuth connect state (routers/integrations.py mints it).
OAUTH_STATE_ROLE = "mailconnect"

# Roles that exist to authorise ONE round trip, never a session. They are
# signed with the same SECRET_KEY as a login token but travel where a login
# token never would -- the connect state rides a query string to
# login.microsoftonline.com, so it lands in browser history and the provider's
# access logs. get_principal re-reads the role off the User row, so without
# this a recovered state token would authenticate as that user at their REAL
# role for its whole TTL. oauth_callback already refuses a login token as
# state; this is the other half of the same wall.
SINGLE_PURPOSE_TOKEN_ROLES = frozenset({OAUTH_STATE_ROLE})


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

    # Explicit, not incidental: a candidate token would also fail the User
    # lookup below, but the rejection must not depend on id prefixes never
    # colliding. A candidate session on a recruiter route is always a 401.
    if payload.get("typ") in CANDIDATE_TOKEN_TYPES:
        raise _UNAUTHORIZED
    if payload.get("role") in SINGLE_PURPOSE_TOKEN_ROLES:
        raise _UNAUTHORIZED

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return Principal(org_id=user.org_id, user_id=user.id, role=user.role, kind="user")


def require_write(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.can_write:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Read-only principal")
    return principal


@dataclass
class CandidatePrincipal:
    """Who a portal session speaks for. One candidate, one org, nothing else.

    Deliberately not a ``Principal``: nothing that accepts a ``Principal``
    can be handed one of these by accident, and portal routes scope every
    query by BOTH ids -- never load-then-check.
    """

    org_id: str
    candidate_id: str
    kind: str = "candidate"


def get_candidate_principal(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> CandidatePrincipal:
    """Bearer-only, and the token must say ``typ: candidate`` exactly.

    A recruiter token (no ``typ``) is refused; so is a magic-link token
    (``typ: portal-link``) -- a link is exchanged for a session, never used
    as one, or every link forwarded in a screenshot would be a session.
    """
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
    if payload.get("typ") != "candidate":
        raise _UNAUTHORIZED
    candidate = db.get(Candidate, payload["sub"])
    if candidate is None or candidate.org_id != payload.get("org"):
        raise _UNAUTHORIZED
    return CandidatePrincipal(org_id=candidate.org_id, candidate_id=candidate.id)
