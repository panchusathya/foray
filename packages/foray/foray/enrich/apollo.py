"""Apollo people-match: paid, shortlist-only, and now nothing is thrown away.

The original inline version in api/app.py paid a credit per call and read back
only person.linkedin_url, discarding the work email, personal emails, and the
employer's primary domain that arrive in the same response. This module
persists all of it.
"""

from __future__ import annotations

import httpx

from ..config import settings
from ..log import get_logger
from .emails import UPSERT_EMAIL
from .employer import is_work_domain

log = get_logger("enrich.apollo")

APOLLO_MATCH_URL = "https://api.apollo.io/v1/people/match"

# Apollo returns this literal local part when a credit did not unlock the
# address. Storing it would plant a plausible-looking fake that eventually
# gets sent to.
PLACEHOLDER_LOCALPART = "email_not_unlocked"


def _usable(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    if email.split("@", 1)[0] == PLACEHOLDER_LOCALPART:
        return None
    return email


async def people_match(node: dict) -> dict | None:
    """One credit. Returns the raw person payload, or None."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            APOLLO_MATCH_URL,
            headers={"X-Api-Key": settings.apollo_api_key, "Content-Type": "application/json"},
            json={"name": node.get("name"), "organization_name": node.get("company")},
        )
        if resp.status_code != 200:
            log.info("apollo.non_200", status=resp.status_code)
            return None
        return (resp.json() or {}).get("person") or None


async def match_and_store(conn, candidate_id: int, node: dict) -> str | None:
    """Call Apollo once, persist emails + employer domain, return linkedin_url.

    Shaped as Callable[[dict], Awaitable[str | None]] via functools.partial so
    it drops into resolve_linkedin(apollo=...) unchanged.
    """
    person = await people_match(node)
    if not person:
        return None

    org = person.get("organization") or {}
    primary_domain = (org.get("primary_domain") or "").strip().lower() or None
    verified = (person.get("email_status") or "").lower() == "verified"

    work = _usable(person.get("email"))
    if work:
        is_work = (primary_domain and work.endswith("@" + primary_domain)) or is_work_domain(work)
        await conn.execute(
            UPSERT_EMAIL,
            candidate_id,
            work,
            "apollo_work" if is_work else "apollo_personal",
            bool(is_work),
            85 if is_work else 70,
            verified,
            "apollo people-match",
            1,
        )

    for personal in person.get("personal_emails") or []:
        email = _usable(personal)
        if email and email != work:
            await conn.execute(
                UPSERT_EMAIL,
                candidate_id,
                email,
                "apollo_personal",
                False,
                70,
                False,
                "apollo personal_emails",
                1,
            )

    if primary_domain:
        await conn.execute(
            "update candidate_signals set employer_domain = $1 "
            "where candidate_id = $2 and employer_domain is null",
            primary_domain,
            candidate_id,
        )

    log.info(
        "apollo.stored",
        candidate_id=candidate_id,
        work=bool(work),
        personals=len(person.get("personal_emails") or []),
        linkedin=bool(person.get("linkedin_url")),
    )
    return person.get("linkedin_url")
