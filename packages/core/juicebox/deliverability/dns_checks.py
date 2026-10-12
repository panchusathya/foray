"""DNS health of a sending domain: SPF, DMARC, DKIM key size, MX, blocklists.

Every answer has three outcomes, and keeping the third honest is the point:
``ok``/``clean``, a definite problem, or ``unknown``. A timeout is unknown, not
"no DMARC". A blocklist that refuses to answer our resolver (Spamhaus returns
127.255.255.x to public and cloud resolvers; SURBL returns 127.0.0.1 when it
is rate-limiting) is unknown, not "listed" -- reading those as listings would
send someone chasing a problem that does not exist.

Consumer domains (gmail.com, yahoo.com, ...) are skipped: their records belong
to Google and Yahoo, and a personal Gmail sender's reputation is the account's,
not the domain's.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable

from ..integrations.discovery_filters import PUBLIC_EMAIL_DOMAINS

DNS_TIMEOUT = 3.0
#: Selectors we try when nobody configured one. zmail is Zoho, google is
#: Workspace, selector1/2 are Microsoft 365.
DEFAULT_DKIM_SELECTORS = (
    "zmail",
    "google",
    "selector1",
    "selector2",
    "default",
    "k1",
    "s1",
    "dkim",
)
#: DKIM keys under this many bits are flagged; 2048 is the current norm.
STRONG_DKIM_BITS = 2048


class NoRecord(Exception):
    """A definite "there is no such record" (NXDOMAIN or no answer)."""


class LookupFailed(Exception):
    """We could not tell: timeout, SERVFAIL, no nameservers."""


Resolve = Callable[[str, str], list[str]]


def _default_resolve(name: str, rdtype: str) -> list[str]:
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    try:
        answer = resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        raise NoRecord(name) from None
    except dns.exception.DNSException as exc:
        raise LookupFailed(f"{type(exc).__name__} resolving {name}") from None
    out = []
    for record in answer:
        if rdtype == "TXT":
            out.append(b"".join(record.strings).decode("utf-8", errors="replace"))
        else:
            out.append(record.to_text())
    return out


def is_consumer_domain(domain: str) -> bool:
    return (domain or "").strip().lower() in PUBLIC_EMAIL_DOMAINS


def _txt(resolve: Resolve, name: str) -> list[str] | None:
    """TXT strings, [] when there are none, None when we could not tell."""
    try:
        return resolve(name, "TXT")
    except NoRecord:
        return []
    except LookupFailed:
        return None


def _mx(resolve: Resolve, domain: str) -> dict:
    try:
        records = resolve(domain, "MX")
    except NoRecord:
        return {"status": "missing"}
    except LookupFailed:
        return {"status": "unknown"}
    return {"status": "ok" if records else "missing", "records": records}


def _spf(resolve: Resolve, domain: str) -> dict:
    records = _txt(resolve, domain)
    if records is None:
        return {"status": "unknown", "record": "", "note": "lookup failed"}
    spf = [r for r in records if r.strip().lower().startswith("v=spf1")]
    if not spf:
        return {"status": "missing", "record": "", "note": "no SPF record"}
    if len(spf) > 1:
        return {
            "status": "invalid",
            "record": " | ".join(spf),
            "note": "more than one SPF record (receivers treat that as a permanent error)",
        }
    record = spf[0]
    terms = record.lower().split()
    if "+all" in terms or "all" in terms:  # a bare "all" qualifier defaults to +
        return {"status": "invalid", "record": record, "note": "+all lets anyone send as you"}
    return {"status": "ok", "record": record, "note": ""}


def _dmarc(resolve: Resolve, domain: str) -> dict:
    records = _txt(resolve, f"_dmarc.{domain}")
    if records is None:
        return {"status": "unknown", "policy": "", "record": ""}
    dmarc = [r for r in records if r.strip().lower().startswith("v=dmarc1")]
    if not dmarc:
        return {"status": "missing", "policy": "", "record": ""}
    record = dmarc[0]
    match = re.search(r"(?:^|;)\s*p\s*=\s*(\w+)", record, re.IGNORECASE)
    policy = match.group(1).lower() if match else ""
    status = "ok" if policy in {"quarantine", "reject"} else "weak"
    return {"status": status, "policy": policy, "record": record}


def _key_bits(record: str) -> int | None:
    match = re.search(r"(?:^|;)\s*p\s*=\s*([A-Za-z0-9+/=\s]+)", record)
    if not match:
        return None
    blob = "".join(match.group(1).split())
    if not blob:
        return None  # p= empty means the key was revoked
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        key = load_der_public_key(base64.b64decode(blob))
        return int(getattr(key, "key_size", 0)) or None
    except (ValueError, TypeError):
        return None


def _dkim(resolve: Resolve, domain: str, selectors) -> dict:
    found = []
    unknown = False
    for selector in selectors:
        records = _txt(resolve, f"{selector}._domainkey.{domain}")
        if records is None:
            unknown = True
            continue
        for record in records:
            if "p=" not in record.replace(" ", ""):
                continue
            found.append({"selector": selector, "bits": _key_bits(record)})
            break
    if not found:
        return {"status": "unknown" if unknown else "missing", "selectors": []}
    weakest = min((f["bits"] or 0) for f in found)
    return {"status": "ok" if weakest >= STRONG_DKIM_BITS else "weak", "selectors": found}


def _blocklists(resolve: Resolve, domain: str) -> list[dict]:
    out = []
    for name, zone, refused in (
        ("Spamhaus DBL", "dbl.spamhaus.org", lambda a: a.startswith("127.255.255.")),
        ("SURBL", "multi.surbl.org", lambda a: a == "127.0.0.1"),
    ):
        try:
            answers = resolve(f"{domain}.{zone}", "A")
        except NoRecord:
            out.append({"name": name, "status": "clean", "note": ""})
            continue
        except LookupFailed as exc:
            out.append({"name": name, "status": "unknown", "note": str(exc)})
            continue
        if not answers:
            out.append({"name": name, "status": "clean", "note": ""})
        elif all(refused(a) for a in answers):
            out.append({"name": name, "status": "unknown", "note": "the list refused our resolver"})
        else:
            out.append({"name": name, "status": "listed", "note": ", ".join(answers)})
    return out


def check_domain(domain: str, *, resolve: Resolve | None = None, selectors=None) -> dict:
    domain = (domain or "").strip().lower()
    resolve = resolve or _default_resolve
    selectors = list(selectors or DEFAULT_DKIM_SELECTORS)
    return {
        "domain": domain,
        "mx": _mx(resolve, domain),
        "spf": _spf(resolve, domain),
        "dmarc": _dmarc(resolve, domain),
        "dkim": _dkim(resolve, domain, selectors),
        "blocklists": _blocklists(resolve, domain),
    }


def run_check(domain: str) -> dict:
    """The production entry point: the module resolver, looked up at call time
    so a test can replace it."""
    from ..config import get_settings

    configured = [
        s.strip()
        for s in (get_settings().deliverability_dkim_selectors or "").split(",")
        if s.strip()
    ]
    return check_domain(domain, resolve=_default_resolve, selectors=configured or None)
