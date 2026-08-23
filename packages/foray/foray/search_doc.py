"""Canonical lexical-search document: one tsvector config, one weighted
expression, one query builder.

``to_tsvector`` and ``websearch_to_tsquery`` MUST use the same configuration.
A mismatch does not error; it silently returns zero rows. This module is the
single source so derive, backfill, and the query path cannot drift.

The two-argument ``to_tsvector(regconfig, text)`` form is required: the
one-argument form reads a GUC and is only STABLE, not IMMUTABLE.
"""

from __future__ import annotations

import re

TS_CONFIG = "english"

# ts_rank_cd weight array is {D, C, B, A}. A = structured facts recruiters
# search on; B = curated vocabularies; C = repo evidence; D = free prose.
# Default Postgres weights already match this; we pass them explicitly so a
# future retune is a Python constant, not a hunt through SQL.
RANK_WEIGHTS = "{0.1,0.2,0.4,1.0}"

# Normalisation 32: bound the rank to 0..1, no length division. Length-
# normalising systematically favours thin profiles over rich ones.
RANK_NORMALIZATION = 32

# SQL aliases: s = candidate_signals, c = candidates, gh/li = raw_profiles
# rows (nullable). Class D prose is optional: the fast backfill omits the
# raw_profiles joins and still produces a usable document.
SEARCH_TSV_SQL = f"""(
    setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.title, '')), 'A')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.employer, '')), 'A')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.employer_domain, '')), 'A')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig,
         coalesce(array_to_string(s.topics, ' '), '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig,
         coalesce(array_to_string(s.domain_markers, ' '), '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.primary_language, '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.secondary_language, '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce((
         select string_agg(coalesce(elem->>'name', '') || ' ' || coalesce(elem->>'description', ''), ' ')
         from jsonb_array_elements(coalesce(s.top_repos, '[]'::jsonb)) elem
       ), '')), 'C')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(c.name, '')), 'D')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig,
         coalesce(gh.data->>'bio', '')), 'D')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig,
         coalesce(li.data->>'about', '') || ' ' || coalesce(li.data->>'position', '')), 'D')
)"""

# Fast pass: A/B/C + c.name. No TOAST of raw_profiles JSONB.
SEARCH_TSV_SQL_FAST = f"""(
    setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.title, '')), 'A')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.employer, '')), 'A')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.employer_domain, '')), 'A')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig,
         coalesce(array_to_string(s.topics, ' '), '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig,
         coalesce(array_to_string(s.domain_markers, ' '), '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.primary_language, '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(s.secondary_language, '')), 'B')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce((
         select string_agg(coalesce(elem->>'name', '') || ' ' || coalesce(elem->>'description', ''), ' ')
         from jsonb_array_elements(coalesce(s.top_repos, '[]'::jsonb)) elem
       ), '')), 'C')
    || setweight(to_tsvector('{TS_CONFIG}'::regconfig, coalesce(c.name, '')), 'D')
)"""

UPDATE_SEARCH_TSV_SQL = f"""
update candidate_signals s
   set search_tsv = {SEARCH_TSV_SQL}
  from candidates c
  left join raw_profiles gh on gh.candidate_id = c.id and gh.source = 'github'
  left join raw_profiles li on li.candidate_id = c.id and li.source = 'linkedin'
 where s.candidate_id = c.id
   and s.candidate_id = any($1::bigint[])
"""

UPDATE_SEARCH_TSV_FAST_SQL = f"""
update candidate_signals s
   set search_tsv = {SEARCH_TSV_SQL_FAST}
  from candidates c
 where s.candidate_id = c.id
   and s.search_tsv is null
   and s.candidate_id = any($1::bigint[])
"""

# "Has a lexical document", the single source for COUNTING, for the same
# reason this module is the single source for deriving and querying: a
# definition that drifts across call sites does not error, it silently
# reports a different corpus. A bare ``search_tsv is not null`` overcounts:
# an admitted profile with no title/employer/topics — and an empty name —
# still gets a non-NULL tsvector from the setweight concatenation above,
# it is just EMPTY, i.e. "was indexed", not "is findable". The lexical arm
# (``@@``) can never match such a row, so health probes and n_lexical_indexed
# must not count it either. The ``s`` alias is baked in because every SQL
# fragment in this module already assumes s = candidate_signals (see the
# alias note above SEARCH_TSV_SQL); count-only call sites add the alias to
# their query rather than this module keeping a second alias-free variant
# that could drift from this one.
HAS_LEXICAL_DOC_SQL = "s.search_tsv is not null and s.search_tsv <> ''::tsvector"

# Function words and recruiter-generic tokens that match nearly every document
# in this corpus. Years/counts are already structured filters.
#
# Boundary rule for what belongs here: words that describe the ROLE or the ASK
# (recruiter framing: "founding", "shipped", "hiring", "strong background")
# are noise; words that describe the candidate's TECHNOLOGY or domain
# ("training", "infrastructure", "platform", "research", "data", "backend",
# "frontend", "mobile", "security") are never noise, however often they
# collide with unrelated titles and employer names. Motivating incident: for
# "founding ML engineer, shipped training infrastructure", the A-weighted
# title/employer fields (ts_rank_cd has no IDF) let a personal training
# counselor at LA Fitness (title hits 'training') and "Infrastructure United
# Inc." (employer name hits 'infrastructure') outrank real ML profiles whose
# matches were C/D-weight technical terms. The fix is to strip the framing
# words around them — 'training' and 'infrastructure' themselves stay
# searchable because they are legitimately load-bearing in queries like
# "ML training infrastructure"; the archetype retrieval gate handles the
# title-collision problem for archetype queries.
_NOISE = {
    "a",
    "an",
    "and",
    "at",
    "background",
    "build",
    "building",
    "builds",
    "built",
    "candidate",
    "candidates",
    "cofounder",
    "companies",
    "company",
    "early",
    "engineer",
    "engineering",
    "engineers",
    "experience",
    "experienced",
    "expert",
    "experts",
    "for",
    "founder",
    "founding",
    "from",
    "has",
    "have",
    "hire",
    "hiring",
    "ideally",
    "in",
    "job",
    "jobs",
    "join",
    "joining",
    "lead",
    "leading",
    "leads",
    "looking",
    "must",
    "of",
    "or",
    "people",
    "plus",
    "principal",
    "role",
    "roles",
    "seeking",
    "senior",
    "ship",
    "shipped",
    "shipping",
    "ships",
    "someone",
    "staff",
    "stage",
    "startup",
    "startups",
    "strong",
    "team",
    "teams",
    "that",
    "the",
    "this",
    "to",
    "who",
    "with",
    "work",
    "worked",
    "working",
    "works",
    "year",
    "years",
}

# Tokens that keep intra-token punctuation meaningful in this vocabulary.
_TERM = re.compile(r"[a-z0-9][a-z0-9+.#/-]*")
_QUOTED = re.compile(r'"([^"]*)"')
# websearch_to_tsquery treats these as operators; never pass them through bare.
_OPERATORISH = re.compile(r"^[&|!():*<>]+$")


def build_websearch_or(text: str, *, max_terms: int = 12) -> str | None:
    """Build an OR websearch string, or None when nothing usable remains.

    None means "run the vector arm alone", never "empty result set".
    ``--prefixed`` tokens are stripped rather than applied: a negation on only
    the lexical arm is incoherent while the vector arm still retrieves the
    excluded candidate.

    Tokens containing ``+`` (c++) are quoted so websearch_to_tsquery does not
    split them into a bare ``c``.
    """
    if not text or not str(text).strip():
        return None
    raw = str(text).lower()
    # Strip --negation / -bare-token prefixes rather than mis-applying them.
    raw = re.sub(r"(^|\s)--?\S+", " ", raw)

    phrases: list[str] = []

    def _keep_quote(match: re.Match[str]) -> str:
        phrase = match.group(1).strip()
        if phrase:
            phrases.append(phrase)
        return " "

    remainder = _QUOTED.sub(_keep_quote, raw)

    terms: list[str] = []
    seen: set[str] = set()
    for token in _TERM.findall(remainder):
        if token.isdigit() or token in _NOISE or _OPERATORISH.fullmatch(token):
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)

    parts: list[str] = []
    for phrase in phrases:
        parts.append(f'"{phrase}"')
        if len(parts) >= max_terms:
            break
    if len(parts) < max_terms:
        for token in terms:
            parts.append(f'"{token}"' if any(ch in token for ch in "+#") else token)
            if len(parts) >= max_terms:
                break
    if not parts:
        return None
    return " or ".join(parts)
