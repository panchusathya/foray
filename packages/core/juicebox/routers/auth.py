"""Signup, login and API-key management."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import ApiKey, Org, User
from ..schemas import ApiKeyOut, LoginRequest, SignupRequest, TokenResponse, UserOut
from ..security import (
    create_access_token,
    generate_api_key,
    hash_password,
    verify_password,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/auth", tags=["auth"])


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "org"


def _unique_slug(db: Session, base: str) -> str:
    slug = base
    suffix = 2
    while db.scalar(select(func.count(Org.id)).where(Org.slug == slug)):
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def _token_for(user: User) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(
            subject=user.id, org_id=user.org_id, role=user.role
        ),
        expires_in=settings.access_token_ttl_seconds,
        user=UserOut.model_validate(user),
    )


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = payload.email.lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    org = Org(name=payload.org_name, slug=_unique_slug(db, _slugify(payload.org_name)))
    db.add(org)
    db.flush()

    user = User(
        org_id=org.id,
        email=email,
        name=payload.name or email.split("@")[0],
        password_hash=hash_password(payload.password),
        role="owner",
    )
    db.add(user)
    db.flush()
    return _token_for(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    return _token_for(user)


@router.get("/me", response_model=UserOut)
def me(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> UserOut:
    if principal.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="API keys have no user profile"
        )
    user = db.get(User, principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserOut.model_validate(user)


@router.post("/api-keys", response_model=ApiKeyOut, status_code=status.HTTP_201_CREATED)
def create_api_key(
    name: str = "default",
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> ApiKeyOut:
    """Mint an org-scoped key. Write permission, because the key IS one.

    A key authenticates as role "service", which can_write, so a viewer
    allowed through here would hand itself permanent write access to the whole
    org — and one that carries no user id, so nothing it does has an actor.
    """
    raw, key_hash, prefix = generate_api_key()
    record = ApiKey(org_id=principal.org_id, name=name, key_hash=key_hash, prefix=prefix)
    db.add(record)
    db.flush()
    # The plaintext key is returned exactly once.
    return ApiKeyOut(
        id=record.id, name=record.name, prefix=record.prefix, key=raw, created_at=record.created_at
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> list[ApiKeyOut]:
    rows = db.scalars(
        select(ApiKey).where(ApiKey.org_id == principal.org_id, ApiKey.revoked.is_(False))
    ).all()
    return [
        ApiKeyOut(id=r.id, name=r.name, prefix=r.prefix, key=None, created_at=r.created_at)
        for r in rows
    ]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(
    key_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> None:
    """Revoking is a mutation, and cutting the org's integrations off is one a
    read-only principal has no business making."""
    record = db.get(ApiKey, key_id)
    if record is None or record.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    record.revoked = True
