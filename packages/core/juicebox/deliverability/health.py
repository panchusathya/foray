"""One label per mailbox (and per domain) from what was measured.

Labels: ``in_spam``, ``at_risk``, ``healthy``, ``no_data``. Step 1 only shows
them -- nothing acts on a label yet. The thresholds are deliberately plain so
the reasons can say exactly which one tripped; step 2 tunes them against what
the seed tests show over the first few weeks.

"No data" outranks "healthy" (failure 21): fewer than three seed results and
fewer than thirty recently-emailed people is not evidence of anything.
"""

from __future__ import annotations

MIN_SEED_RESULTS = 3
MIN_PEOPLE = 30
MIN_PEOPLE_FOR_REPLY_COMPARISON = 40
SPAM_SHARE_IN_SPAM = 0.5
SPAM_SHARE_AT_RISK = 0.2
BOUNCE_AT_RISK = 0.03
# One bounce in twenty sends is 5%; a rate is only a signal over enough sends.
MIN_SENDS_FOR_BOUNCE = 50
REPLY_RATIO_AT_RISK = 0.5
BLOCKS_IN_SPAM = 2

RESULT_KEYS = ("inbox", "other_tab", "spam", "missing")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def mailbox_health(
    *,
    seed: dict,
    people_recent: int,
    reply_rate_recent: float | None,
    domain_reply_rate: float | None = None,
    bounce_rate: float | None = None,
    sends: int | None = None,
    blocks: int = 0,
) -> dict:
    results = sum(int(seed.get(k, 0)) for k in RESULT_KEYS)
    not_inbox = int(seed.get("spam", 0)) + int(seed.get("missing", 0))
    share = not_inbox / results if results else 0.0

    bad: list[str] = []
    risky: list[str] = []
    if blocks >= BLOCKS_IN_SPAM:
        bad.append(f"{blocks} bounces refused on policy or reputation in 14 days")
    elif blocks:
        risky.append("1 bounce refused on policy or reputation in 14 days")
    if results >= MIN_SEED_RESULTS:
        line = f"{not_inbox} of {results} seed tests not in the inbox (7 days)"
        if share >= SPAM_SHARE_IN_SPAM:
            bad.append(line)
        elif share >= SPAM_SHARE_AT_RISK:
            risky.append(line)
    enough_sends = sends is None or sends >= MIN_SENDS_FOR_BOUNCE
    if bounce_rate is not None and enough_sends and bounce_rate >= BOUNCE_AT_RISK:
        risky.append(f"bounce rate {_pct(bounce_rate)} over 14 days")
    if (
        people_recent >= MIN_PEOPLE_FOR_REPLY_COMPARISON
        and domain_reply_rate
        and reply_rate_recent is not None
        and reply_rate_recent < REPLY_RATIO_AT_RISK * domain_reply_rate
    ):
        risky.append(
            f"reply rate {_pct(reply_rate_recent)} vs {_pct(domain_reply_rate)} across the domain"
        )

    if bad:
        return {"label": "in_spam", "reasons": bad + risky}
    if risky:
        return {"label": "at_risk", "reasons": risky}
    if results < MIN_SEED_RESULTS and people_recent < MIN_PEOPLE:
        return {
            "label": "no_data",
            "reasons": [
                f"{results} seed result{'s' if results != 1 else ''} and "
                f"{people_recent} recently emailed people: not enough to judge"
            ],
        }
    return {"label": "healthy", "reasons": []}


def domain_health(mailbox_labels: list[str], checks: dict | None) -> dict:
    """The domain is as bad as its DNS, or as its worst-placed mailboxes.

    One mailbox in spam does not make a domain red; most of them do, because
    providers rate the domain and that is what "most mailboxes" is showing.
    """
    reasons: list[str] = []
    label = "healthy"
    if checks:
        listed = [b["name"] for b in checks.get("blocklists", []) if b.get("status") == "listed"]
        if listed:
            label = "in_spam"
            reasons.append(f"listed on {', '.join(listed)}")
        for key, name in (("spf", "SPF"), ("dmarc", "DMARC"), ("dkim", "DKIM")):
            status = (checks.get(key) or {}).get("status")
            if status in {"missing", "invalid"}:
                label = "in_spam" if label == "in_spam" else "at_risk"
                reasons.append(f"{name} {status}")
            elif status == "weak":
                if label == "healthy":
                    label = "at_risk"
                reasons.append(f"{name} weak")
    judged = [lbl for lbl in mailbox_labels if lbl != "no_data"]
    if judged:
        in_spam = sum(1 for lbl in judged if lbl == "in_spam")
        at_risk = sum(1 for lbl in judged if lbl == "at_risk")
        if in_spam * 2 >= len(judged):
            label = "in_spam"
            reasons.append(f"{in_spam} of {len(judged)} mailboxes landing in spam")
        elif (in_spam + at_risk) * 2 >= len(judged):
            if label == "healthy":
                label = "at_risk"
            reasons.append(f"{in_spam + at_risk} of {len(judged)} mailboxes at risk or worse")
    elif label == "healthy":
        return {"label": "no_data", "reasons": ["no mailbox has enough data yet"]}
    return {"label": label, "reasons": reasons}
