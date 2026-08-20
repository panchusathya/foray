"""Offline evaluation of candidate↔job matching (not search nDCG).

    python eval/run_job_match.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "core"))

LABELS = REPO_ROOT / "eval" / "job_match.yaml"


def main() -> int:
    from juicebox.config import reset_settings_cache
    from juicebox.db import create_all, reset_engine, session_scope
    from juicebox.jobmatch.engine import match_posting
    from juicebox.jobmatch.parse import parse_posting
    from juicebox.models import Candidate, JobMatch, JobPosting, Org
    from juicebox.repository import upsert_candidates

    os.environ.setdefault("EMBEDDING_PROVIDER", "hashing")
    os.environ["JUICEBOX_LLM_ENABLED"] = "false"
    os.environ["ANTHROPIC_API_KEY"] = ""
    tmp = Path(tempfile.mkdtemp(prefix="juicebox-job-eval-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp / 'eval.db').as_posix()}"
    reset_settings_cache()
    reset_engine()
    create_all()

    spec = yaml.safe_load(LABELS.read_text())
    with session_scope() as db:
        org = Org(name="JobEval", slug="job-eval")
        db.add(org)
        db.flush()
        payloads = [
            {
                "source": "eval",
                "source_id": "ml-1",
                "full_name": "Ada Okonkwo",
                "location_city": "San Francisco",
                "emails": ["ada@example.com"],
                "skills": ["Python", "PyTorch", "LLM"],
                "experiences": [
                    {
                        "company": "Modal",
                        "title": "Senior Machine Learning Engineer",
                        "is_current": True,
                    }
                ],
            },
            {
                "source": "eval",
                "source_id": "fe-1",
                "full_name": "Bo Lindqvist",
                "location_city": "Berlin",
                "emails": ["bo@example.com"],
                "skills": ["TypeScript", "React"],
                "experiences": [
                    {
                        "company": "Linear",
                        "title": "Senior Frontend Engineer",
                        "is_current": True,
                    }
                ],
            },
            {
                "source": "eval",
                "source_id": "be-1",
                "full_name": "Cleo Rahman",
                "location_city": "London",
                "emails": ["cleo@example.com"],
                "skills": ["Go", "PostgreSQL", "Kafka"],
                "experiences": [
                    {
                        "company": "Ramp",
                        "title": "Staff Backend Engineer",
                        "is_current": True,
                    }
                ],
            },
        ]
        upsert_candidates(db, org_id=org.id, payloads=payloads)
        org_id = org.id

        postings = {}
        for item in spec["postings"]:
            posting = JobPosting(
                org_id=org_id,
                source="eval",
                source_id=item["id"],
                title=item["title"],
                company=item.get("company", ""),
                location=item.get("location", ""),
                jd_text=item.get("jd_text", ""),
                status="pending_parse",
            )
            db.add(posting)
            db.flush()
            parse_posting(db, posting)
            match_posting(db, posting)
            postings[item["id"]] = posting.id

        by_source = {
            c.source_id: c.id
            for c in db.query(Candidate).filter(Candidate.org_id == org_id).all()
        }
        tp = fp = fn = 0
        for label in spec["labels"]:
            cand_id = by_source.get(label["source_id"])
            posting_id = postings[label["posting"]]
            row = (
                db.query(JobMatch)
                .filter_by(job_posting_id=posting_id, candidate_id=cand_id)
                .one_or_none()
                if cand_id
                else None
            )
            predicted = row is not None
            relevant = bool(label["relevant"])
            if predicted and relevant:
                tp += 1
            elif predicted and not relevant:
                fp += 1
            elif not predicted and relevant:
                fn += 1

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    report = {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
