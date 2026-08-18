"""Operational CLI: ``juicebox <command>``.

    juicebox init-db
    juicebox seed [--count 120]
    juicebox import <file.jsonl> --org <org_id>
    juicebox search "senior ML engineers in SF" --org <org_id>
    juicebox run-outreach --org <org_id> [--dry-run]
    juicebox run-agents --org <org_id>
    juicebox reembed --org <org_id>
    juicebox export-seed <file.jsonl> [--count 120]
    juicebox serve [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from .db import create_all, session_scope
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
    create_all()
    print("schema created")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .seed import seed_demo

    create_all()
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
    create_all()
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


def cmd_reembed(args: argparse.Namespace) -> int:
    with session_scope() as db:
        org_id = _resolve_org(db, args.org)
        count = reembed_org(db, org_id=org_id)
    print(f"re-embedded {count} candidates")
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

    sub.add_parser("init-db", help="create tables").set_defaults(func=cmd_init_db)

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

    reembed = sub.add_parser("reembed", help="recompute candidate embeddings")
    reembed.add_argument("--org", default=None)
    reembed.set_defaults(func=cmd_reembed)

    export = sub.add_parser("export-seed", help="write the generated corpus to jsonl")
    export.add_argument("file")
    export.add_argument("--count", type=int, default=120)
    export.set_defaults(func=cmd_export_seed)

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
