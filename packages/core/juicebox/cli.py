"""Operational CLI: ``juicebox <command>``.

    juicebox init-db
    juicebox migrate [revision] [--down]
    juicebox seed [--count 120]
    juicebox import <file.jsonl> --org <org_id>
    juicebox search "senior ML engineers in SF" --org <org_id>
    juicebox run-outreach --org <org_id> [--dry-run]
    juicebox run-agents --org <org_id>
    juicebox work [--once]
    juicebox backfill-jobs [--org]
    juicebox reembed --org <org_id>
    juicebox export-seed <file.jsonl> [--count 120]
    juicebox purge-placeholder-emails [--apply]
    juicebox serve [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from .db import session_scope
from .migrate import ensure_schema
from .models import Org
from .repository import reembed_org, upsert_candidates


def _resolve_org(db, org: str | None) -> str:
    if org:
        found = db.get(Org, org) or db.scalar(select(Org).where(Org.slug == org))
        if found is None:
            raise SystemExit(f"org not found: {org}")
        return found.id
    orgs = db.scalars(select(Org).order_by(Org.created_at)).all()
    if not orgs:
        raise SystemExit("no orgs yet — run `juicebox seed` first")
    if len(orgs) > 1:
        names = ", ".join(f"{o.slug}({o.id})" for o in orgs)
        raise SystemExit(f"multiple orgs, pass --org: {names}")
    return orgs[0].id


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"skipping line {line_number}: {exc}", file=sys.stderr)
    return records


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init_db(_args: argparse.Namespace) -> int:
    from .migrate import current_revision

    how = ensure_schema()
    print(f"schema ready via {how} (revision {current_revision()})")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from . import migrate

    if args.down:
        migrate.downgrade(args.revision)
    else:
        migrate.upgrade(args.revision)
    print(f"database at revision {migrate.current_revision()}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .seed import seed_demo

    ensure_schema()
    with session_scope() as db:
        info = seed_demo(db, candidate_count=args.count)
    print(json.dumps(info, indent=2))
    print(f"\nlog in with {info['user_email']} / {info['password']}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    records = _read_jsonl(path)
    ensure_schema()
    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        result = upsert_candidates(db, org_id=org_id, payloads=records)
    print(f"created={result.created} updated={result.updated}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .search.engine import get_engine

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        outcome = get_engine().search(
            db,
            org_id=org_id,
            query=args.query,
            limit=args.limit,
            persist=False,
            use_rerank=args.rerank,
        )
        print(
            f"parser={outcome.parser} reranked={outcome.reranked} "
            f"matched={outcome.total_matched} in {outcome.latency_ms}ms\n"
        )
        for hit in outcome.results:
            doc = hit.doc
            print(
                f"{hit.rank:>3}. {doc.full_name:<24} {hit.final_score:.3f}  "
                f"{doc.current_title} @ {doc.current_company}"
            )
            if args.verbose:
                for criterion in hit.criteria:
                    mark = {"strong": "+", "partial": "~", "miss": "-"}.get(criterion.status, "?")
                    print(f"       {mark} {criterion.label}: {criterion.evidence}")
    return 0


def cmd_run_outreach(args: argparse.Namespace) -> int:
    from .outreach.sequencer import run_due

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        summary = run_due(db, org_id=org_id, dry_run=args.dry_run)
    print(
        f"processed={summary.processed} sent={summary.sent} failed={summary.failed} "
        f"completed={summary.completed} skipped={summary.skipped}"
    )
    return 0


def cmd_run_agents(args: argparse.Namespace) -> int:
    from .agents.sourcing import run_due_agents

    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        runs = run_due_agents(db, org_id=org_id)
    if not runs:
        print("no agents due")
    for run in runs:
        print(f"{run.agent_id}: evaluated={run.evaluated} added={run.added} enrolled={run.enrolled}")
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    from .worker import drain, run_loop

    if args.once:
        with session_scope() as db:
            processed = drain(db)
        print(f"processed={processed}")
        return 0
    run_loop()
    return 0


def cmd_backfill_jobs(args: argparse.Namespace) -> int:
    from .jobmatch.sync import backfill_project_postings

    ensure_schema()
    with session_scope() as db:
        org_id = _resolve_org(db, args.org) if args.org else None
        count = backfill_project_postings(db, org_id=org_id)
    print(f"upserted {count} project postings")
    return 0


def cmd_reembed(args: argparse.Namespace) -> int:
    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        count = reembed_org(db, org_id=org_id)
    print(f"re-embedded {count} candidates (and any job postings)")
    return 0


def cmd_export_seed(args: argparse.Namespace) -> int:
    from .seed import generate_candidates

    path = Path(args.file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in generate_candidates(args.count):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"wrote {args.count} candidates to {path}")
    return 0


def cmd_purge_placeholder_emails(args: argparse.Namespace) -> int:
    """Strip filler addresses out of candidates that already have them.

    Cleanup for a provider incident: ContactOut answered HTTP 200 with a sample
    profile once its quota ran out, so email1@example.com / email2@gmail.com
    reached real candidate records. Goes through patch_candidate rather than
    writing the column directly, so the candidate_emails projection and the
    search index stay consistent -- the projection is a subset of the JSON and
    drifts silently otherwise.

    Defaults to a dry run. Nothing is written without --apply.
    """
    from .enrich.normalize import strip_placeholders
    from .models import Candidate
    from .repository import sync_candidate_emails
    from .search.index import invalidate

    changed = 0
    removed = 0
    with session_scope() as db:
        candidates = db.scalars(select(Candidate)).all()
        touched_orgs = set()
        for candidate in candidates:
            keep, drop = strip_placeholders(candidate.emails)
            if not drop:
                continue
            changed += 1
            removed += len(drop)
            print(
                f"{'apply' if args.apply else 'would drop'}  {candidate.id}  "
                f"{candidate.full_name}: {', '.join(drop)}"
                + (f"  (keeping {', '.join(keep)})" if keep else "  (leaving no address)")
            )
            if args.apply:
                candidate.emails = keep
                # The projection is a strict subset of this column and drifts
                # silently if it is not rebuilt. Deliberately NOT going through
                # patch_candidate: that records the field as a manual edit, which
                # would block a later real enrichment from filling the gap.
                sync_candidate_emails(db, candidate)
                touched_orgs.add(candidate.org_id)
        if args.apply:
            for org_id in touched_orgs:
                invalidate(org_id)

    verb = "removed" if args.apply else "would remove"
    print(f"\n{verb} {removed} address(es) from {changed} candidate(s)")
    if not args.apply and changed:
        print("re-run with --apply to write the change")
    return 0


def cmd_intake_number(args: argparse.Namespace) -> int:
    """Register a phone number against an org, or list what is registered.

    Without a row here every inbound call is refused with a 404, because the
    webhook resolves the org from the number that was dialled and refuses to
    guess. This is the one step that cannot be scripted from the provider side.
    """
    from .intake.phones import lookup_key
    from .models import IntakeNumber

    with session_scope() as db:
        if args.list:
            rows = db.scalars(select(IntakeNumber)).all()
            if not rows:
                print("no numbers registered")
                return 0
            for row in rows:
                state = "active" if row.active else "disabled"
                print(
                    f"{row.phone}  org={row.org_id}  {row.provider}  {state}  "
                    f"in={row.inbound_agent_id or '-'}  out={row.outbound_agent_id or '-'}"
                )
            return 0

        key = lookup_key(args.number)
        if not key:
            print(
                f"cannot normalise {args.number!r} to E.164. Give the full "
                "international form, e.g. +16283865454."
            )
            return 2

        org_id = _resolve_org(db, args.org)
        existing = db.scalar(select(IntakeNumber).where(IntakeNumber.phone == key))
        if existing is not None and existing.org_id != org_id:
            # One number, one org. Reassigning silently would hand another
            # workspace's calls -- and their transcripts -- to this one.
            print(
                f"{key} is already registered to org {existing.org_id}. "
                "Numbers belong to exactly one org; remove it there first."
            )
            return 2

        row = existing or IntakeNumber(org_id=org_id, phone=key)
        row.provider = args.provider
        row.label = args.label or row.label
        row.active = not args.disable
        if args.inbound_agent:
            row.inbound_agent_id = args.inbound_agent
        if args.outbound_agent:
            row.outbound_agent_id = args.outbound_agent
        db.add(row)
        db.flush()
        verb = "updated" if existing is not None else "registered"
        print(f"{verb} {key} -> org {org_id} ({'active' if row.active else 'disabled'})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "juicebox.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="juicebox", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="bring the schema to head").set_defaults(func=cmd_init_db)

    migrate_cmd = sub.add_parser("migrate", help="run database migrations")
    migrate_cmd.add_argument("revision", nargs="?", default="head")
    migrate_cmd.add_argument(
        "--down", action="store_true", help="downgrade to the given revision instead"
    )
    migrate_cmd.set_defaults(func=cmd_migrate)

    seed = sub.add_parser("seed", help="create the demo workspace")
    seed.add_argument("--count", type=int, default=120)
    seed.set_defaults(func=cmd_seed)

    imp = sub.add_parser("import", help="import candidates from a .jsonl file")
    imp.add_argument("file")
    imp.add_argument("--org", default=None)
    imp.set_defaults(func=cmd_import)

    search = sub.add_parser("search", help="run a natural-language search")
    search.add_argument("query")
    search.add_argument("--org", default=None)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--verbose", action="store_true")
    search.add_argument(
        "--rerank",
        dest="rerank",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="force LLM reranking on or off",
    )
    search.set_defaults(func=cmd_search)

    outreach = sub.add_parser("run-outreach", help="send every due sequence step")
    outreach.add_argument("--org", default=None)
    outreach.add_argument("--dry-run", action="store_true")
    outreach.set_defaults(func=cmd_run_outreach)

    agents = sub.add_parser("run-agents", help="run every due sourcing agent")
    agents.add_argument("--org", default=None)
    agents.set_defaults(func=cmd_run_agents)

    intake_number = sub.add_parser(
        "intake-number", help="register a voice intake phone number against an org"
    )
    intake_number.add_argument("number", nargs="?", default="", help="E.164, e.g. +16283865454")
    intake_number.add_argument("--org", default="")
    intake_number.add_argument("--provider", default="retell")
    intake_number.add_argument("--label", default="")
    intake_number.add_argument("--inbound-agent", default="")
    intake_number.add_argument("--outbound-agent", default="")
    intake_number.add_argument("--disable", action="store_true", help="keep the row, stop taking calls")
    intake_number.add_argument("--list", action="store_true", help="show registered numbers")
    intake_number.set_defaults(func=cmd_intake_number)

    work = sub.add_parser("work", help="claim and run jobs from the durable queue")
    work.add_argument(
        "--once",
        action="store_true",
        help="drain due work and exit (used by make worker)",
    )
    work.set_defaults(func=cmd_work)

    backfill = sub.add_parser(
        "backfill-jobs", help="upsert a JobPosting for every Project mandate"
    )
    backfill.add_argument("--org", default=None)
    backfill.set_defaults(func=cmd_backfill_jobs)

    reembed = sub.add_parser("reembed", help="recompute candidate embeddings")
    reembed.add_argument("--org", default=None)
    reembed.set_defaults(func=cmd_reembed)

    export = sub.add_parser("export-seed", help="write the generated corpus to jsonl")
    export.add_argument("file")
    export.add_argument("--count", type=int, default=120)
    export.set_defaults(func=cmd_export_seed)

    purge = sub.add_parser(
        "purge-placeholder-emails",
        help="strip filler addresses (email1@example.com and friends) from candidates",
    )
    purge.add_argument(
        "--apply", action="store_true", help="write the change (default is a dry run)"
    )
    purge.set_defaults(func=cmd_purge_placeholder_emails)

    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
