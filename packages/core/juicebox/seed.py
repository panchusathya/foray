"""Deterministic demo data.

Generated rather than hand-written so the corpus is large enough for retrieval
to be interesting, and identical on every machine (fixed RNG seed) so eval
numbers are comparable.
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Org, Project, Sequence, SequenceStep, SourcingAgent, User
from .repository import upsert_candidates
from .security import hash_password

SEED = 20240917

FIRST_NAMES = [
    "Amara", "Priya", "Wei", "Sofia", "Diego", "Noor", "Kenji", "Elena", "Tobi", "Mira",
    "Jonas", "Fatima", "Luca", "Ana", "Ravi", "Yuki", "Omar", "Ingrid", "Mateo", "Chen",
    "Zara", "Nikhil", "Clara", "Hassan", "Aoife", "Sam", "Lena", "Arjun", "Rosa", "Ida",
    "Malik", "Nina", "Theo", "Grace", "Youssef", "Hana", "Pablo", "Anya", "Felix", "Leila",
]

LAST_NAMES = [
    "Okonkwo", "Sharma", "Zhang", "Ramirez", "Silva", "Haddad", "Tanaka", "Petrova",
    "Adeyemi", "Kaur", "Lindqvist", "Rahman", "Bianchi", "Costa", "Iyer", "Nakamura",
    "Farouk", "Berg", "Lopez", "Wu", "Ahmed", "Menon", "Dubois", "Karim", "Byrne",
    "Novak", "Fischer", "Reddy", "Moreno", "Larsen", "Diallo", "Kowalski", "Meyer",
    "Mensah", "Barakat", "Kim", "Rossi", "Volkov", "Schneider", "Nasser",
]

CITIES = [
    ("San Francisco", "CA", "United States"),
    ("New York", "NY", "United States"),
    ("Seattle", "WA", "United States"),
    ("Austin", "TX", "United States"),
    ("Boston", "MA", "United States"),
    ("London", "England", "United Kingdom"),
    ("Berlin", "Berlin", "Germany"),
    ("Bangalore", "Karnataka", "India"),
    ("Toronto", "Ontario", "Canada"),
    ("Singapore", "", "Singapore"),
]

COMPANIES: list[dict[str, str]] = [
    {"name": "Stripe", "domain": "stripe.com", "industry": "fintech", "size": "5000+", "stage": "growth"},
    {"name": "Ramp", "domain": "ramp.com", "industry": "fintech", "size": "501-1000", "stage": "series d"},
    {"name": "Plaid", "domain": "plaid.com", "industry": "fintech", "size": "501-1000", "stage": "growth"},
    {"name": "Databricks", "domain": "databricks.com", "industry": "developer tools", "size": "5000+", "stage": "growth"},
    {"name": "Snowflake", "domain": "snowflake.com", "industry": "developer tools", "size": "5000+", "stage": "public"},
    {"name": "Anthropic", "domain": "anthropic.com", "industry": "ai", "size": "1001-5000", "stage": "growth"},
    {"name": "Hugging Face", "domain": "huggingface.co", "industry": "ai", "size": "201-500", "stage": "series d"},
    {"name": "Weights & Biases", "domain": "wandb.ai", "industry": "ai", "size": "201-500", "stage": "series c"},
    {"name": "Vercel", "domain": "vercel.com", "industry": "developer tools", "size": "501-1000", "stage": "series d"},
    {"name": "Linear", "domain": "linear.app", "industry": "saas", "size": "51-200", "stage": "series b"},
    {"name": "Notion", "domain": "notion.so", "industry": "saas", "size": "501-1000", "stage": "growth"},
    {"name": "Figma", "domain": "figma.com", "industry": "saas", "size": "1001-5000", "stage": "public"},
    {"name": "Oscar Health", "domain": "hioscar.com", "industry": "healthtech", "size": "5000+", "stage": "public"},
    {"name": "Tempus", "domain": "tempus.com", "industry": "healthtech", "size": "1001-5000", "stage": "public"},
    {"name": "Benchling", "domain": "benchling.com", "industry": "biotech", "size": "501-1000", "stage": "series f"},
    {"name": "Cribl", "domain": "cribl.io", "industry": "cybersecurity", "size": "501-1000", "stage": "series d"},
    {"name": "Wiz", "domain": "wiz.io", "industry": "cybersecurity", "size": "1001-5000", "stage": "growth"},
    {"name": "Modal", "domain": "modal.com", "industry": "ai", "size": "11-50", "stage": "series a"},
    {"name": "Baseten", "domain": "baseten.co", "industry": "ai", "size": "51-200", "stage": "series b"},
    {"name": "Sardine", "domain": "sardine.ai", "industry": "fintech", "size": "51-200", "stage": "series b"},
    {"name": "Neon", "domain": "neon.tech", "industry": "developer tools", "size": "51-200", "stage": "series b"},
    {"name": "Airbyte", "domain": "airbyte.com", "industry": "developer tools", "size": "201-500", "stage": "series b"},
    {"name": "Shopify", "domain": "shopify.com", "industry": "ecommerce", "size": "5000+", "stage": "public"},
    {"name": "Instacart", "domain": "instacart.com", "industry": "ecommerce", "size": "5000+", "stage": "public"},
]

ARCHETYPES: list[dict[str, Any]] = [
    {
        "family": "machine learning engineer",
        "titles": ["Machine Learning Engineer", "ML Engineer", "Senior Machine Learning Engineer"],
        "core": ["python", "pytorch", "machine learning", "deep learning"],
        "extras": ["llm", "retrieval augmented generation", "vector database", "kubernetes",
                   "aws", "tensorflow", "mlops", "spark", "natural language processing"],
        "blurbs": [
            "Built and shipped a retrieval-augmented answer engine serving {n}M monthly queries.",
            "Owned model training infrastructure on {cloud}, cutting fine-tuning time by {pct}%.",
            "Led the migration from batch scoring to a real-time inference service in PyTorch.",
        ],
    },
    {
        "family": "backend engineer",
        "titles": ["Backend Engineer", "Senior Backend Engineer", "Staff Backend Engineer"],
        "core": ["python", "postgresql", "distributed systems"],
        "extras": ["go", "kafka", "redis", "kubernetes", "aws", "grpc", "microservices",
                   "terraform", "observability"],
        "blurbs": [
            "Re-architected the payments ledger to handle {n}k transactions per second.",
            "Ran the platform migration from a monolith to {n} services with zero downtime.",
            "Cut p99 latency on the core API by {pct}% through query and cache redesign.",
        ],
    },
    {
        "family": "frontend engineer",
        "titles": ["Frontend Engineer", "Senior Frontend Engineer", "Staff Frontend Engineer"],
        "core": ["typescript", "react", "javascript"],
        "extras": ["next.js", "graphql", "design systems", "figma", "node.js", "observability"],
        "blurbs": [
            "Rebuilt the onboarding flow, lifting activation by {pct}%.",
            "Owned the company design system used by {n} product teams.",
            "Drove a full accessibility audit and remediation across the customer dashboard.",
        ],
    },
    {
        "family": "data engineer",
        "titles": ["Data Engineer", "Senior Data Engineer", "Analytics Engineer"],
        "core": ["python", "sql", "spark"],
        "extras": ["airflow", "dbt", "snowflake", "bigquery", "databricks", "kafka", "aws"],
        "blurbs": [
            "Built the streaming ingestion pipeline processing {n}TB per day.",
            "Migrated the warehouse to Snowflake and cut spend by {pct}%.",
            "Introduced dbt and contract testing across {n} downstream data products.",
        ],
    },
    {
        "family": "site reliability engineer",
        "titles": ["Site Reliability Engineer", "Platform Engineer", "Infrastructure Engineer"],
        "core": ["kubernetes", "terraform", "linux"],
        "extras": ["aws", "google cloud", "ci/cd", "observability", "go", "python", "security"],
        "blurbs": [
            "Ran the multi-region failover programme, reaching {pct}% availability.",
            "Cut cloud spend by {pct}% through right-sizing and spot orchestration.",
            "Built the internal developer platform used by {n} engineers.",
        ],
    },
    {
        "family": "security engineer",
        "titles": ["Security Engineer", "Senior Security Engineer", "Application Security Engineer"],
        "core": ["security", "threat modeling", "python"],
        "extras": ["penetration testing", "cryptography", "kubernetes", "aws", "observability"],
        "blurbs": [
            "Led SOC 2 Type II readiness and closed {n} findings ahead of audit.",
            "Built the automated secrets-scanning pipeline across {n} repositories.",
            "Ran the company's first red-team exercise and the remediation programme after it.",
        ],
    },
    {
        "family": "product manager",
        "titles": ["Product Manager", "Senior Product Manager", "Group Product Manager"],
        "core": ["product management", "roadmapping", "user research"],
        "extras": ["saas", "sql", "figma", "design systems"],
        "blurbs": [
            "Took the developer platform from private beta to {n}k active teams.",
            "Owned the pricing and packaging change that lifted net revenue retention by {pct}%.",
            "Ran discovery with {n} enterprise customers to reset the roadmap.",
        ],
    },
    {
        "family": "product designer",
        "titles": ["Product Designer", "Senior Product Designer", "Design Lead"],
        "core": ["figma", "design systems", "user research"],
        "extras": ["product management", "react", "typescript"],
        "blurbs": [
            "Redesigned the core workflow, cutting time-to-first-value by {pct}%.",
            "Established the design system now used across {n} surfaces.",
            "Ran a research programme with {n} customers to reframe the information architecture.",
        ],
    },
]

SENIORITY_PREFIX = {
    "junior": "",
    "mid": "",
    "senior": "Senior ",
    "staff": "Staff ",
    "principal": "Principal ",
}

SCHOOLS = [
    "Stanford University", "MIT", "Carnegie Mellon University", "UC Berkeley",
    "University of Toronto", "ETH Zurich", "IIT Bombay", "University of Cambridge",
    "Technical University of Munich", "National University of Singapore",
    "Georgia Institute of Technology", "University of Washington",
]

DEGREES = [("BS", "Computer Science"), ("MS", "Computer Science"),
           ("PhD", "Machine Learning"), ("BS", "Electrical Engineering"),
           ("MS", "Data Science"), ("BA", "Design")]


def _pick_seniority(rng: random.Random, years: float) -> str:
    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    if years < 9:
        return "senior"
    if years < 13:
        return rng.choice(["senior", "staff"])
    return rng.choice(["staff", "principal"])


def generate_candidates(count: int = 120, *, seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    today = date.today()
    candidates: list[dict[str, Any]] = []

    for index in range(count):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        full_name = f"{first} {last}"
        archetype = ARCHETYPES[index % len(ARCHETYPES)]
        city, region, country = rng.choice(CITIES)

        total_years = round(rng.uniform(1.5, 16.0), 1)
        seniority = _pick_seniority(rng, total_years)

        skills = list(archetype["core"])
        skills += rng.sample(archetype["extras"], k=rng.randint(2, min(5, len(archetype["extras"]))))

        # Build 1-3 roles, most recent first, walking backwards through time.
        role_count = 1 if total_years < 3 else rng.randint(2, 3)
        remaining_months = int(total_years * 12)
        cursor_year, cursor_month = today.year, today.month
        experiences: list[dict[str, Any]] = []

        for role_index in range(role_count):
            company = rng.choice(COMPANIES)
            months = (
                remaining_months
                if role_index == role_count - 1
                else max(12, int(remaining_months * rng.uniform(0.3, 0.6)))
            )
            months = max(6, min(months, remaining_months))
            end_year, end_month = cursor_year, cursor_month
            start_total = (cursor_year * 12 + cursor_month) - months
            start_year, start_month = divmod(start_total, 12)
            if start_month == 0:
                start_year, start_month = start_year - 1, 12

            base_title = rng.choice(archetype["titles"])
            if role_index == 0:
                prefix = SENIORITY_PREFIX.get(seniority, "")
                title = base_title if base_title.startswith(("Senior", "Staff", "Principal")) else f"{prefix}{base_title}"
            else:
                title = base_title.replace("Senior ", "").replace("Staff ", "")

            blurb = rng.choice(archetype["blurbs"]).format(
                n=rng.randint(2, 40),
                pct=rng.randint(15, 60),
                cloud=rng.choice(["AWS", "GCP", "Azure"]),
            )
            experiences.append(
                {
                    "company": company["name"],
                    "company_domain": company["domain"],
                    "company_industry": company["industry"],
                    "company_size": company["size"],
                    "company_stage": company["stage"],
                    "title": title.strip(),
                    "start_date": f"{start_year:04d}-{start_month:02d}",
                    "end_date": "" if role_index == 0 else f"{end_year:04d}-{end_month:02d}",
                    "is_current": role_index == 0,
                    "location": f"{city}, {region}" if region else city,
                    "description": blurb,
                }
            )
            remaining_months -= months
            cursor_year, cursor_month = start_year, start_month
            if remaining_months <= 6:
                break

        degree, study = rng.choice(DEGREES)
        grad_year = today.year - int(total_years) - rng.randint(0, 2)
        handle = f"{first}.{last}".lower()

        candidates.append(
            {
                "source": "seed",
                "source_id": f"seed-{index:04d}",
                "full_name": full_name,
                "headline": f"{experiences[0]['title']} at {experiences[0]['company']}",
                "summary": (
                    f"{seniority.capitalize()} {archetype['family']} with {total_years:g} years "
                    f"of experience. {experiences[0]['description']}"
                ),
                "location_city": city,
                "location_region": region,
                "location_country": country,
                "open_to_remote": rng.random() < 0.45,
                "linkedin_url": f"https://linkedin.com/in/{handle}-{index:04d}",
                "github_url": (
                    f"https://github.com/{first.lower()}{last.lower()}" if rng.random() < 0.6 else ""
                ),
                "emails": [f"{handle}@example.com"],
                "skills": sorted(set(skills)),
                "industries": [experiences[0]["company_industry"]],
                "experiences": experiences,
                "educations": [
                    {
                        "school": rng.choice(SCHOOLS),
                        "degree": degree,
                        "field_of_study": study,
                        "end_year": grad_year,
                    }
                ],
            }
        )
    return candidates


# --------------------------------------------------------------------------
# full demo workspace
# --------------------------------------------------------------------------

# Must be a routable-looking domain: reserved TLDs (.local, .test, .invalid)
# are rejected by the EmailStr validator on the login endpoint.
DEMO_EMAIL = "demo@juicebox.dev"
DEMO_PASSWORD = "juicebox-demo"

SEQUENCE_STEPS = [
    {
        "delay_days": 0,
        "subject_template": "{{ subject_hint | A quick note }}",
        "body_template": (
            "Hi {{ first_name }},\n\n"
            "{{ opening_line }}\n\n"
            "{{ why_relevant }}\n\n"
            "We're hiring a {{ role_title | senior engineer }} and I'd love 15 minutes to walk "
            "you through it. Open to a chat this week?\n\n"
            "Best,\n{{ sender_first_name | The Talent Team }}"
        ),
    },
    {
        "delay_days": 4,
        "subject_template": "Re: {{ subject_hint | A quick note }}",
        "body_template": (
            "Hi {{ first_name }},\n\n"
            "Following up on my note about the {{ role_title | role }} — happy to send over the "
            "team and scope details if that's easier than a call.\n\n"
            "Either way, thanks for reading.\n\n"
            "{{ sender_first_name | The Talent Team }}"
        ),
    },
    {
        "delay_days": 7,
        "subject_template": "Closing the loop",
        "body_template": (
            "Hi {{ first_name }},\n\n"
            "I'll stop here so I'm not cluttering your inbox. If the timing changes, my door is "
            "open — your work on {{ top_skill | your side of the stack }} was genuinely "
            "interesting.\n\n"
            "{{ sender_first_name | The Talent Team }}"
        ),
    },
]


def seed_demo(db: Session, *, candidate_count: int = 120) -> dict[str, Any]:
    """Idempotent: re-running tops up candidates without duplicating the workspace."""
    org = db.scalar(select(Org).where(Org.slug == "demo"))
    if org is None:
        org = Org(name="Juicebox Demo", slug="demo")
        db.add(org)
        db.flush()

    user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        user = User(
            org_id=org.id,
            email=DEMO_EMAIL,
            name="Demo Recruiter",
            password_hash=hash_password(DEMO_PASSWORD),
            role="owner",
        )
        db.add(user)
        db.flush()

    result = upsert_candidates(
        db, org_id=org.id, payloads=generate_candidates(candidate_count)
    )

    project = db.scalar(select(Project).where(Project.org_id == org.id))
    if project is None:
        project = Project(
            org_id=org.id,
            name="Founding ML Engineer",
            role_title="Senior Machine Learning Engineer",
            description=(
                "Own the retrieval and ranking stack end to end: training, serving, and the "
                "evaluation harness. Small team, high autonomy, production LLM systems."
            ),
            location="San Francisco, CA",
            created_by=user.id,
        )
        db.add(project)
        db.flush()

    sequence = db.scalar(select(Sequence).where(Sequence.org_id == org.id))
    if sequence is None:
        sequence = Sequence(
            org_id=org.id,
            project_id=project.id,
            name="ML Engineer — 3 touch",
            from_email="talent@juicebox.dev",
            from_name="Juicebox Talent",
        )
        db.add(sequence)
        db.flush()
        for order, step in enumerate(SEQUENCE_STEPS):
            db.add(SequenceStep(sequence_id=sequence.id, step_order=order, **step))
        db.flush()

    agent = db.scalar(select(SourcingAgent).where(SourcingAgent.org_id == org.id))
    if agent is None:
        agent = SourcingAgent(
            org_id=org.id,
            project_id=project.id,
            name="Senior ML engineers in SF",
            query=(
                "senior machine learning engineers in San Francisco with pytorch and "
                "production LLM experience, 5+ years"
            ),
            min_score=0.55,
            max_per_run=10,
            cadence_minutes=1440,
            auto_enroll_sequence_id=sequence.id,
        )
        db.add(agent)
        db.flush()

    return {
        "org_id": org.id,
        "user_email": DEMO_EMAIL,
        "password": DEMO_PASSWORD,
        "project_id": project.id,
        "sequence_id": sequence.id,
        "agent_id": agent.id,
        "candidates_created": result.created,
        "candidates_updated": result.updated,
    }
