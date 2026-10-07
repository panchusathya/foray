"""Free checks on a recipient address before a sequence mails it.

No verification service and no mailbox probe -- the VM cannot reach port 25,
and a paid verifier was turned down. What is left is what DNS and our own
sending history can say, mapped onto the reach-score scale the sequencer's
floor already understands (``FORAY_MIN_REACH_SCORE``; 0 switches all of this
off, including the DNS lookups):

* ``UNDELIVERABLE`` (0): a typo of a big mailbox provider, a domain that does
  not exist, a domain with neither an MX nor an A record, or a null MX
  (RFC 7505, "this domain takes no mail").
* ``RISKY`` (40): an old ISP domain (AOL, AT&T, Comcast...) -- 23% of what we
  sent to these bounced, against 0.14% for Gmail -- or a domain whose bounce
  rate in this org's own history is high on a real sample.

Why each rule is shaped the way it is (the failure list this was written
from):

1. Our DNS failing (timeout, SERVFAIL) is not evidence about their domain.
   Only NXDOMAIN, or a definite empty answer for both MX and A, counts.
2. No MX but an A record still receives mail (RFC 5321 implicit MX).
3. A null MX is an explicit refusal and is undeliverable.
4. Typos are exact domain matches, never fuzzy: ymail.com and gmail.co.uk
   are real, and a fuzzy matcher would hold both.
5. The learned rule needs a sample (``LEARN_MIN_BOUNCES``) and a rate, so one
   bounce from one send cannot hold a domain and Gmail's absolute bounce
   count can never trip it.
6. An address we already delivered to (sent, not bounced, a day ago) is
   proven: its follow-ups are never held by a heuristic.
7. A stored verifier score beats a guess -- "deliverable" on aol.com still
   sends. Only the hard facts (typo, dead domain) override a stored score.
8. Floor 0 means no check at all, so the test suite stays offline.
9. A held first address falls through to the next one (the sequencer's
   existing order), rather than stopping the person.
10. DNS answers and the per-org bounce table are cached, so a busy day costs
    one lookup per domain, not one per send.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

UNDELIVERABLE = 0
RISKY = 40

# Old ISP and webmail domains. Mostly accounts opened decades ago and since
# abandoned; abandoned inboxes are what providers recycle into spam traps.
LEGACY_DOMAINS = frozenset(
    {
        "aol.com",
        "aim.com",
        "att.net",
        "sbcglobal.net",
        "bellsouth.net",
        "pacbell.net",
        "swbell.net",
        "ameritech.net",
        "flash.net",
        "prodigy.net",
        "comcast.net",
        "cox.net",
        "verizon.net",
        "earthlink.net",
        "mindspring.com",
        "juno.com",
        "netzero.net",
        "netzero.com",
        "excite.com",
        "charter.net",
        "roadrunner.com",
        "rr.com",
        "tds.net",
        "frontier.com",
        "frontiernet.net",
        "optonline.net",
        "centurylink.net",
        "embarqmail.com",
        "windstream.net",
        "rocketmail.com",
        "compuserve.com",
        "lycos.com",
        "mail.com",
        "usa.net",
    }
)

# Exact misspellings of the big providers. Never fuzzy: see rule 4.
TYPO_DOMAINS = {
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmal.com": "gmail.com",
    "gamil.com": "gmail.com",
    "gnail.com": "gmail.com",
    "gmaill.com": "gmail.com",
    "gmail.co": "gmail.com",
    "gmail.con": "gmail.com",
    "gmail.cm": "gmail.com",
    "gmail.om": "gmail.com",
    "gmailcom": "gmail.com",
    "gmail.comm": "gmail.com",
    "yaho.com": "yahoo.com",
    "yahooo.com": "yahoo.com",
    "yhoo.com": "yahoo.com",
    "yahoo.co": "yahoo.com",
    "yahoo.con": "yahoo.com",
    "yahoo.cm": "yahoo.com",
    "hotmial.com": "hotmail.com",
    "hotmal.com": "hotmail.com",
    "hotmai.com": "hotmail.com",
    "hotmail.co": "hotmail.com",
    "hotmail.con": "hotmail.com",
    "homail.com": "hotmail.com",
    "outlok.com": "outlook.com",
    "outlook.co": "outlook.com",
    "outlook.con": "outlook.com",
    "iclod.com": "icloud.com",
    "icloud.co": "icloud.com",
    "icoud.com": "icloud.com",
}

LEARN_MIN_BOUNCES = 3
LEARN_RATE = 0.20
# A send is "proven" once it has gone this long without a bounce.
PROVEN_AFTER = timedelta(days=1)
DNS_TIMEOUT = 3.0
DNS_TTL_SECONDS = 6 * 3600
STATS_TTL_SECONDS = 600


@dataclass(frozen=True)
class Verdict:
    score: int | None  # None: nothing against it
    reason: str = ""


OK = Verdict(None)


def domain_of(address: str) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[1] if "@" in address else ""


# -- DNS --------------------------------------------------------------------

# domain -> (expires_at, True if it can receive mail / False if not / None unknown)
_dns_cache: dict[str, tuple[float, bool | None]] = {}


def _lookup(domain: str) -> bool | None:
    """True: can receive mail. False: definitely cannot. None: we could not tell."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    try:
        answer = resolver.resolve(domain, "MX")
        exchanges = [str(r.exchange).rstrip(".") for r in answer]
        # RFC 7505 null MX: a single "0 ." record says no mail, ever.
        return not (len(exchanges) == 1 and exchanges[0] == "")
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass  # no MX: fall back to an A record (rule 2)
    except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.exception.DNSException):
        return None
    try:
        resolver.resolve(domain, "A")
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except dns.exception.DNSException:
        return None


def receives_mail(
    domain: str, *, lookup: Callable[[str], bool | None] | None = None
) -> bool | None:
    now = time.monotonic()
    hit = _dns_cache.get(domain)
    if hit is not None and hit[0] > now:
        return hit[1]
    result = (lookup or _lookup)(domain)
    # An unknown answer is retried sooner than a definite one.
    ttl = DNS_TTL_SECONDS if result is not None else 300
    _dns_cache[domain] = (now + ttl, result)
    return result


# -- what this org's own sending has taught us --------------------------------

_stats_cache: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}


def domain_bounce_stats(db: Session, org_id: str) -> dict[str, tuple[int, int]]:
    """domain -> (bounced, delivered) over this org's sequence sends."""
    from ..models import Message

    now = time.monotonic()
    hit = _stats_cache.get(org_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    rows = db.execute(
        select(Message.to_address, Message.status, func.count())
        .where(
            Message.org_id == org_id,
            Message.direction == "outbound",
            Message.status.in_(("sent", "bounced")),
        )
        .group_by(Message.to_address, Message.status)
    ).all()
    stats: dict[str, list[int]] = {}
    for address, status, count in rows:
        pair = stats.setdefault(domain_of(address), [0, 0])
        pair[0 if status == "bounced" else 1] += int(count)
    out = {domain: (b, d) for domain, (b, d) in stats.items() if domain}
    _stats_cache[org_id] = (now + STATS_TTL_SECONDS, out)
    return out


def learned_risky(stats: dict[str, tuple[int, int]], domain: str) -> tuple[int, int] | None:
    bounced, delivered = stats.get(domain, (0, 0))
    total = bounced + delivered
    if bounced >= LEARN_MIN_BOUNCES and total and bounced / total >= LEARN_RATE:
        return bounced, total
    return None


def delivered_before(db: Session, org_id: str, address: str, now: datetime) -> bool:
    """Sent to this exact address a while ago, and it did not bounce."""
    from ..models import Message

    address = (address or "").strip().lower()
    rows = db.execute(
        select(Message.status, Message.sent_at).where(
            Message.org_id == org_id,
            Message.direction == "outbound",
            func.lower(Message.to_address) == address,
            Message.status.in_(("sent", "bounced")),
        )
    ).all()
    if any(status == "bounced" for status, _ in rows):
        return False
    cutoff = now - PROVEN_AFTER
    for _status, sent_at in rows:
        if sent_at is None:
            continue
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=cutoff.tzinfo)
        if sent_at <= cutoff:
            return True
    return False


# -- the verdict ----------------------------------------------------------------


def hard_verdict(address: str, *, lookup: Callable[[str], bool | None] | None = None) -> Verdict:
    """Facts about the domain itself; these override even a stored score."""
    domain = domain_of(address)
    if not domain or "." not in domain:
        return Verdict(UNDELIVERABLE, "not a valid email domain")
    if domain in TYPO_DOMAINS:
        return Verdict(UNDELIVERABLE, f"looks like a typo of {TYPO_DOMAINS[domain]}")
    if receives_mail(domain, lookup=lookup) is False:
        return Verdict(UNDELIVERABLE, f"{domain} cannot receive email")
    return OK


def soft_verdict(address: str, stats: dict[str, tuple[int, int]]) -> Verdict:
    """Guesses from the domain; a stored verifier score beats these."""
    domain = domain_of(address)
    if domain in LEGACY_DOMAINS:
        return Verdict(RISKY, f"old ISP address ({domain})")
    learned = learned_risky(stats, domain)
    if learned is not None:
        bounced, total = learned
        return Verdict(RISKY, f"{domain} bounced {bounced} of {total} sends")
    return OK


def make_checker(
    db: Session,
    org_id: str,
    now: datetime,
    *,
    lookup: Callable[[str], bool | None] | None = None,
) -> Callable[[str, int | None], Verdict]:
    """A per-send checker: (address, stored score) -> Verdict."""
    stats: dict[str, tuple[int, int]] | None = None

    def check(address: str, stored: int | None) -> Verdict:
        nonlocal stats
        hard = hard_verdict(address, lookup=lookup)
        if hard.score is not None:
            return hard
        if stored is not None:
            return Verdict(stored)
        if delivered_before(db, org_id, address, now):
            return OK
        if stats is None:
            stats = domain_bounce_stats(db, org_id)
        return soft_verdict(address, stats)

    return check


def reset_caches() -> None:
    _dns_cache.clear()
    _stats_cache.clear()
