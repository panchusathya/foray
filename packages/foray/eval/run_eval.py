"""Foray-native relevance harness.

eval/run_eval.py cannot evaluate foray — it bootstraps a juicebox Org. This
harness labels the synthetic THEMES corpus with rules and reports nDCG plus
arm diagnostics (lexical_hits / vector_hits / overlap).

    uv run python eval/run_eval.py
    FORAY_RETRIEVAL_ARMS=score uv run python eval/run_eval.py

Kept out of per-push CI the way the `model` marker already is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml
from metrics import junk_at_k, mrr, ndcg_at_k, precision_at_k, recall_at_k

from foray.api.search import parse_arms, run_search
from foray.db import close_pool, get_pool
from foray.embed.hashing import hashing_embed

HERE = Path(__file__).resolve().parent


def is_distractor(login: str) -> bool:
    """Seeded junk personas (personal trainer, recruiter, logistics, ...) carry a
    "-distr" slug / "_distr" login suffix — the one marker the pipeline cannot
    lose (LinkedIn login is "li:"+slug verbatim; GitHub login is inserted as-is).
    Prose-based detection would be circular: the recruiter distractor NAMES the
    tech on purpose."""
    return login.endswith(("-distr", "_distr"))


def is_relevant(row: dict, rule: dict) -> bool:
    # Distractors are relevant to NO query by construction — that is their job.
    if row.get("distractor"):
        return False
    themes = set(row.get("themes") or [])
    text = " ".join(
        [
            str(row.get("bio") or ""),
            " ".join(row.get("terms") or []),
            " ".join(row.get("domain_markers") or []),
        ]
    ).lower()
    want = rule.get("theme_any") or []
    if want and not themes.intersection(want):
        return False
    want = rule.get("terms_any") or []
    return not want or any(term in text for term in want)


async def collect_labels(conn) -> dict[str, dict]:
    # LinkedIn-seeded logins are "li:{first}-{last}-synthNNNNN[-distr]" — the old
    # 'synth_%'-only filter plus the github-only raw_profiles join left the whole
    # LinkedIn population unlabeled, which is exactly how the personal-trainer
    # incident sailed past the eval. Labels are source-agnostic now: themes/terms
    # come from candidate_signals either way, prose from whichever raw row exists.
    rows = await conn.fetch(
        """
        select c.login, c.name, s.domain_markers, s.topics,
               coalesce(gh.data->>'bio', li.data->>'about') as bio
        from candidates c
        join candidate_signals s on s.candidate_id = c.id
        left join raw_profiles gh on gh.candidate_id = c.id and gh.source = 'github'
        left join raw_profiles li on li.candidate_id = c.id and li.source = 'linkedin'
        where (c.login like 'synth_%' or c.login like 'li:%synth%')
          and c.merged_into is null
        """
    )
    out = {}
    for row in rows:
        markers = list(row["domain_markers"] or [])
        bio = row["bio"] or ""
        out[row["login"]] = {
            "login": row["login"],
            "themes": markers,
            "terms": list(row["topics"] or []),
            "bio": bio,
            "domain_markers": markers,
            "distractor": is_distractor(row["login"]),
        }
    return out


async def amain(args: argparse.Namespace) -> int:
    spec = yaml.safe_load((HERE / "queries.yaml").read_text(encoding="utf-8"))
    pool = await get_pool()
    async with pool.acquire() as conn:
        corpus = await collect_labels(conn)
        if not corpus:
            print("no synth_ candidates; run scripts/seed_synthetic.py && foray derive")
            await close_pool()
            return 2
        print(f"corpus {len(corpus)} synth logins | arms={parse_arms()}")
        rows = []
        for case in spec["queries"]:
            relevant = {login for login, doc in corpus.items() if is_relevant(doc, case["relevant"])}
            result = await run_search(
                conn, case["query"], 40, use_llm=False, embed=hashing_embed
            )
            ranked = [hit["login"] for hit in result.results]
            labels = [login in relevant for login in ranked]
            # Junk labeling is by login marker, not corpus membership: an
            # unknown login is merely unlabeled, never counted as junk.
            labels_junk = [is_distractor(login) for login in ranked]
            diag = result.diagnostics or {}
            rows.append(
                {
                    "id": case["id"],
                    "query": case["query"],
                    "relevant": len(relevant),
                    "returned": result.returned,
                    "p10": round(precision_at_k(labels, 10), 3),
                    "r10": round(recall_at_k(labels, len(relevant), 10), 3),
                    "mrr": round(mrr(labels), 3),
                    "ndcg10": round(ndcg_at_k(labels, len(relevant), 10), 3),
                    "junk10": round(junk_at_k(labels_junk, 10), 3),
                    "vector_hits": diag.get("vector_hits"),
                    "lexical_hits": diag.get("lexical_hits"),
                    "overlap": diag.get("overlap"),
                    "rrf_only_from_lexical@10": diag.get("rrf_only_from_lexical_at_10"),
                    "unscored_in_top_10": diag.get("unscored_in_top_10"),
                    "empty_lexical_query": diag.get("empty_lexical_query"),
                }
            )
            print(
                f"  {case['id']:22s} nDCG@10={rows[-1]['ndcg10']:.3f}  "
                f"junk@10={rows[-1]['junk10']:.3f}  "
                f"P@10={rows[-1]['p10']:.3f}  unscored@10={rows[-1]['unscored_in_top_10']}  "
                f"lex={rows[-1]['lexical_hits']} vec={rows[-1]['vector_hits']} "
                f"overlap={rows[-1]['overlap']} lex-only@10={rows[-1]['rrf_only_from_lexical@10']}"
            )
    await close_pool()
    mean = sum(r["ndcg10"] for r in rows) / len(rows) if rows else 0.0
    mean_junk = sum(r["junk10"] for r in rows) / len(rows) if rows else 0.0
    print(f"mean nDCG@10 {mean:.3f} | mean junk@10 {mean_junk:.3f}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"rows": rows, "mean_ndcg10": mean, "mean_junk10": mean_junk}, indent=2)
        )
    # Gate on junk, not just report it — the incident query LOOKED fine on nDCG
    # because the junk that outranked good hits was never labeled at all.
    if args.max_junk is not None and mean_junk > args.max_junk:
        print(f"FAIL mean junk@10 {mean_junk:.3f} > --max-junk {args.max_junk}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write a JSON report")
    ap.add_argument(
        "--max-junk",
        type=float,
        help="exit 1 if mean junk@10 (fraction of top-10 that are seeded distractors) exceeds this",
    )
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
