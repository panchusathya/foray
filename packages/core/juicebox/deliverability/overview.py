"""Everything the Deliverability page shows, assembled in one read.

Grouped by sending domain because that is the unit mail providers rate: twenty
goforay.io mailboxes share one reputation, while each personal Gmail sender
stands alone (and gets no domain checks -- gmail.com is Google's).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import DomainCheck, MailboxConnection, Org, SeedInbox, SeedTest
from ..outreach.rotation import domain_of
from .dns_checks import is_consumer_domain, run_check
from .health import domain_health, mailbox_health
from .signals import MailboxSignals, mailbox_signals, recent_weeks
from .warmup import effective_cap, warmup_state

SEED_WINDOW = timedelta(days=7)
DOMAIN_CHECK_MAX_AGE = timedelta(hours=24)
RECENT_TESTS = 30
_SEED_KEYS = ("inbox", "other_tab", "spam", "missing")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def sending_mailboxes(db: Session, org_id: str) -> list[MailboxConnection]:
    return list(
        db.scalars(
            select(MailboxConnection)
            .where(MailboxConnection.org_id == org_id, MailboxConnection.status != "revoked")
            .order_by(MailboxConnection.created_at, MailboxConnection.address)
        )
    )


def custom_domains(db: Session, org_id: str) -> list[str]:
    domains = {domain_of(m.address) for m in sending_mailboxes(db, org_id)}
    return sorted(d for d in domains if d and not is_consumer_domain(d))


def stale_domains(db: Session, org_id: str, now: datetime) -> list[str]:
    checked = {
        row.domain: _aware(row.checked_at)
        for row in db.scalars(select(DomainCheck).where(DomainCheck.org_id == org_id))
    }
    return [
        d
        for d in custom_domains(db, org_id)
        if checked.get(d) is None or now - checked[d] >= DOMAIN_CHECK_MAX_AGE
    ]


def refresh_domain(db: Session, org_id: str, domain: str, now: datetime) -> DomainCheck:
    result = run_check(domain)
    row = db.scalar(
        select(DomainCheck).where(DomainCheck.org_id == org_id, DomainCheck.domain == domain)
    )
    if row is None:
        row = DomainCheck(org_id=org_id, domain=domain)
        db.add(row)
    row.result = result
    row.checked_at = now
    db.flush()
    return row


def _seed_counts(db: Session, org_id: str, now: datetime) -> dict[str, dict[str, int]]:
    rows = db.execute(
        select(SeedTest.mailbox_id, SeedTest.status, func.count(SeedTest.id))
        .where(SeedTest.org_id == org_id, SeedTest.sent_at >= now - SEED_WINDOW)
        .group_by(SeedTest.mailbox_id, SeedTest.status)
    ).all()
    out: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys((*_SEED_KEYS, "pending"), 0))
    for mailbox_id, status, count in rows:
        key = "pending" if status == "sent" else status
        if key in out[mailbox_id]:
            out[mailbox_id][key] += count
    return out


def _weekly(signals: MailboxSignals, weeks: list[str]) -> list[dict]:
    return [
        {
            "week": w,
            "people": signals.weekly.get(w, [0, 0])[0],
            "replied": signals.weekly.get(w, [0, 0])[1],
        }
        for w in weeks
    ]


def _test_out(test: SeedTest, mailboxes: dict, seeds: dict) -> dict:
    return {
        "id": test.id,
        "mailbox_id": test.mailbox_id,
        "mailbox_address": mailboxes.get(test.mailbox_id, ""),
        "seed_address": seeds.get(test.seed_id, ("", ""))[0],
        "seed_provider": seeds.get(test.seed_id, ("", ""))[1],
        "status": test.status,
        "folder": test.folder,
        "subject": test.subject,
        "error": test.error,
        "sent_at": _iso(test.sent_at),
        "checked_at": _iso(test.checked_at),
        "created_at": _iso(test.created_at),
    }


def seed_out(seed: SeedInbox, tests_7d: int = 0) -> dict:
    return {
        "id": seed.id,
        "address": seed.address,
        "kind": seed.kind,
        "provider_label": seed.provider_label,
        "imap_host": seed.imap_host,
        "imap_port": seed.imap_port,
        "status": seed.status,
        "error": seed.error,
        "spam_visible": seed.spam_visible,
        "last_checked_at": _iso(seed.last_checked_at),
        "tests_7d": tests_7d,
    }


def build_overview(db: Session, org: Org, now: datetime) -> dict:
    mailboxes = sending_mailboxes(db, org.id)
    signals = mailbox_signals(db, org.id, now)
    counts = _seed_counts(db, org.id, now)
    weeks = recent_weeks(now)
    seeds = list(
        db.scalars(select(SeedInbox).where(SeedInbox.org_id == org.id).order_by(SeedInbox.address))
    )
    seed_tests_7d = dict(
        db.execute(
            select(SeedTest.seed_id, func.count(SeedTest.id))
            .where(SeedTest.org_id == org.id, SeedTest.sent_at >= now - SEED_WINDOW)
            .group_by(SeedTest.seed_id)
        ).all()
    )
    checks = {
        row.domain: row
        for row in db.scalars(select(DomainCheck).where(DomainCheck.org_id == org.id))
    }

    last_tests: dict[str, SeedTest] = {}
    for test in db.scalars(
        select(SeedTest)
        .where(SeedTest.org_id == org.id)
        .order_by(SeedTest.created_at.desc())
        .limit(500)
    ):
        last_tests.setdefault(test.mailbox_id, test)

    seed_names = {s.id: (s.address, s.provider_label) for s in seeds}
    mailbox_names = {m.id: m.address for m in mailboxes}

    by_domain: dict[str, list[MailboxConnection]] = defaultdict(list)
    for mailbox in mailboxes:
        by_domain[domain_of(mailbox.address)].append(mailbox)

    domains = []
    for domain in sorted(by_domain, key=lambda d: (is_consumer_domain(d), d)):
        rows = by_domain[domain]
        total = MailboxSignals()
        seed_total = dict.fromkeys((*_SEED_KEYS, "pending"), 0)
        for mailbox in rows:
            total.add(signals.get(mailbox.id, MailboxSignals()))
            for key, value in counts.get(mailbox.id, {}).items():
                seed_total[key] += value
        consumer = is_consumer_domain(domain)
        domain_rate = total.reply_rate_recent

        out_rows = []
        for mailbox in rows:
            sig = signals.get(mailbox.id, MailboxSignals())
            seed = dict(counts.get(mailbox.id, dict.fromkeys((*_SEED_KEYS, "pending"), 0)))
            health = mailbox_health(
                seed=seed,
                people_recent=sig.people_recent,
                reply_rate_recent=sig.reply_rate_recent,
                # A consumer "domain" is unrelated accounts; comparing one
                # Gmail sender with another says nothing.
                domain_reply_rate=None if consumer else domain_rate,
                bounce_rate=sig.bounce_rate_14d,
                sends=sig.sends_14d,
                blocks=sig.blocks_14d,
            )
            last = last_tests.get(mailbox.id)
            out_rows.append(
                {
                    "id": mailbox.id,
                    "address": mailbox.address,
                    "provider": mailbox.provider,
                    "status": mailbox.status,
                    "send_enabled": mailbox.send_enabled,
                    "daily_send_cap": mailbox.daily_send_cap,
                    "effective_cap": effective_cap(mailbox, now),
                    "warmup": warmup_state(mailbox, now),
                    "connected_at": _iso(mailbox.created_at),
                    "health": health,
                    "seed": seed,
                    "people_recent": sig.people_recent,
                    "replied_recent": sig.replied_recent,
                    "reply_rate_recent": sig.reply_rate_recent,
                    "sends_14d": sig.sends_14d,
                    "bounce_rate_14d": sig.bounce_rate_14d,
                    "blocks_14d": sig.blocks_14d,
                    "weekly": _weekly(sig, weeks),
                    "last_test": _test_out(last, mailbox_names, seed_names) if last else None,
                }
            )
        check = checks.get(domain)
        domain_checks = None if consumer else (check.result if check else None)
        if consumer:
            health = {"label": "no_data", "reasons": ["personal accounts: rated one by one"]}
            labels = [r["health"]["label"] for r in out_rows]
            if any(lbl != "no_data" for lbl in labels):
                health = {"label": "mixed", "reasons": ["each personal account stands alone"]}
        else:
            health = domain_health([r["health"]["label"] for r in out_rows], domain_checks)
        domains.append(
            {
                "domain": domain,
                "consumer": consumer,
                "health": health,
                "checks": domain_checks,
                "checked_at": None if consumer or check is None else _iso(check.checked_at),
                "seed": seed_total,
                "people_recent": total.people_recent,
                "replied_recent": total.replied_recent,
                "reply_rate_recent": domain_rate,
                "bounce_rate_14d": total.bounce_rate_14d,
                "blocks_14d": total.blocks_14d,
                "weekly": _weekly(total, weeks),
                "mailboxes": out_rows,
            }
        )

    recent = list(
        db.scalars(
            select(SeedTest)
            .where(SeedTest.org_id == org.id)
            .order_by(SeedTest.created_at.desc())
            .limit(RECENT_TESTS)
        )
    )
    return {
        "settings": {"seed_test_interval_hours": org.seed_test_interval_hours},
        "seeds": [seed_out(s, seed_tests_7d.get(s.id, 0)) for s in seeds],
        "domains": domains,
        "recent_tests": [_test_out(t, mailbox_names, seed_names) for t in recent],
        "weeks": weeks,
    }
