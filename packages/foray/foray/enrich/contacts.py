"""The seam a paid email provider plugs into.

Apollo arrived as a callable threaded through ``resolve_linkedin``'s waterfall,
which was the right shape for what it did there: return a LinkedIn URL, stop at
the first hit. It is the wrong shape for an email provider. That waterfall's
currency is ``linkedin_url``, so a provider that returns addresses but no URL is
recorded as a *miss* -- stamping ``linkedin_checked_at`` and reporting
``found: false`` after the credit was already spent. Adding a fifth step would
also be short-circuited by Apollo succeeding at step four.

So email providers live here instead, behind a Protocol, and the persistence and
spend-discipline they all need lives here with them. A provider implements
``find_emails`` and nothing else; it never writes to the database.

Confidence answers "how sure are we this address belongs to this person". It is
*not* send-preference -- which address outreach uses is decided by the ordering
in ``foray/api/search.py``, and that prefers personal regardless of confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..log import get_logger
from .emails import UPSERT_EMAIL

log = get_logger("enrich.contacts")

# Where each provider's addresses sit on the ladder documented in emails.py.
# contactout_personal sits just above apollo_personal (70) because the provider
# is specialised for consumer addresses and measured 7x its hit rate; both stay
# below commit (75) and profile (90), which are self-published and so are
# evidence rather than assertion.
CONFIDENCE = {
    "contactout_personal": 72,
    "contactout_work": 80,
}


@dataclass(frozen=True)
class FoundEmail:
    """One address as a provider reports it.

    ``verified`` may only be set from a provider that actually asserts the
    address is deliverable. Never infer it from a syntax or MX check: those say
    the domain exists, not that the mailbox does.
    """

    email: str
    is_work: bool
    verified: bool = False


@dataclass
class ProviderResult:
    emails: list[FoundEmail] = field(default_factory=list)
    # Providers often return more than they were asked for. Taking the employer
    # domain costs nothing extra and improves ranking whether or not an address
    # came back.
    linkedin_url: str | None = None
    employer_domain: str | None = None
    api_calls: int = 0

    @property
    def personal(self) -> list[FoundEmail]:
        return [e for e in self.emails if not e.is_work]


class EmailProvider(Protocol):
    """A paid lookup that turns a candidate into contact addresses."""

    name: str

    def is_configured(self) -> bool:
        """False when this deployment has no credentials for the provider."""
        ...

    async def find_emails(self, node: dict) -> ProviderResult:
        """One lookup. ``node`` carries at least ``linkedin_url``, ``name`` and
        ``company``; a provider uses whichever it needs."""
        ...


async def store_result(
    conn, candidate_id: int, provider: str, result: ProviderResult
) -> int:
    """Persist a provider's addresses. Returns how many rows were written.

    Goes through the shared UPSERT_EMAIL, which has three behaviours worth
    knowing before trusting what lands: ``is_work`` is frozen at first insert,
    ``evidence`` is first-write-wins, and ``source`` only changes on a *strict*
    confidence improvement. So re-finding an address a better source already
    supplied is a no-op rather than a downgrade -- which is the point.
    """
    written = 0
    for found in result.emails:
        source = f"{provider}_{'work' if found.is_work else 'personal'}"
        confidence = CONFIDENCE.get(source)
        if confidence is None:
            # A source string with no place on the ladder would violate the
            # candidate_emails check constraint at the last possible moment.
            # Refuse loudly here instead.
            raise ValueError(f"no confidence defined for email source {source!r}")
        await conn.execute(
            UPSERT_EMAIL,
            candidate_id,
            found.email,
            source,
            found.is_work,
            confidence,
            found.verified,
            f"{provider} lookup",
            1,
        )
        written += 1

    if result.employer_domain:
        # Guarded the way apollo.py guards it: fill a gap, never overwrite a
        # domain another source already established.
        await conn.execute(
            "update candidate_signals set employer_domain = $1"
            " where candidate_id = $2 and employer_domain is null",
            result.employer_domain,
            candidate_id,
        )

    log.info(
        f"{provider}.stored",
        candidate_id=candidate_id,
        written=written,
        personal=len(result.personal),
        api_calls=result.api_calls,
    )
    return written
