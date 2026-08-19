"""Employer inference. The GitHub company field is blank or a joke for most
engineers, so we infer in priority order and record which rule fired. We never
guess: if nothing fires, the employer is null.
"""

from __future__ import annotations

from functools import lru_cache

import yaml

from ..config import settings


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    path = settings.data_dir / "company_aliases.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k).strip().lower(): str(v) for k, v in data.items()}


@lru_cache(maxsize=1)
def _community_orgs() -> set[str]:
    path = settings.data_dir / "community_orgs.txt"
    return {
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


@lru_cache(maxsize=1)
def freemail_domains() -> set[str]:
    path = settings.data_dir / "freemail.txt"
    return {
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def domain_of(email: str) -> str | None:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[-1] or None


def is_work_domain(email: str) -> bool:
    domain = domain_of(email)
    return bool(domain) and domain not in freemail_domains()


def _canonical_company(raw: str) -> str:
    cleaned = raw.strip().lstrip("@").strip()
    key = cleaned.lower()
    if key in _aliases():
        return _aliases()[key]
    return cleaned


def infer_employer(node: dict) -> tuple[str | None, str | None]:
    """Return (employer, source) where source is company | org | email_domain."""
    # 1. company field
    company = (node.get("company") or "").strip()
    if company:
        canonical = _canonical_company(company)
        if canonical:
            return canonical, "company"

    # 2. organization membership (excluding communities)
    orgs = ((node.get("organizations") or {}).get("nodes")) or []
    for org in orgs:
        login = (org.get("login") or "").lower()
        if not login or login in _community_orgs():
            continue
        name = org.get("name") or org.get("login")
        return _canonical_company(name), "org"

    # 3. public email domain, if not freemail
    email = (node.get("email") or "").strip().lower()
    if "@" in email:
        domain = email.rsplit("@", 1)[-1]
        if domain and domain not in freemail_domains():
            # Map through aliases too (e.g. stripe.com -> Stripe if listed).
            base = domain.split(".")[0]
            return _aliases().get(base, domain), "email_domain"

    # 4. otherwise null. Do not guess.
    return None, None
