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

import re
from dataclasses import dataclass, field
from typing import Protocol

from ..log import get_logger
from .emails import UPSERT_EMAIL
from .employer import freemail_domains

log = get_logger("enrich.contacts")


class ProviderUnavailable(RuntimeError):
    """The provider answered, but not with real data about the person we asked for.

    Its own class because the caller has to treat it differently from "no
    address found": a miss is a fact worth caching, and this is not. Caching it
    would stamp the negative cache from a vendor's quota wall and make the
    candidate permanently unenrichable.
    """


# RFC 2606 / RFC 6761 reserve these so documentation and tests can use them
# without ever reaching a real mailbox. An address in one of them is by
# definition not a way to contact anybody.
RESERVED_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "invalid", "localhost", "test"}
)
RESERVED_SUFFIXES = (".example", ".invalid", ".localhost", ".test")

# Local parts that are filler in any payload, on any domain. These are never
# somebody's real address.
_ALWAYS_FILLER = re.compile(
    r"^(?:sample|example|your[-_.]?e?-?mail|first[-_.]?last|full[-_.]?name|"
    r"no-?reply|do-?not-?reply|email[-_.]?address)$",
    re.IGNORECASE,
)

# Filler words that are *also* real addresses in the wild: plenty of developers
# use mail@theirdomain.com. Rejected only with a numeric suffix -- which is what
# a generated sample looks like (`email1`, `email2`, `test2`) and what a person
# almost never chooses -- or on a freemail domain, where there is no personal
# domain to justify it.
#
# `abc` and `xyz` are deliberately absent: abc360982737@gmail.com is a real
# commit-mined address in this database, and the numbered rule would have eaten
# it.
_SOFT_WORDS = r"e?-?mail|test|sample|user|username|name|foo|bar|baz"
_SOFT_FILLER = re.compile(rf"^(?:{_SOFT_WORDS})$", re.IGNORECASE)
_SOFT_FILLER_NUMBERED = re.compile(rf"^(?:{_SOFT_WORDS})[-_.]?\d{{1,3}}$", re.IGNORECASE)


def is_placeholder(email: str) -> bool:
    """Whether this address is documentation filler rather than a person.

    Checked at the last gate before a write, not only in the adapter, because a
    stored fake address becomes ``primary_email`` and then a send address. The
    freemail half of a placeholder pair is the dangerous one:
    ``email2@gmail.com`` is syntactically perfect and may well belong to a
    stranger.

    The rule is deliberately narrower than the obvious one. A first pass matched
    any filler word and would have deleted fifteen real addresses already in the
    database -- ``mail@danijar.com``, ``email@jamie.sh``, ``mail@taeuk.me`` --
    all mined from git history, all genuine. Somebody's own domain is exactly
    where ``mail@`` is a real choice. A false negative is a retryable miss; a
    false positive silently destroys the only way to reach someone.
    """
    local, _, domain = (email or "").strip().lower().rpartition("@")
    if not local or not domain:
        return True
    if domain in RESERVED_DOMAINS or domain.endswith(RESERVED_SUFFIXES):
        return True
    if _ALWAYS_FILLER.match(local):
        return True
    if _SOFT_FILLER_NUMBERED.match(local):
        return True
    if _SOFT_FILLER.match(local):
        # Bare `mail@` is a real address on a personal domain and filler on a
        # freemail one, where nobody had to own anything to pick it.
        from .employer import is_work_domain

        return not is_work_domain(email)
    return False


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
        if is_placeholder(found.email):
            # Belt and braces. The adapter filters these too, but this is the
            # last gate before the row exists, and a stored placeholder becomes
            # Candidate.primary_email and then a send address.
            log.warning(
                f"{provider}.placeholder_rejected",
                candidate_id=candidate_id,
                email=found.email,
            )
            continue
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
        # domain another source already established. A freemail domain is not
        # an employer; the profile-email backfill already refuses those, and
        # writing gmail.com here would poison search filters.
        domain = result.employer_domain.strip().lower()
        if domain and domain not in freemail_domains():
            await conn.execute(
                "update candidate_signals set employer_domain = $1"
                " where candidate_id = $2 and employer_domain is null",
                domain,
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
