"""Hand-maintained taxonomies for titles, seniority, skills and locations.

Deliberately small and inspectable: recruiters need to reason about why a
profile matched, and an opaque model can't be audited the way a table can.
Extend the dictionaries rather than adding one-off regexes at call sites.
"""

from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^a-z0-9+#./\- ]+")
_WS_RE = re.compile(r"\s+")


def slugify_text(value: str) -> str:
    """Lowercase, strip accents, collapse punctuation. Keeps ``c++``/``c#``/``node.js``."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("_", " ")
    value = _PUNCT_RE.sub(" ", value)
    return _WS_RE.sub(" ", value).strip()


# --------------------------------------------------------------------------
# seniority
# --------------------------------------------------------------------------

SENIORITY_ORDER = [
    "intern",
    "junior",
    "mid",
    "senior",
    "staff",
    "principal",
    "director",
    "vp",
    "cxo",
]

SENIORITY_RANK = {name: index for index, name in enumerate(SENIORITY_ORDER)}

# Longest patterns first — "vice president" must beat "president".
_SENIORITY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("cxo", ("chief executive", "chief technology", "chief product", "chief financial",
             "chief operating", "chief people", "chief revenue", "chief data",
             "chief information", "cto", "ceo", "cfo", "coo", "cpo", "cro", "ciso", "founder",
             "co founder")),
    ("vp", ("vice president", "svp", "evp", "vp", "head of")),
    ("director", ("director", "sr director", "senior director", "group manager")),
    ("principal", ("principal", "distinguished", "fellow", "architect")),
    ("staff", ("staff", "senior staff", "lead", "tech lead", "team lead")),
    ("senior", ("senior", "sr", "snr", "iii", "iv")),
    ("junior", ("junior", "jr", "associate", "entry level", "graduate", "new grad")),
    ("intern", ("intern", "internship", "trainee", "apprentice")),
]

SENIORITY_ALIASES = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "mid level": "mid",
    "midlevel": "mid",
    "mid-level": "mid",
    "ic": "senior",
    "exec": "cxo",
    "executive": "cxo",
    "c-level": "cxo",
    "c level": "cxo",
    "chief": "cxo",
    "vice president": "vp",
    "head": "vp",
}


def normalize_seniority(value: str) -> str:
    """Map a free-text seniority word onto the canonical ladder."""
    slug = slugify_text(value)
    if not slug:
        return "unknown"
    if slug in SENIORITY_RANK:
        return slug
    if slug in SENIORITY_ALIASES:
        return SENIORITY_ALIASES[slug]
    return infer_seniority_from_title(slug)


def infer_seniority_from_title(title: str) -> str:
    """Derive a seniority band from a job title string."""
    slug = slugify_text(title)
    if not slug:
        return "unknown"
    tokens = set(slug.split())
    for level, patterns in _SENIORITY_PATTERNS:
        for pattern in patterns:
            if " " in pattern:
                if pattern in slug:
                    return level
            elif pattern in tokens:
                return level
    return "mid" if slug else "unknown"


def seniority_distance(actual: str, wanted: str) -> int | None:
    """Signed rung distance on the ladder, or None when either side is unknown."""
    if actual not in SENIORITY_RANK or wanted not in SENIORITY_RANK:
        return None
    return SENIORITY_RANK[actual] - SENIORITY_RANK[wanted]


# --------------------------------------------------------------------------
# job titles
# --------------------------------------------------------------------------

# Noise words stripped before comparing two titles.
_TITLE_NOISE = {
    "senior", "sr", "snr", "junior", "jr", "staff", "principal", "lead", "head",
    "of", "the", "a", "an", "and", "at", "for", "i", "ii", "iii", "iv", "v",
    "associate", "assistant", "distinguished", "fellow", "chief", "vice",
    "president", "vp", "svp", "evp", "director", "intern", "contract",
    "contractor", "freelance", "part", "time", "full", "remote", "hybrid",
}

TITLE_ALIASES = {
    # IC engineering titles at AI labs. Without these, noise-stripping
    # reduces "member of technical staff" to "member technical", which
    # matches no posting -- so the whole point of sharing a vocabulary with
    # the JD parser is lost for a title a real candidate actually said.
    "member of technical staff": "software engineer",
    "member technical staff": "software engineer",
    "member technical": "software engineer",
    "mts": "software engineer",
    "swe": "software engineer",
    "sde": "software engineer",
    "software developer": "software engineer",
    "software development engineer": "software engineer",
    "programmer": "software engineer",
    "backend engineer": "backend engineer",
    "back end engineer": "backend engineer",
    "back-end engineer": "backend engineer",
    "server engineer": "backend engineer",
    "frontend engineer": "frontend engineer",
    "front end engineer": "frontend engineer",
    "front-end engineer": "frontend engineer",
    "ui engineer": "frontend engineer",
    "fullstack engineer": "full stack engineer",
    "full-stack engineer": "full stack engineer",
    "ml engineer": "machine learning engineer",
    "mle": "machine learning engineer",
    "ai engineer": "machine learning engineer",
    "applied scientist": "research scientist",
    "research engineer": "machine learning engineer",
    "data scientist": "data scientist",
    "ds": "data scientist",
    "data engineer": "data engineer",
    "analytics engineer": "data engineer",
    "devops engineer": "site reliability engineer",
    "sre": "site reliability engineer",
    "platform engineer": "site reliability engineer",
    "infrastructure engineer": "site reliability engineer",
    "security engineer": "security engineer",
    "appsec engineer": "security engineer",
    "product manager": "product manager",
    "pm": "product manager",
    "tpm": "technical program manager",
    "program manager": "technical program manager",
    "designer": "product designer",
    "ux designer": "product designer",
    "ui designer": "product designer",
    "account executive": "account executive",
    "ae": "account executive",
    "sdr": "sales development representative",
    "bdr": "sales development representative",
    "recruiter": "recruiter",
    "technical recruiter": "recruiter",
    "talent partner": "recruiter",
    "mobile engineer": "mobile engineer",
    "ios engineer": "mobile engineer",
    "android engineer": "mobile engineer",
    "qa engineer": "quality engineer",
    "test engineer": "quality engineer",
    "solutions architect": "solutions architect",
    "sales engineer": "solutions engineer",
    "solutions engineer": "solutions engineer",
    "customer success manager": "customer success manager",
    "csm": "customer success manager",
}

# Title families used for "close enough" credit when there is no exact match.
TITLE_FAMILIES = {
    "software engineer": {"backend engineer", "frontend engineer", "full stack engineer",
                          "mobile engineer", "machine learning engineer",
                          "site reliability engineer", "security engineer", "data engineer",
                          "quality engineer"},
    "backend engineer": {"software engineer", "full stack engineer", "data engineer",
                         "site reliability engineer"},
    "frontend engineer": {"software engineer", "full stack engineer", "product designer"},
    "full stack engineer": {"software engineer", "backend engineer", "frontend engineer"},
    "machine learning engineer": {"research scientist", "data scientist", "software engineer",
                                  "data engineer"},
    "research scientist": {"machine learning engineer", "data scientist"},
    "data scientist": {"machine learning engineer", "data engineer", "research scientist"},
    "data engineer": {"backend engineer", "data scientist", "software engineer"},
    "site reliability engineer": {"backend engineer", "software engineer", "security engineer"},
    "security engineer": {"site reliability engineer", "backend engineer"},
    "product manager": {"technical program manager", "product designer"},
    "technical program manager": {"product manager"},
    "product designer": {"frontend engineer", "product manager"},
    "account executive": {"sales development representative", "solutions engineer"},
    "solutions engineer": {"solutions architect", "account executive", "software engineer"},
}


def normalize_title(title: str) -> str:
    """Canonicalise a job title: alias map first, then noise-word stripping."""
    slug = slugify_text(title)
    if not slug:
        return ""
    if slug in TITLE_ALIASES:
        return TITLE_ALIASES[slug]

    # Try progressively shorter suffixes, e.g. "senior staff ml engineer" -> "ml engineer".
    words = slug.split()
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        if candidate in TITLE_ALIASES:
            return TITLE_ALIASES[candidate]

    core = [w for w in words if w not in _TITLE_NOISE]
    cleaned = " ".join(core) if core else slug
    return TITLE_ALIASES.get(cleaned, cleaned)


def title_similarity(actual: str, wanted: str) -> float:
    """0..1 similarity between two normalised titles."""
    a, b = normalize_title(actual), normalize_title(wanted)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if b in TITLE_FAMILIES.get(a, set()) or a in TITLE_FAMILIES.get(b, set()):
        return 0.72
    a_tokens, b_tokens = set(a.split()), set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
    # Substring containment ("engineer" inside "platform engineer") is a decent signal.
    if a in b or b in a:
        return max(overlap, 0.6)
    return overlap


# --------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------

SKILL_ALIASES = {
    "js": "javascript",
    "node": "node.js",
    "nodejs": "node.js",
    "node js": "node.js",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "c sharp": "c#",
    "csharp": "c#",
    "cpp": "c++",
    "c plus plus": "c++",
    "postgres": "postgresql",
    "psql": "postgresql",
    "pg": "postgresql",
    "k8s": "kubernetes",
    "gcp": "google cloud",
    "aws cloud": "aws",
    "amazon web services": "aws",
    "ml": "machine learning",
    "dl": "deep learning",
    "nlp": "natural language processing",
    "cv": "computer vision",
    "llms": "llm",
    "large language models": "llm",
    "large language model": "llm",
    "genai": "generative ai",
    "gen ai": "generative ai",
    "torch": "pytorch",
    "tf": "tensorflow",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "react.js": "react",
    "reactjs": "react",
    "vue.js": "vue",
    "vuejs": "vue",
    "rest apis": "rest",
    "restful": "rest",
    "ci cd": "ci/cd",
    "cicd": "ci/cd",
    "iac": "terraform",
    "dbt tool": "dbt",
    "elastic": "elasticsearch",
    "es": "elasticsearch",
    "rag": "retrieval augmented generation",
    "vector databases": "vector database",
    "vector db": "vector database",
    "distributed system": "distributed systems",
    "microservice": "microservices",
}

# Skills that meaningfully substitute for one another during scoring.
SKILL_NEIGHBOURS = {
    "pytorch": {"tensorflow", "jax", "deep learning"},
    "tensorflow": {"pytorch", "jax", "deep learning"},
    "jax": {"pytorch", "tensorflow"},
    "react": {"vue", "angular", "svelte", "javascript", "typescript"},
    "vue": {"react", "angular", "javascript"},
    "kubernetes": {"docker", "terraform", "aws", "google cloud"},
    "docker": {"kubernetes"},
    "aws": {"google cloud", "azure"},
    "google cloud": {"aws", "azure"},
    "azure": {"aws", "google cloud"},
    "postgresql": {"mysql", "sql", "snowflake"},
    "mysql": {"postgresql", "sql"},
    "snowflake": {"bigquery", "redshift", "postgresql"},
    "bigquery": {"snowflake", "redshift"},
    "llm": {"generative ai", "retrieval augmented generation", "natural language processing"},
    "generative ai": {"llm", "retrieval augmented generation"},
    "retrieval augmented generation": {"llm", "vector database", "generative ai"},
    "typescript": {"javascript"},
    "javascript": {"typescript"},
    "go": {"rust", "java", "c++"},
    "rust": {"go", "c++"},
    "spark": {"hadoop", "flink", "databricks"},
    "airflow": {"dagster", "prefect", "dbt"},
}

# Recognised when scanning free text for implicit skills.
KNOWN_SKILLS = sorted(
    {
        "python", "javascript", "typescript", "go", "rust", "java", "kotlin", "swift", "scala",
        "ruby", "php", "c++", "c#", "sql", "r", "matlab", "elixir",
        "react", "vue", "angular", "svelte", "next.js", "node.js", "django", "flask", "fastapi",
        "rails", "spring", "graphql", "rest", "grpc",
        "pytorch", "tensorflow", "jax", "scikit-learn", "pandas", "numpy", "hugging face",
        "machine learning", "deep learning", "natural language processing", "computer vision",
        "llm", "generative ai", "retrieval augmented generation", "vector database",
        "recommendation systems", "reinforcement learning", "mlops", "feature store",
        "aws", "google cloud", "azure", "kubernetes", "docker", "terraform", "ansible",
        "ci/cd", "linux", "kafka", "rabbitmq", "redis", "elasticsearch",
        "postgresql", "mysql", "mongodb", "dynamodb", "snowflake", "bigquery", "redshift",
        "spark", "hadoop", "flink", "airflow", "dbt", "dagster", "databricks",
        "distributed systems", "microservices", "system design", "observability",
        "security", "cryptography", "penetration testing", "threat modeling",
        "product management", "roadmapping", "user research", "figma", "design systems",
        "salesforce", "hubspot", "outbound", "enterprise sales", "saas",
    }
)


def normalize_skill(skill: str) -> str:
    slug = slugify_text(skill)
    if not slug:
        return ""
    return SKILL_ALIASES.get(slug, slug)


def normalize_skills(skills: list[str] | None) -> list[str]:
    seen: dict[str, None] = {}
    for skill in skills or []:
        normalized = normalize_skill(skill)
        if normalized:
            seen.setdefault(normalized, None)
    return list(seen)


def extract_skills_from_text(text: str) -> list[str]:
    """Find known skills mentioned anywhere in free text."""
    slug = slugify_text(text)
    if not slug:
        return []
    padded = f" {slug} "
    found: dict[str, None] = {}
    for skill in KNOWN_SKILLS:
        needle = f" {skill} "
        if needle in padded:
            found.setdefault(skill, None)
    for alias, canonical in SKILL_ALIASES.items():
        if f" {alias} " in padded:
            found.setdefault(canonical, None)
    return list(found)


def skill_match_strength(wanted: str, candidate_skills: set[str], haystack: str) -> float:
    """1.0 explicit skill, 0.7 adjacent skill, 0.45 mentioned in prose, 0.0 absent."""
    target = normalize_skill(wanted)
    if not target:
        return 0.0
    if target in candidate_skills:
        return 1.0
    for neighbour in SKILL_NEIGHBOURS.get(target, set()):
        if neighbour in candidate_skills:
            return 0.7
    if f" {target} " in f" {haystack} ":
        return 0.45
    return 0.0


# --------------------------------------------------------------------------
# locations
# --------------------------------------------------------------------------

LOCATION_ALIASES = {
    "sf": "san francisco",
    "sfo": "san francisco",
    "bay area": "san francisco",
    "sf bay area": "san francisco",
    "silicon valley": "san francisco",
    "nyc": "new york",
    "new york city": "new york",
    "la": "los angeles",
    "uk": "united kingdom",
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "blr": "bangalore",
    "bengaluru": "bangalore",
    "sg": "singapore",
    "ldn": "london",
}

METRO_MEMBERS = {
    "san francisco": {"san francisco", "oakland", "berkeley", "palo alto", "mountain view",
                      "menlo park", "san jose", "sunnyvale", "san mateo", "redwood city",
                      "santa clara", "california", "bay area"},
    "new york": {"new york", "brooklyn", "queens", "jersey city", "hoboken", "manhattan"},
    "london": {"london", "united kingdom", "england"},
    "seattle": {"seattle", "bellevue", "redmond", "kirkland", "washington"},
    "austin": {"austin", "texas"},
    "boston": {"boston", "cambridge", "somerville", "massachusetts"},
    "bangalore": {"bangalore", "karnataka", "india"},
    "berlin": {"berlin", "germany"},
    "toronto": {"toronto", "ontario", "canada"},
}


def normalize_location(value: str) -> str:
    slug = slugify_text(value)
    if not slug:
        return ""
    return LOCATION_ALIASES.get(slug, slug)


def location_match(candidate_parts: list[str], wanted: str) -> float:
    """1.0 exact city/region/country hit, 0.75 same metro, else 0.0."""
    target = normalize_location(wanted)
    if not target:
        return 0.0
    parts = {normalize_location(p) for p in candidate_parts if p}
    parts.discard("")
    if target in parts:
        return 1.0
    metro = METRO_MEMBERS.get(target, set())
    if metro & parts:
        return 0.75
    for part in parts:
        if part and (part in target or target in part):
            return 0.65
    return 0.0


# --------------------------------------------------------------------------
# companies
# --------------------------------------------------------------------------

COMPANY_SIZE_BUCKETS = ["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5000+"]

COMPANY_STAGES = ["pre-seed", "seed", "series a", "series b", "series c", "series d",
                  "growth", "public", "bootstrapped"]


def normalize_company(name: str) -> str:
    slug = slugify_text(name)
    for suffix in (" inc", " inc.", " llc", " ltd", " limited", " corp", " corporation", " gmbh",
                   " co", " plc", " sa", " bv"):
        if slug.endswith(suffix):
            slug = slug[: -len(suffix)].strip()
    return slug


def normalize_company_size(value: str) -> str:
    slug = slugify_text(value).replace(" to ", "-").replace(" ", "")
    mapping = {
        "1-10": "1-10", "110": "1-10", "startup": "11-50",
        "11-50": "11-50", "1150": "11-50",
        "51-200": "51-200", "51200": "51-200",
        "201-500": "201-500", "201500": "201-500",
        "501-1000": "501-1000", "5011000": "501-1000",
        "1001-5000": "1001-5000", "10015000": "1001-5000",
        "5000+": "5000+", "5000": "5000+", "enterprise": "5000+",
    }
    return mapping.get(slug, slug)


def normalize_company_stage(value: str) -> str:
    slug = slugify_text(value)
    mapping = {
        "preseed": "pre-seed", "pre seed": "pre-seed", "pre-seed": "pre-seed",
        "seed": "seed",
        "a": "series a", "series a": "series a", "seriesa": "series a",
        "b": "series b", "series b": "series b", "seriesb": "series b",
        "c": "series c", "series c": "series c", "seriesc": "series c",
        "d": "series d", "series d": "series d",
        "late stage": "growth", "growth": "growth", "unicorn": "growth",
        "public": "public", "ipo": "public", "listed": "public",
        "bootstrapped": "bootstrapped", "profitable": "bootstrapped",
    }
    return mapping.get(slug, slug)


INDUSTRY_ALIASES = {
    "fin tech": "fintech",
    "financial technology": "fintech",
    "health tech": "healthtech",
    "healthcare technology": "healthtech",
    "bio tech": "biotech",
    "e commerce": "ecommerce",
    "e-commerce": "ecommerce",
    "cyber security": "cybersecurity",
    "dev tools": "developer tools",
    "devtools": "developer tools",
    "artificial intelligence": "ai",
    # NB: "machine learning" is deliberately not aliased to the `ai` industry —
    # it is a skill, and treating it as an industry made every ML query demand
    # an AI-industry employer.
    "b2b saas": "saas",
    "enterprise software": "saas",
}


def normalize_industry(value: str) -> str:
    slug = slugify_text(value)
    return INDUSTRY_ALIASES.get(slug, slug)
