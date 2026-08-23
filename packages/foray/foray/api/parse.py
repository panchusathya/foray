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
from dataclasses import asdict, dataclass, field, fields

from ..config import settings
from ..harvest.shards import load_metros
from ..llm import Chat, OpenRouterChat
from ..log import get_logger
from ..score.archetypes import load_archetypes
from ..vocab import match_languages

log = get_logger("api.parse")

# Bumped when ARCHETYPE_KEYWORDS or heuristic extraction changes — or when the
# ParsedFilter dict shape changes (v3 added strict_language, v4 added
# include_unknown_experience): the cache stores
# to_dict() output, so a shape change without a bump would hydrate old rows
# into the new dataclass (and, on rollback, new rows into the old one). Folded
# into the cache key so parse_query cannot serve a row that was written under
# an older vocabulary or shape. Orphan rows in query_parse_cache are harmless.
PARSER_VERSION = 4

# Keyword -> archetype id (first match wins, order matters: specific before broad).
ARCHETYPE_KEYWORDS: list[tuple[str, str]] = [
    (r"\bfounding\b.*\bml\b|\bfounding\b.*machine learning", "founding_ml"),
    (r"\bml research|research engineer|research scientist", "ml_research"),
    (r"\bmlops|ml platform|ml infra", "ml_platform"),
    (r"\bgpu|cuda|kernel|inference runtime|triton|vllm", "gpu_systems"),
    # Specific ML vocab before the broad \bml\b rule so "VLM engineer" maps
    # here rather than falling through to vector-fallback, but AFTER research /
    # mlops / gpu so "VLM research scientist" stays ml_research.
    (
        r"\bvlm\b|multimodal|vision-language|\brlhf\b|pretraining|pre-training|\bdiffusion\b",
        "founding_ml",
    ),
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
    # Policy switch, never extracted from the query text — neither parser sets
    # it and the LLM schema prompt deliberately does not advertise it yet.
    # Measured on prod (2026-08): `primary_language = any(['python'])` matched
    # 550 of 204,535 rows (0.3%) because primary_language is a GitHub-derived
    # column, populated on ZERO of the 196,778 LinkedIn-only rows — the hard
    # equality was a source filter in disguise ("and is a GitHub row"). The
    # default False therefore emits three-valued matching in _filter_clauses
    # (language matches OR is unknown); True restores the strict predicate for
    # callers that genuinely mean "known to write X", accepting the collapse.
    strict_language: bool = False
    # The same unknown-policy question for experience. est_experience_years is
    # NULL for 134,536 rows (no dated role in the profile), so a bare
    # `>= 3` / `<= 8` excludes them under three-valued logic. Strict is the
    # right DEFAULT — "we do not know this person's experience" is not evidence
    # that they have 3 years, and the pre-NULL behaviour of storing 0 made
    # every unknown row silently answer "yes" to `<= 8`. But the loose reading
    # has to be expressible: a max-only brief ("under 8 years") reasonably
    # wants the unknowns back, since it is screening OUT the over-experienced
    # rather than screening IN a floor. Neither parser sets this and the LLM
    # schema prompt does not advertise it; it is a caller policy switch.
    include_unknown_experience: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ParsedFilter:
        """Hydrate from a cached to_dict(), dropping keys this build lacks.

        The PARSER_VERSION bump on shape changes is the primary protection
        (a bumped version hashes to a different cache key, so old rows are
        simply never read). This is the belt-and-braces for the case the bump
        does not cover: a rollback, or a shape change that forgot to bump —
        there ``ParsedFilter(**data)`` raises TypeError on the unknown key and
        500s every search for that cached query. Unknown keys degrade to
        defaults instead; a stale parse is strictly better than no search.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def _query_hash(query: str) -> str:
    payload = f"{PARSER_VERSION}\n{query.strip().lower()}".encode()
    return hashlib.sha256(payload).hexdigest()


def heuristic_parse(query: str) -> ParsedFilter:
    q = query.lower()

    archetype_id = None
    for pattern, aid in ARCHETYPE_KEYWORDS:
        if re.search(pattern, q):
            archetype_id = aid
            break

    metros = [m for m in load_metros() if m.lower() in q]
    # vocab.match_languages returns STORED forms (what derive writes, what the
    # SQL filter compares against). The previous implementation matched the
    # query against harvest's GitHub-qualifier list with rf"\b{lang}\b", which
    # was dead twice over: it emitted "cpp" where the column holds "c++", and
    # \b after '+' can never match "c++" at end-of-token anyway.
    languages = match_languages(q)

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


def llm_parse(query: str, client: Chat | None = None) -> ParsedFilter | None:
    try:
        completion = OpenRouterChat() if client is None else client
        text = (
            completion.complete(_llm_schema_prompt(), query, model=settings.parse_model)
            or "{}"
        )
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


async def parse_query(
    conn,
    query: str,
    *,
    use_llm: bool | None = None,
    chat: Chat | None = None,
) -> tuple[ParsedFilter, str]:
    """Return (filter, parser_name). parser_name in cache|llm|heuristic|vector-fallback."""
    qhash = _query_hash(query)
    cached = await conn.fetchval(
        "select parsed from query_parse_cache where query_hash = $1", qhash
    )
    if cached is not None:
        data = json.loads(cached) if isinstance(cached, str) else cached
        parsed = ParsedFilter.from_dict(data)
        return parsed, ("cache" if parsed.archetype_id else "vector-fallback")

    want_llm = bool(settings.openrouter_api_key) if use_llm is None else use_llm
    parsed = llm_parse(query, chat) if want_llm else None
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
