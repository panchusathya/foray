"""Routes that spend money, and are therefore not open to the internet.

``/foray/*`` is proxied straight through by nginx with no authentication in
front of it -- no Caddy rule, no ``Depends`` on any principal, and Foray's
database has no ``org_id`` to scope anything to. That is tolerable for search
and enrichment, which are read-only and free. It is not tolerable for a
provider lookup billed per address found: an unauthenticated POST that costs
money is a way for a stranger to spend our credits.

So the paid path lives here behind a shared secret that only the Juicebox API
container holds, and Juicebox does the real authentication -- an org-scoped
principal with write permission -- before it ever calls in. nginx also refuses
``/foray/internal/`` outright, which makes the token a second line rather than
the only one.

The spend discipline, in the order it is applied per candidate:

1. no LinkedIn URL -> no request, because nothing could match it;
2. ``contactout_checked_at`` already set -> no request, because we have already
   asked this question and paid for the answer;
3. a personal address already on file -> no request, because the address is
   what we were buying.

Everything left over is looked up once, and stamped whether or not it produced
an address.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from ..config import settings
from ..db import get_pool
from ..enrich.contacts import (
    EmailProvider,
    ProviderResult,
    ProviderUnavailable,
    store_result,
)
from ..log import get_logger

log = get_logger("api.internal")

router = APIRouter(prefix="/internal", tags=["internal"])

# ContactOut answers in well under a second, so the wall-clock cost of a batch
# is concurrency-bound, not rate-bound (the limiter allows 300/min). Eight at a
# time keeps a 100-candidate batch inside a request someone is waiting on
# without opening a hundred sockets to a vendor.
LOOKUP_CONCURRENCY = 8
MAX_BATCH = 100

# Which candidates are worth a lookup, and everything the writer needs after.
# `has_personal` is the do-we-already-own-it check, and a work address does not
# count towards it: a recruiting pitch cannot go to somebody's work inbox, so a
# work-only candidate is still unreachable and still worth paying for.
SELECT_TARGETS = """
select c.id,
       c.login,
       c.linkedin_url,
       c.contactout_checked_at is not null as checked,
       exists (
         select 1 from candidate_emails e
         where e.candidate_id = c.id and e.is_work = false and e.source <> 'guess'
       ) as has_personal
  from candidates c
 where c.login = any($1::text[])
"""

SKIPPED = {"cached", "has_personal", "no_profile"}

# Marks a failure as "the provider is up but not answering honestly" rather
# than a one-off error, so the caller can report the difference.
_UNAVAILABLE = "unavailable:"

_provider: EmailProvider | None = None


def get_provider() -> EmailProvider:
    global _provider
    if _provider is None:
        from ..enrich.contactout import ContactOutProvider

        _provider = ContactOutProvider()
    return _provider


def set_provider(provider: EmailProvider | None) -> None:
    """Tests inject a recorder. Nothing in production calls this."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = None


class FindEmailsBody(BaseModel):
    logins: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    reason: str = Field(min_length=3, max_length=200)


def _require_token(token: str | None) -> None:
    expected = settings.foray_internal_token
    if not expected:
        # Deliberately not settings.require(), which raises SystemExit -- inside
        # a request handler that kills the worker process instead of answering.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FORAY_INTERNAL_TOKEN is not configured on this deployment",
        )
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


@router.post("/candidates/find-emails")
async def find_emails(
    body: FindEmailsBody,
    x_foray_token: str | None = Header(default=None, alias="X-Foray-Token"),
) -> dict:
    _require_token(x_foray_token)

    provider = get_provider()
    if not provider.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CONTACTOUT_API_KEY is not configured on this deployment",
        )

    logins = list(dict.fromkeys(login.strip() for login in body.logins if login.strip()))
    if not logins:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No logins given"
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(SELECT_TARGETS, logins)

    results, to_look_up = triage(logins, rows)
    looked_up = await _look_up(provider, to_look_up)

    written = 0
    unavailable: str | None = None
    async with pool.acquire() as conn:
        for row, result, failure in looked_up:
            if result is None:
                # The lookup failed. Do NOT stamp the cache: that would record a
                # vendor problem as "we asked, there is nothing there", and no
                # later click could ever correct it.
                if failure and failure.startswith(_UNAVAILABLE):
                    unavailable = failure[len(_UNAVAILABLE) :]
                    results[row["login"]] = {"status": "provider_unavailable"}
                else:
                    results[row["login"]] = {"status": "error"}
                continue
            async with conn.transaction():
                await conn.execute(
                    "insert into shortlist_events (candidate_id, reason) values ($1, $2)",
                    row["id"],
                    body.reason,
                )
                written += await store_result(conn, row["id"], provider.name, result)
                await conn.execute(
                    "update candidates set contactout_checked_at = now() where id = $1",
                    row["id"],
                )
            personal = [e.email for e in result.personal]
            results[row["login"]] = {
                "status": "found" if personal else "none",
                "personal": personal,
                "work": [e.email for e in result.emails if e.is_work],
            }

    summary = {
        "requested": len(logins),
        "looked_up": len(looked_up),
        "rows_written": written,
        "found_personal": sum(1 for r in results.values() if r["status"] == "found"),
        "skipped": sum(1 for r in results.values() if r["status"] in SKIPPED),
        "errors": sum(1 for r in results.values() if r["status"] == "error"),
        "unavailable": sum(
            1 for r in results.values() if r["status"] == "provider_unavailable"
        ),
    }
    log.info("internal.find_emails", reason=body.reason, **summary)
    payload: dict = {"provider": provider.name, "summary": summary, "results": results}
    if unavailable:
        # Say what the provider actually said. "0 found" would read as "none of
        # these people have an address", which is a different and wrong answer.
        payload["detail"] = unavailable
    return payload


def triage(
    logins: list[str], rows: list[Any]
) -> tuple[dict[str, dict[str, Any]], list[Any]]:
    """Split a request into "already answered" and "worth paying for".

    Every branch that returns a status instead of a row is a credit not spent,
    which is why this is a separate function with its own tests rather than a
    loop inside the handler.
    """
    known = {row["login"]: row for row in rows}
    results: dict[str, dict[str, Any]] = {}
    to_look_up: list[Any] = []
    for login in logins:
        row = known.get(login)
        if row is None:
            results[login] = {"status": "unknown"}
        elif row["has_personal"]:
            # We were buying a personal address and one is already on file.
            results[login] = {"status": "has_personal"}
        elif row["checked"]:
            # Asked before, paid before. The answer does not change often enough
            # to be worth asking twice.
            results[login] = {"status": "cached"}
        elif not row["linkedin_url"]:
            # Nothing for the provider to match on.
            results[login] = {"status": "no_profile"}
        else:
            to_look_up.append(row)
    return results, to_look_up


async def _look_up(
    provider: EmailProvider, rows: list[Any]
) -> list[tuple[Any, ProviderResult | None, str | None]]:
    """Concurrent lookups, each result paired back with its row.

    Returns ``(row, result, failure)``. ``failure`` is prefixed with
    ``_UNAVAILABLE`` when the provider is answering but not with real data, so
    the caller can say so instead of reporting "nothing found".

    Writes are deliberately left to the caller on a single connection: a hundred
    tasks each acquiring from the pool would starve the rest of the service for
    the duration of one bulk click.
    """
    if not rows:
        return []
    gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def one(row: Any) -> tuple[Any, ProviderResult | None, str | None]:
        async with gate:
            try:
                result = await provider.find_emails(
                    {"login": row["login"], "linkedin_url": row["linkedin_url"]}
                )
                return row, result, None
            except ProviderUnavailable as exc:
                log.warning(
                    "internal.find_emails.provider_unavailable",
                    login=row["login"],
                    error=str(exc),
                )
                return row, None, f"{_UNAVAILABLE}{exc}"
            except Exception as exc:  # noqa: BLE001 - one bad lookup must not fail the batch
                log.warning("internal.find_emails.failed", login=row["login"], error=str(exc))
                return row, None, str(exc)

    return list(await asyncio.gather(*(one(row) for row in rows)))


class ApolloBody(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/candidates/{login}/apollo")
async def apollo_resolve(
    login: str,
    body: ApolloBody,
    x_foray_token: str | None = Header(default=None, alias="X-Foray-Token"),
) -> dict:
    """Explicit shortlist action: allow the Apollo step (a credit). Requires a reason."""
    import functools

    from ..enrich.apollo import match_and_store
    from ..enrich.linkedin import resolve_and_store

    _require_token(x_foray_token)
    if not settings.apollo_api_key:
        # settings.require() raises SystemExit, which inside a request
        # handler kills the worker rather than answering the caller.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APOLLO_API_KEY is not configured on this deployment",
        )
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidate_id = await conn.fetchval(
            "insert into shortlist_events (candidate_id, reason) "
            "select id, $2 from candidates where login = $1 "
            "returning candidate_id",
            login,
            body.reason,
        )
        if candidate_id is None:
            return {"login": login, "linkedin_url": None, "linkedin_source": None, "found": False}
        return await resolve_and_store(
            conn,
            login,
            allow_apollo=True,
            apollo_reason=body.reason,
            apollo=functools.partial(match_and_store, conn, candidate_id),
        )
