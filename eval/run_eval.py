#!/usr/bin/env python
"""Offline evaluation of the search engine.

Builds an isolated corpus from the deterministic seed generator, labels it with
the rules in ``queries.yaml``, then scores the engine's ranking against those
labels.

    python eval/run_eval.py                 # heuristic parser, no model calls
    python eval/run_eval.py --rerank        # include the LLM reranker
    python eval/run_eval.py --json report.json

Reported metrics
    parser_ok   did the parser extract what the query obviously states
    P@k         share of the top k that are relevant
    R@10        share of all relevant people that made the top 10
    MRR         1 / rank of the first relevant result
    nDCG@10     rank-weighted gain, the headline ranking-quality number
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "core"))


def bootstrap(corpus_size: int):
    """Fresh throwaway database so an eval run never touches real data."""
    import os

    tmp = Path(tempfile.mkdtemp(prefix="juicebox-eval-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp / 'eval.db').as_posix()}"
    os.environ.setdefault("EMBEDDING_PROVIDER", "hashing")

    from juicebox.config import reset_settings_cache
    from juicebox.db import create_all, session_scope
    from juicebox.models import Org
    from juicebox.repository import upsert_candidates
    from juicebox.seed import generate_candidates

    reset_settings_cache()
    create_all()
    with session_scope() as db:
        org = Org(name="Eval", slug="eval")
        db.add(org)
        db.flush()
        upsert_candidates(db, org_id=org.id, payloads=generate_candidates(corpus_size))
        org_id = org.id
    return org_id


# --------------------------------------------------------------------------
# labelling
# --------------------------------------------------------------------------


def is_relevant(doc, rule: dict) -> bool:
    from juicebox.enrich import taxonomy as tx

    family = rule.get("title_family")
    if family:
        titles = [doc.current_title_normalized, *doc.titles_held]
        if not any(tx.title_similarity(title, family) >= 0.95 for title in titles if title):
            return False

    seniority = rule.get("seniority")
    if seniority and doc.seniority not in seniority:
        return False

    location = rule.get("location")
    if location and tx.location_match(doc.location_parts, location) < 0.75:
        return False

    skills_all = {tx.normalize_skill(s) for s in rule.get("skills_all", [])}
    if skills_all and not skills_all <= doc.skills:
        return False

    skills_any = {tx.normalize_skill(s) for s in rule.get("skills_any", [])}
    if skills_any and not (skills_any & doc.skills):
        return False

    industry_any = {tx.normalize_industry(i) for i in rule.get("industry_any", [])}
    if industry_any and not (industry_any & doc.industries):
        return False

    stages = {tx.normalize_company_stage(s) for s in rule.get("company_stage_any", [])}
    if stages:
        held = {entry.get("company_stage", "") for entry in doc.companies}
        if not (stages & held):
            return False

    if "min_years" in rule and doc.years_experience < rule["min_years"]:
        return False
    return not ("max_years" in rule and doc.years_experience > rule["max_years"])


def check_parser(parsed, expectations: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not expectations:
        return True, problems

    payload = parsed.to_dict()
    for key, expected in expectations.items():
        if key.endswith("_include"):
            field = key[: -len("_include")]
            actual = set(payload.get(field) or [])
            missing = set(expected) - actual
            if missing:
                problems.append(f"{field} missing {sorted(missing)}")
        elif isinstance(expected, list):
            actual = payload.get(key) or []
            missing = [item for item in expected if item not in actual]
            if missing:
                problems.append(f"{key} missing {missing} (got {actual})")
        else:
            actual = payload.get(key)
            if actual != expected:
                problems.append(f"{key}={actual!r} expected {expected!r}")
    return not problems, problems


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


def precision_at_k(labels: list[bool], k: int) -> float:
    window = labels[:k]
    return sum(window) / len(window) if window else 0.0


def recall_at_k(labels: list[bool], total_relevant: int, k: int) -> float:
    return sum(labels[:k]) / total_relevant if total_relevant else 0.0


def mrr(labels: list[bool]) -> float:
    for index, relevant in enumerate(labels, start=1):
        if relevant:
            return 1.0 / index
    return 0.0


def ndcg_at_k(labels: list[bool], total_relevant: int, k: int) -> float:
    dcg = sum(1.0 / math.log2(index + 1) for index, hit in enumerate(labels[:k], start=1) if hit)
    ideal_hits = min(total_relevant, k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def run(corpus_size: int, use_rerank: bool, limit: int) -> dict:
    org_id = bootstrap(corpus_size)

    from juicebox.db import session_scope
    from juicebox.search.engine import get_engine
    from juicebox.search.index import get_index

    spec = yaml.safe_load((Path(__file__).parent / "queries.yaml").read_text(encoding="utf-8"))
    rows: list[dict] = []

    with session_scope() as db:
        index = get_index(db, org_id)
        engine = get_engine()

        for case in spec["queries"]:
            relevant_ids = {doc.id for doc in index.docs if is_relevant(doc, case["relevant"])}

            outcome = engine.search(
                db,
                org_id=org_id,
                query=case["query"],
                limit=limit,
                use_rerank=use_rerank,
                persist=False,
            )
            parser_ok, problems = check_parser(outcome.parsed, case.get("expect_parsed", {}))
            labels = [hit.doc.id in relevant_ids for hit in outcome.results]

            rows.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "parser": outcome.parser,
                    "parser_ok": parser_ok,
                    "parser_problems": problems,
                    "relevant_in_corpus": len(relevant_ids),
                    # A query with no relevant profiles measures nothing about
                    # ranking, so it is reported but kept out of the means.
                    "skipped": len(relevant_ids) == 0,
                    "returned": len(labels),
                    "p_at_5": round(precision_at_k(labels, 5), 3),
                    "p_at_10": round(precision_at_k(labels, 10), 3),
                    "r_at_10": round(recall_at_k(labels, len(relevant_ids), 10), 3),
                    "mrr": round(mrr(labels), 3),
                    "ndcg_at_10": round(ndcg_at_k(labels, len(relevant_ids), 10), 3),
                    "latency_ms": outcome.latency_ms,
                    "reranked": outcome.reranked,
                }
            )

    scored = [row for row in rows if not row["skipped"]]

    def mean(key: str) -> float:
        values = [row[key] for row in scored if isinstance(row[key], (int, float))]
        return round(statistics.mean(values), 3) if values else 0.0

    return {
        "corpus_size": len(index.docs),
        "queries": len(rows),
        "scored_queries": len(scored),
        "skipped_queries": [row["id"] for row in rows if row["skipped"]],
        "summary": {
            "parser_accuracy": round(sum(r["parser_ok"] for r in rows) / len(rows), 3),
            "p_at_5": mean("p_at_5"),
            "p_at_10": mean("p_at_10"),
            "r_at_10": mean("r_at_10"),
            "mrr": mean("mrr"),
            "ndcg_at_10": mean("ndcg_at_10"),
            "median_latency_ms": round(statistics.median(r["latency_ms"] for r in rows), 1),
            "reranked": any(r["reranked"] for r in rows),
        },
        "results": rows,
    }


def print_report(report: dict) -> None:
    print(
        f"\ncorpus={report['corpus_size']} queries={report['queries']} "
        f"scored={report['scored_queries']}"
    )
    if report["skipped_queries"]:
        print(f"skipped (no relevant profiles in corpus): {', '.join(report['skipped_queries'])}")
    print()
    header = f"{'query':<24}{'parser':<9}{'rel':>5}{'P@5':>7}{'P@10':>7}{'R@10':>7}{'MRR':>7}{'nDCG':>7}{'ms':>7}"
    print(header)
    print("-" * len(header))
    for row in report["results"]:
        flag = "SKIP" if row["skipped"] else ("ok" if row["parser_ok"] else "FAIL")
        print(
            f"{row['id']:<24}{flag:<9}{row['relevant_in_corpus']:>5}"
            f"{row['p_at_5']:>7.2f}{row['p_at_10']:>7.2f}{row['r_at_10']:>7.2f}"
            f"{row['mrr']:>7.2f}{row['ndcg_at_10']:>7.2f}{row['latency_ms']:>7}"
        )
        for problem in row["parser_problems"]:
            print(f"    parser: {problem}")

    summary = report["summary"]
    print("-" * len(header))
    print(
        f"{'MEAN':<24}{summary['parser_accuracy']:<9.2f}{'':>5}"
        f"{summary['p_at_5']:>7.2f}{summary['p_at_10']:>7.2f}{summary['r_at_10']:>7.2f}"
        f"{summary['mrr']:>7.2f}{summary['ndcg_at_10']:>7.2f}"
        f"{summary['median_latency_ms']:>7.0f}"
    )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-size", type=int, default=400)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--rerank", action="store_true", help="enable the LLM reranker (needs ANTHROPIC_API_KEY)"
    )
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument(
        "--min-ndcg",
        type=float,
        default=None,
        help="exit non-zero if mean nDCG@10 falls below this (for CI)",
    )
    args = parser.parse_args()

    report = run(args.corpus_size, args.rerank, args.limit)
    print_report(report)

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json_path}")

    if args.min_ndcg is not None and report["summary"]["ndcg_at_10"] < args.min_ndcg:
        print(
            f"FAIL: nDCG@10 {report['summary']['ndcg_at_10']} < threshold {args.min_ndcg}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
