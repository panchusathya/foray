"""Turn a free-text query into a structured filter.

One LLM call, cached on a hash of the raw query so repeat searches skip it
entirely. Falls back to a deterministic heuristic parser (keyword -> archetype,
regex metros/languages/experience) when no key is set or the model fails, and to
pure vector search when nothing picks an archetype.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

from ..config import settings
from ..harvest.shards import LANGUAGES, load_metros
from ..log import get_logger
from ..score.archetypes import load_archetypes

log = get_logger("api.parse")

# Keyword -> archetype id (first match wins, order matters: specific before broad).
ARCHETYPE_KEYWORDS: list[tuple[str, str]] = [
    (r"\bfounding\b.*\bml\b|\bfounding\b.*machine learning", "founding_ml"),
    (r"\bml research|research engineer|research scientist", "ml_research"),
    (r"\bmlops|ml platform|ml infra", "ml_platform"),
    (r"\bgpu|cuda|kernel|inference runtime|triton|vllm", "gpu_systems"),
    (r"\bml\b|machine learning|deep learning|\bnlp\b|\bllm\b", "founding_ml"),
    (r"\bsre\b|site reliability|observability|on-call|oncall", "site_reliability"),
    (r"\binfra|infrastructure|platform|kubernetes|devops", "infra_platform"),
    (r"data engineer|data pipeline|\betl\b|warehouse|spark|airflow", "data_engineer"),
    (r"database|storage engine|distributed systems|consensus", "database_engineer"),
    (r"security|appsec|fuzzing|pentest|vulnerability", "security_engineer"),
    (r"embedded|firmware|robotics|\brtos\b", "embedded_engineer"),
    (r"mobile|ios|android|swift|kotlin", "mobile_engineer"),
    (r"front-?end|react|vue|svelte", "frontend_specialist"),
    (r"full-?stack|product engineer", "fullstack_product"),
    (r"backend|back-end|server|api|golang|\bgo\b|rust", "senior_backend"),
]

_EXPERIENCE_RANGE = re.compile(r"(\d+)\s*[-to]+\s*(\d+)\s*year")
_EXPERIENCE_MIN = re.compile(r"(\d+)\s*\+?\s*year")


@dataclass
class ParsedFilter:
    archetype_id: str | None = None
    metros: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    experience_min: float | None = None
    experience_max: float | None = None
    free_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()


def heuristic_parse(query: str) -> ParsedFilter:
    q = query.lower()

    archetype_id = None
    for pattern, aid in ARCHETYPE_KEYWORDS:
        if re.search(pattern, q):
            archetype_id = aid
            break

    metros = [m for m in load_metros() if m.lower() in q]
    languages = [lang for lang in LANGUAGES if re.search(rf"\b{re.escape(lang)}\b", q)]

    experience_min = experience_max = None
    if (m := _EXPERIENCE_RANGE.search(q)) is not None:
        experience_min, experience_max = float(m.group(1)), float(m.group(2))
    elif (m := _EXPERIENCE_MIN.search(q)) is not None:
        experience_min = float(m.group(1))

    return ParsedFilter(
        archetype_id=archetype_id,
        metros=metros,
        languages=languages,
        experience_min=experience_min,
        experience_max=experience_max,
        free_text=query.strip(),
    )


def _llm_schema_prompt() -> str:
    ids = ", ".join(load_archetypes())
    return (
        "Extract a candidate search filter as JSON with keys: "
        "archetype_id (one of: " + ids + ", or null), "
        "metros (list of US location strings), languages (list of programming languages), "
        "experience_min (number or null), experience_max (number or null), "
        "free_text (the semantic part of the query). Output ONLY the JSON object."
    )


def llm_parse(query: str) -> ParsedFilter | None:
    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.parse_model,
            max_tokens=400,
            system=[{"type": "text", "text": _llm_schema_prompt()}],
            messages=[{"role": "user", "content": query}],
        )
        text = msg.content[0].text if msg.content else "{}"
        if text.strip().startswith("```"):
            text = text.strip().split("\n", 1)[1].rsplit("```", 1)[0]
        obj = json.loads(text)
        valid_ids = set(load_archetypes())
        aid = obj.get("archetype_id")
        return ParsedFilter(
            archetype_id=aid if aid in valid_ids else None,
            metros=list(obj.get("metros") or []),
            languages=[str(x).lower() for x in (obj.get("languages") or [])],
            experience_min=obj.get("experience_min"),
            experience_max=obj.get("experience_max"),
            free_text=str(obj.get("free_text") or query),
        )
    except Exception as exc:  # noqa: BLE001 - any failure degrades to heuristic
        log.info("parse.llm_failed", error=str(exc))
        return None


async def parse_query(conn, query: str, *, use_llm: bool | None = None) -> tuple[ParsedFilter, str]:
    """Return (filter, parser_name). parser_name in cache|llm|heuristic|vector-fallback."""
    qhash = _query_hash(query)
    cached = await conn.fetchval(
        "select parsed from query_parse_cache where query_hash = $1", qhash
    )
    if cached is not None:
        data = json.loads(cached) if isinstance(cached, str) else cached
        parsed = ParsedFilter(**data)
        return parsed, ("cache" if parsed.archetype_id else "vector-fallback")

    want_llm = bool(settings.anthropic_api_key) if use_llm is None else use_llm
    parsed = llm_parse(query) if want_llm else None
    parser_name = "llm"
    if parsed is None:
        parsed = heuristic_parse(query)
        parser_name = "heuristic"
    if parsed.archetype_id is None:
        parser_name = "vector-fallback"

    await conn.execute(
        "insert into query_parse_cache (query_hash, raw_query, parsed) "
        "values ($1, $2, $3::jsonb) on conflict (query_hash) do nothing",
        qhash,
        query,
        json.dumps(parsed.to_dict()),
    )
    return parsed, parser_name
