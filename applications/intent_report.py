#!/usr/bin/env python3
"""What intent have we actually captured? — the numbers the applications need.

Run against the production database. Nothing is written; this only reads.

    python applications/intent_report.py                 # headline + per-field
    python applications/intent_report.py --org foray     # if you have >1 org
    python applications/intent_report.py --samples 5     # + anonymised quotes

Why this exists
---------------
The positioning is "we capture intent." The applications need a number behind
that, and it already exists in the database -- ``candidate_preference_facts``
carries, per field, who said it, which call or message it came from, whether it
was a **direct candidate statement**, and when it was captured.

That last set of columns is the whole thesis in schema form: provenance,
consent and freshness on every fact. This script just counts it.

The headline number to use is **direct candidate statements** -- things a person
told us in their own words, not something inferred. That is the number nobody
else in hiring can produce.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "core"))

from sqlalchemy import func, select  # noqa: E402

from juicebox.db import session_scope  # noqa: E402
from juicebox.models import (  # noqa: E402
    Candidate,
    CandidatePreference,
    CandidatePreferenceFact,
    Org,
    utcnow,
)

# Fields that are statements of *intent* rather than of fact. A job title is a
# profile attribute; a comp floor and a no-go list are not, and only the second
# kind is unobtainable anywhere else.
INTENT_FIELDS = {
    "timeline": "when they'd move",
    "compensation": "comp expectations",
    "employment_status": "employed or looking",
    "reason_for_looking": "why they'd leave",
    "urgency_driver": "what's forcing the timing",
    "offers_in_hand": "competing offers",
    "active_processes": "processes already running",
    "spec.excluded_companies": "no-go list",
    "spec.target_companies": "where they want to go",
    "spec.company_stages": "stage preference",
    "equity_importance": "equity vs cash",
    "willing_to_relocate": "relocation",
    "confidential_items": "explicitly confidential",
    "ok_to_share_with_clients": "consent to share",
}


def resolve_org(db, slug: str | None) -> tuple[str, str]:
    if slug:
        org = db.get(Org, slug) or db.scalar(select(Org).where(Org.slug == slug))
        if org is None:
            raise SystemExit(f"org not found: {slug}")
        return org.id, org.slug
    orgs = db.scalars(select(Org).order_by(Org.created_at)).all()
    if not orgs:
        raise SystemExit("no orgs in this database")
    if len(orgs) > 1:
        raise SystemExit("multiple orgs, pass --org: " + ", ".join(o.slug for o in orgs))
    return orgs[0].id, orgs[0].slug


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", default=None)
    ap.add_argument("--samples", type=int, default=0, help="print N anonymised examples")
    args = ap.parse_args()

    with session_scope() as db:
        org_id, org_slug = resolve_org(db, args.org)

        facts = db.scalars(
            select(CandidatePreferenceFact)
            .where(CandidatePreferenceFact.org_id == org_id)
            .where(CandidatePreferenceFact.declined.is_(False))
        ).all()

        people_with_prefs = db.scalar(
            select(func.count(func.distinct(CandidatePreference.candidate_id))).where(
                CandidatePreference.org_id == org_id
            )
        )

        intent_facts = [f for f in facts if f.field_key in INTENT_FIELDS]
        direct = [f for f in intent_facts if f.direct_candidate_statement]
        direct_people = {f.candidate_id for f in direct}
        confidential = [f for f in direct if f.field_key == "confidential_items"]
        consented = [f for f in direct if f.field_key == "ok_to_share_with_clients"]

        now = utcnow()
        fresh_90 = [f for f in direct if f.captured_at and now - f.captured_at < timedelta(days=90)]

        print(f"\n  Intent captured — org {org_slug}\n" + "  " + "─" * 58)
        print(f"  {people_with_prefs:>6}  candidates with a preference record")
        print(f"  {len(direct_people):>6}  people who stated intent in their own words")
        print(f"  {len(direct):>6}  direct intent statements")
        print(f"  {len(intent_facts) - len(direct):>6}  inferred (do not claim these)")
        print(f"  {len(fresh_90):>6}  still fresh (captured in the last 90 days)")
        print(f"  {len(confidential):>6}  marked explicitly confidential")
        print(f"  {len(consented):>6}  with consent to share")

        by_field = Counter(f.field_key for f in direct)
        if by_field:
            print("\n  By field — direct statements only\n  " + "─" * 58)
            width = max(len(k) for k in by_field)
            for key, n in by_field.most_common():
                people = len({f.candidate_id for f in direct if f.field_key == key})
                print(f"  {key:<{width}}  {n:>4} statements  {people:>4} people   {INTENT_FIELDS.get(key, '')}")

        sourced = Counter(
            "voice/text call" if f.source_call_id else
            "email thread" if f.source_thread_id or f.source_message_id else
            "other"
            for f in direct
        )
        if sourced:
            print("\n  Where it came from\n  " + "─" * 58)
            for src, n in sourced.most_common():
                print(f"  {src:<20} {n:>5}")

        if args.samples and direct:
            print("\n  Sample statements — ANONYMISE BEFORE USING\n  " + "─" * 58)
            seen: set[str] = set()
            shown = 0
            for f in sorted(direct, key=lambda x: x.captured_at or now, reverse=True):
                if f.candidate_id in seen:
                    continue
                seen.add(f.candidate_id)
                cand = db.get(Candidate, f.candidate_id)
                title = (getattr(cand, "current_title", "") or "a candidate") if cand else "a candidate"
                print(f"  · [{f.field_key}] {title} — {f.value_revision or '(see record)'}")
                shown += 1
                if shown >= args.samples:
                    break

        print("\n  For the application\n  " + "─" * 58)
        if direct_people:
            print(f"  \"{len(direct_people)} people have told us, in their own words, what would")
            print(f"   actually move them — {len(direct)} distinct statements about timing, comp,")
            print("   competing offers and where they will and won't go. None of it is")
            print("   published anywhere, and none of it can be scraped.\"")
        else:
            print("  Nothing captured yet. Run the intake agent, or mine the Zoho corpus —")
            print("  see applications/traction-inventory.md.")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
