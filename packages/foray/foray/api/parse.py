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
from ..enrich.markers import load_markers
from ..llm import Chat, OpenRouterChat
from ..log import get_logger
from ..score.archetypes import load_archetypes
from ..seniority import (
    SENIORITY_DISTINGUISHED,
    SENIORITY_MID,
    SENIORITY_SENIOR,
    SENIORITY_STAFF,
)
from ..vocab import (
    CANONICAL_LANGUAGES,
    match_languages,
    metro_keys,
    metro_spans,
    vocab_hash,
)
from .coerce import coerce

log = get_logger("api.parse")

# Bumped when ARCHETYPE_KEYWORDS or heuristic extraction changes — or when the
# ParsedFilter dict shape changes (v3 added strict_language, v4 added
# include_unknown_experience, v5 added the wave-1 facet set and folded
# vocab_hash into the key): the cache stores
# to_dict() output, so a shape change without a bump would hydrate old rows
# into the new dataclass (and, on rollback, new rows into the old one). Folded
# into the cache key so parse_query cannot serve a row that was written under
# an older vocabulary or shape. Orphan rows in query_parse_cache are harmless.
PARSER_VERSION = 5

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

# The trailing ``s?`` is not about matching (``year`` already matched inside
# "years") — it is about the SPAN. heuristic_parse strips what it consumed from
# free_text, and a span ending at "year" left a stray "s" token behind for the
# tsquery to search on.
_EXPERIENCE_RANGE = re.compile(r"(\d+)\s*[-to]+\s*(\d+)\s*years?")
_EXPERIENCE_MIN = re.compile(r"(\d+)\s*\+?\s*years?")


@dataclass
class ParsedFilter:
    """The Boolean surface, wave 1.

    Every list field is a closed or sanitized vocabulary by the time it lands
    here — ``coerce`` is the only constructor the parsers use, so a value in
    this dataclass is already eligible to reach SQL.

    NOT here, on purpose: past_employers, schools, degrees, fields_of_study.
    Those columns exist (migration 013 derives them) but the plan puts them in
    wave 2, and a filter we cannot yet EVALUATE well is worse than no filter:
    it renders a UI row that is permanently "unknown", it consumes prompt
    budget teaching the model to emit it, and — because education is derived
    from LinkedIn only — it would be another source filter in disguise. The
    facet_dropped log in coerce.py is how we will learn whether recruiters
    actually ask for them before building them.
    """

    archetype_id: str | None = None
    metros: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    experience_min: float | None = None
    experience_max: float | None = None
    # --- wave 1: text facets (OR-chained LIKEs / array overlap in SQL) ---
    # Substrings, not exact titles: matched as `lower(s.title) like '%v%'`.
    titles: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    employers: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    # --- wave 1: ranges and enums ---
    tenure_min_months: int | None = None
    tenure_max_months: int | None = None
    # 'github' | 'linkedin' | 'both' — a deliberate source filter, which is the
    # only honest way to express one. Every other facet must NOT become one by
    # accident; that is what the unknown policies below are for.
    source: str | None = None
    require_code_evidence: bool = False
    # Ordinal IC ladder, 1..6 (see SENIORITY_FLOOR). Separate from is_manager
    # because "Engineering Manager" is not "senior IC" — collapsing the two is
    # what makes a seniority filter useless.
    seniority_min: int | None = None
    seniority_max: int | None = None
    is_manager: bool | None = None
    is_founder: bool | None = None
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
    # --- wave-1 unknown policies. §9 of the plan: "every facet needs a
    # source-aware unknown policy, or it becomes a source filter in disguise."
    # Each switch below names the population its default protects.
    #
    # topics is GitHub-derived and populated on ZERO of the 196,778 LinkedIn
    # rows, exactly like primary_language. Same shape of bug, same default:
    # unknown passes, strict is opt-in. strict_topics=True is a legitimate
    # request ("known to work on inference") that accepts losing 96% of the
    # corpus.
    strict_topics: bool = False
    # title is NULL on all 7,757 GitHub rows (derive has no title source for
    # them). A titles predicate with no unknown arm therefore means "and is a
    # LinkedIn row" — and title_exclude is worse, because SQL's three-valued
    # NOT makes `not (null like ...)` NULL, i.e. it deletes those rows in the
    # very filter meant to remove personal trainers. Unknown passes by default;
    # strict_titles=True narrows to rows whose title we actually read.
    strict_titles: bool = False
    # seniority_rank is derived from title, so it is NULL for all 7,757 GitHub
    # rows AND for every LinkedIn title carrying no level word — "Software
    # Engineer" is the single most common title shape in the corpus. Strict
    # here would make the most common query in the product ("senior backend
    # engineer", which heuristic_parse turns into seniority_min=3
    # automatically) silently delete a majority of the corpus: the language-
    # filter collapse again, one order of magnitude bigger. So this one
    # DEFAULTS TRUE — unknown passes — and a caller that genuinely means
    # "titled at or above senior" sets it False. Named include_unknown_* to
    # match include_unknown_experience; the default differs because the
    # populations differ (est_experience_years is unknown for a data reason,
    # seniority_rank is unknown for a SOURCE reason).
    include_unknown_seniority: bool = True
    # tenure_months is NULL on all 7,757 GitHub rows (derive.py sets it None
    # there) and on LinkedIn rows with no dated current role. Numeric band, so
    # it follows the include_unknown_experience precedent instead: an absence
    # of data is not evidence of 18 months in seat, and a tenure floor is
    # normally a hard requirement ("has been there 2+ years"). Strict by
    # default, loose on request — the cost is 3.8% of the corpus, stated here
    # so it is a decision rather than a surprise.
    include_unknown_tenure: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_predicate_summary(self) -> dict:
        """Facet name -> the coerced value that became a SQL predicate.

        Surfaced in run_search's ``diagnostics`` and read by the eval harness's
        ``filter_violations@k``. Without it, "why did this junk match?" is
        unanswerable from a response: the parse is cached, the LLM output is
        gone, and the WHERE clause is assembled per arm.

        Contract: only facets that actually reached ``_filter_clauses`` appear,
        and the value is what SQL saw (canonical metro keys, lowercased
        languages, sanitized LIKE bodies), NOT what the recruiter typed.
        ``free_text`` is absent because it is a ranking input, not a filter.

        Caveat for violation counting: the unknown policies above mean a
        returned row may legitimately hold NULL in a facet's column (titles,
        seniority, topics by default). A NULL is compliant; a non-NULL value
        that fails the predicate is the violation.
        """
        summary: dict = {}
        for name in (
            "archetype_id",
            "metros",
            "languages",
            "titles",
            "title_exclude",
            "employers",
            "markers",
            "topics",
            "source",
        ):
            value = getattr(self, name)
            if value:
                summary[name] = value
        for name in (
            "experience_min",
            "experience_max",
            "tenure_min_months",
            "tenure_max_months",
            "seniority_min",
            "seniority_max",
            "is_manager",
            "is_founder",
        ):
            value = getattr(self, name)
            if value is not None:
                summary[name] = value
        if self.require_code_evidence:
            summary["require_code_evidence"] = True
        return summary

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
    # vocab_hash() is part of the key, not just PARSER_VERSION: editing
    # data/metro_aliases.yaml or company_aliases.yaml changes what the SAME
    # query parses into, and without this a cached row would keep serving the
    # old vocabulary until someone remembered to bump the version by hand.
    # Cache rows are regenerable by definition, so over-invalidating is free.
    payload = f"{PARSER_VERSION}\n{vocab_hash()}\n{query.strip().lower()}".encode()
    return hashlib.sha256(payload).hexdigest()


# The IC seniority ladder, as a query word -> FLOOR on the 1..6 ordinal that
# derive stores in candidate_signals.seniority_rank:
#
#   1 junior/entry   2 mid   3 senior   4 staff   5 principal   6 distinguished
#
# Duplicated rather than imported: foray/seniority.py (added in this same
# stage, by the derive workstream) maps a STORED TITLE to a rank, which is the
# opposite direction from what a parser needs — a query word to a rank floor —
# over a different input distribution (recruiter phrasing, not LinkedIn title
# strings). Importing it would also put the derive-side module on the API
# import path for no shared code. The ladder VALUES are the shared contract and
# must match seniority.py's; test_search pins them so a drift shows up as a
# test failure rather than as a filter that quietly means something else.
# Query word -> the FLOOR it implies on seniority_rank. The ranks are IMPORTED
# from foray.seniority, never re-typed: this table is the query side of the
# same ladder derive writes into the column, and a one-rung disagreement is
# invisible (the predicate still runs, it just admits a rung too many). The
# first cut of this table did exactly that -- it read staff as 4 and senior as
# 3, one below the derive ranks -- so "senior backend engineer" admitted
# mid-level rows and "staff ML engineer" admitted seniors. Importing the
# constants makes that class of drift a NameError instead of a silent
# over-match.
#
# Note principal and staff share a rung, because derive's SENIORITY_STAFF is
# documented as "staff / lead / principal (IC reading)".
SENIORITY_FLOOR: tuple[tuple[re.Pattern[str], int], ...] = (
    # Longest/most specific first: "senior staff engineer" must be read as
    # staff, and _seniority_floor takes the LOWEST floor of everything matched
    # anyway (see there).
    (re.compile(r"(?<![a-z])(?:distinguished|fellow)(?![a-z])"), SENIORITY_DISTINGUISHED),
    (re.compile(r"(?<![a-z])principal(?![a-z])"), SENIORITY_STAFF),
    (re.compile(r"(?<![a-z])staff(?![a-z])"), SENIORITY_STAFF),
    (re.compile(r"(?<![a-z])(?:senior|sr\.?)(?![a-z])"), SENIORITY_SENIOR),
    (re.compile(r"(?<![a-z])mid[- ]?level(?![a-z])"), SENIORITY_MID),
)

# Founder NOUNS only. "founding" is deliberately absent: "founding ML
# engineer" is a stage descriptor — employee number three, not the person who
# incorporated the company — and turning it into `s.is_founder` would gate the
# single most important query in this product down to actual founders. The
# archetype keyword table reads "founding ML" for what it is (founding_ml)
# before any of this runs, and search_doc._NOISE already keeps "founding" out
# of the tsquery.
_FOUNDER = re.compile(r"(?<![a-z])(?:co-?founders?|founders?)(?![a-z])")

# Management framing -> is_manager, NEVER seniority_min. An Engineering Manager
# is not a senior IC; the two ranks are separate columns for exactly this
# reason. "lead" is excluded: it is ambiguous (tech lead vs people lead) and
# already in _NOISE.
_MANAGER = re.compile(
    r"(?<![a-z])(?:engineering manager|manager|managers|director of engineering"
    r"|directors?|vp of engineering|vp|head of engineering)(?![a-z])"
)


def _seniority_floor(q: str) -> tuple[int | None, list[tuple[int, int]]]:
    """Lowest floor named anywhere in the query, plus the spans to strip.

    Lowest, not highest: seniority_min is a hard predicate, so reading "senior
    or staff" as staff>=4 silently deletes every senior. Over-widening a filter
    is a ranking problem the arms can fix; over-narrowing one is invisible.
    """
    floors: list[int] = []
    spans: list[tuple[int, int]] = []
    for pattern, rank in SENIORITY_FLOOR:
        hits = list(pattern.finditer(q))
        if hits:
            floors.append(rank)
            spans.extend(hit.span() for hit in hits)
    return (min(floors) if floors else None), spans


def _strip_spans(query: str, lowered: str, spans: list[tuple[int, int]]) -> str:
    """Remove consumed spans from the query, returning the residue.

    This is the STRUCTURAL version of the _NOISE guarantee: a word that became
    a predicate cannot also reach the lexical tsquery, whether or not anyone
    remembers to add it to search_doc._NOISE. (_NOISE stays exactly as it is —
    it is still the backstop for the LLM parser and for a query consumed down
    to nothing, where run_search falls back to the raw text.)
    """
    if not spans:
        return query.strip()
    # str.lower() is length-preserving for ASCII but not for every codepoint
    # ('İ' lowercases to two characters), and the spans were found in the
    # lowered string. When the lengths disagree, slice the lowered text rather
    # than mis-slicing the original.
    source = query if len(query) == len(lowered) else lowered
    keep: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue  # overlapping match already consumed
        keep.append(source[cursor:start])
        cursor = end
    keep.append(source[cursor:])
    return " ".join("".join(keep).split())


def heuristic_parse(query: str) -> ParsedFilter:
    q = query.lower()

    # Archetype matching runs on the FULL query, before anything is consumed:
    # "founding ML engineer" must still reach founding_ml even though the
    # founder/seniority extraction below eats parts of that phrase.
    archetype_id = None
    for pattern, aid in ARCHETYPE_KEYWORDS:
        if re.search(pattern, q):
            archetype_id = aid
            break

    consumed: list[tuple[int, int]] = []

    # Metros are canonical keys now, not shard-grid strings. load_metros() was
    # the wrong table twice over: it is a GitHub harvest shard grid with
    # deliberate duplicate variants ("San Francisco", "SF Bay Area", "San
    # Francisco Bay Area" as three peers), and a bare `m.lower() in q`
    # substring test matched them without token boundaries.
    metro_hits = metro_spans(q)
    metros = list(dict.fromkeys(key for _s, _e, key in metro_hits))
    consumed.extend((start, end) for start, end, _key in metro_hits)

    # vocab.match_languages returns STORED forms (what derive writes, what the
    # SQL filter compares against). The previous implementation matched the
    # query against harvest's GitHub-qualifier list with rf"\b{lang}\b", which
    # was dead twice over: it emitted "cpp" where the column holds "c++", and
    # \b after '+' can never match "c++" at end-of-token anyway.
    #
    # Language spans are NOT consumed. Role-framing words become predicates;
    # TECHNOLOGY words stay lexical — "rust" is both a weak three-valued filter
    # and a term the tsvector/embedding should still see, because for 96% of
    # the corpus the lexical arm is the only place a language can match at all.
    languages = match_languages(q)

    seniority_min, seniority_spans = _seniority_floor(q)
    consumed.extend(seniority_spans)

    is_founder = None
    if (founder_hits := list(_FOUNDER.finditer(q))):
        is_founder = True
        consumed.extend(hit.span() for hit in founder_hits)

    is_manager = None
    if (manager_hits := list(_MANAGER.finditer(q))):
        is_manager = True
        consumed.extend(hit.span() for hit in manager_hits)

    experience_min = experience_max = None
    if (m := _EXPERIENCE_RANGE.search(q)) is not None:
        experience_min, experience_max = float(m.group(1)), float(m.group(2))
        consumed.append(m.span())
    elif (m := _EXPERIENCE_MIN.search(q)) is not None:
        experience_min = float(m.group(1))
        consumed.append(m.span())

    return coerce(
        {
            "archetype_id": archetype_id,
            "metros": metros,
            "languages": languages,
            "experience_min": experience_min,
            "experience_max": experience_max,
            "seniority_min": seniority_min,
            "is_founder": is_founder,
            "is_manager": is_manager,
            "free_text": _strip_spans(query, q, consumed),
        },
        query=query,
    )


def _llm_schema_prompt() -> str:
    """The schema the parse model sees.

    Closed vocabularies are inlined verbatim — metro KEYS, stored language
    spellings, marker names, archetype ids — because a model that cannot see
    the key set emits "SF Bay Area" or "Golang" and coerce drops it. Inlining
    them is the difference between a facet the model can use and a facet that
    logs parse.facet_dropped forever.

    The policy switches (strict_language, include_unknown_*, strict_topics,
    strict_titles) are NOT advertised: no natural-language phrase maps to
    them, and a model guessing one is a model choosing which source population
    to delete.
    """
    ids = ", ".join(load_archetypes())
    return (
        "Extract a candidate search filter as JSON. Keys, all optional:\n"
        f"archetype_id: one of [{ids}] or null.\n"
        f"metros: list of location KEYS, only from [{', '.join(metro_keys())}].\n"
        f"languages: list of programming languages, only from "
        f"[{', '.join(CANONICAL_LANGUAGES)}].\n"
        f"markers: list of domain tags, only from [{', '.join(sorted(load_markers()))}].\n"
        "topics: list of lowercase open-source topic words (e.g. vllm, kubernetes).\n"
        "titles: list of short job-title SUBSTRINGS to require (e.g. "
        '"machine learning engineer").\n'
        "title_exclude: list of title substrings to reject (e.g. \"recruiter\", "
        '"trainer").\n'
        "employers: list of company names.\n"
        "experience_min / experience_max: total years, numbers or null.\n"
        "tenure_min_months / tenure_max_months: months in the CURRENT role.\n"
        "seniority_min / seniority_max: 1=junior 2=mid 3=senior 4=staff "
        "5=principal 6=distinguished. Individual-contributor ladder only.\n"
        "is_manager: true if the query asks for people managers, false to "
        "exclude them, null if unstated. Never use seniority for management.\n"
        "is_founder: true only if the query asks for actual founders. "
        '"founding engineer" is a stage, not a founder.\n'
        "source: 'github', 'linkedin' or 'both', only if the query says so.\n"
        "require_code_evidence: true only if the query demands public code.\n"
        "free_text: the remaining semantic part of the query, with the words "
        "you turned into filters removed.\n"
        "Omit a key rather than guessing. Output ONLY the JSON object."
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
        if not isinstance(obj, dict):
            raise TypeError(f"parse model returned {type(obj).__name__}, not an object")
        # Everything the model said now goes through coerce, which is the
        # difference between this and the previous version: metros and
        # languages used to be spliced into SQL predicates unvalidated, so an
        # unmappable spelling became a predicate matching zero rows and a "%"
        # became a full scan.
        return coerce(obj, query=query)
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
