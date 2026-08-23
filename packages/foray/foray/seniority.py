"""Title -> (seniority_rank, mgmt_rank, is_founder). Pure: no DB, no settings.

Why derive-time and not query-time: an ordinal smallint on a btree supports
range predicates (``seniority_rank >= 4``), is unit-testable against the real
title distribution, and is computed once per profile offline instead of once
per query per row. The alternative — an LLM classification pass over 204,535
rows — was scoped at ~$61 and dropped because ``s.title`` already carries the
level word.

THREE columns, not one, because *"Engineering Manager" is not "senior IC"*.
Collapsing the management ladder into the IC ladder is precisely what makes a
seniority filter useless: a recruiter asking for a staff engineer does not want
directors, and a recruiter asking for an eng leader does not want a Staff
Accountant. So there are two independent ordinals plus a founder flag, and a
caller wanting pure ICs writes ``mgmt_rank is null``.

SCOPE, stated once because it is the thing this module keeps being asked to do
and must not: **seniority is a LEVEL, not a role.** "Staff Accountant" and
"Senior Personal Training Counselor" carry a real level word and get a real
rank here. Filtering them out of an engineering search is the job of the
titles/markers facets (``titles``, ``title_exclude``, ``domain_markers``), not
of this module. The LA Fitness incident — a personal training counselor
returned for "founding ML engineer" — was a *relevance* failure; trying to fix
relevance inside a level parser just moves the bug somewhere with no test.

Source reality this module is designed around (measured on prod): ``s.title``
is populated on ZERO of the 7,757 GitHub rows, because derive_signals sets
``title=None``. A seniority column derived from titles alone would therefore be
NULL for every GitHub row, and ``seniority_rank >= 4`` would silently delete the
entire GitHub half of the corpus — the "facet becomes a source filter in
disguise" failure. Hence ``seniority_from_experience`` and the
``seniority_source`` column that keeps the two readings distinguishable.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------- the ladders --
#
# Documented as module constants because three other layers order-compare these
# numbers (the facet filter, the eval's ``seniority_min`` label, the API's
# parse echo) and a renumbering would silently change every stored row's
# meaning without changing a single type.

# IC ladder. NULL (absent) means unknown, never "junior" — the whole point of
# stage 2's est_experience_years fix was that unknown-as-zero read as juniors.
SENIORITY_INTERN = 1
SENIORITY_ENTRY = 2  # entry / junior / associate / new grad
SENIORITY_MID = 3
SENIORITY_SENIOR = 4
SENIORITY_STAFF = 5  # staff / lead / principal (IC reading)
SENIORITY_DISTINGUISHED = 6  # distinguished / technical fellow

# Management ladder. NULL means "not management" — which is the common case, so
# ``mgmt_rank is not null`` is itself the is_manager predicate.
MGMT_MANAGER = 1
MGMT_DIRECTOR = 2  # director, and "Head of X"
MGMT_VP = 3
MGMT_CHIEF = 4  # CxO / chief

SENIORITY_MAX = SENIORITY_DISTINGUISHED
MGMT_MAX = MGMT_CHIEF

# Token boundaries are hand-rolled rather than ``\b`` for the same reason
# vocab.py does it: ``\b`` is defined relative to ``\w``, so ``\b`` after a "."
# in "sr." demands a following word character and never matches at end of
# string. "not glued to another alphanumeric" is what we actually mean.
_L = r"(?<![a-z0-9])"
_R = r"(?![a-z0-9])"


def _re(body: str) -> re.Pattern[str]:
    # The non-capturing group is load-bearing: without it the boundaries would
    # bind to the FIRST and LAST alternative only ("(?<!..)a|b|c(?!..)"), so
    # every middle alternative would match unbounded — "sr" inside "user",
    # "vp" inside "vpn". Every pattern below is an alternation, so this is not
    # a hypothetical.
    return re.compile(_L + "(?:" + body + ")" + _R)


# ------------------------------------------------------------------ founder --
#
# "founder" / "co-founder" / "cofounder" in any position is unambiguous.
#
# "founding" ALONE is deliberately not enough: "founding member of the choir",
# "founding board member", "founding donor" are all real LinkedIn titles and
# none of them is a startup founder. So the bare-adjective reading is rejected
# and we require the founding-<role> shape. The cost is a miss on an exotic
# founding-<something> we did not list, which is a NULL flag on one row; the
# cost of the loose reading is a founder facet full of choir members.
_FOUNDER_RE = _re(r"(?:co[-\s]?)?founders?")
_FOUNDING_ROLE_RE = _re(
    r"founding\s+(?:"
    r"engineer(?:ing)?s?|developer|dev|designer|scientist|researcher|architect|"
    r"partner|principal|c(?:t|e|o|f|m|p|r)o|"
    # "founding member of technical staff" is the OpenAI/Anthropic shape and IS
    # a founding engineer; bare "founding member" is the choir. Spelled out so
    # the distinction is visible rather than emergent.
    r"member\s+of\s+(?:the\s+)?technical\s+staff|"
    r"software|data|product|ml|ai|team"
    r")"
)


def _is_founder(text: str) -> bool:
    return bool(_FOUNDER_RE.search(text) or _FOUNDING_ROLE_RE.search(text))


# ------------------------------------------------------------- the IC ladder --
#
# Ordered MOST SPECIFIC / HIGHEST FIRST and short-circuited, so "Senior Staff
# Engineer" is staff (5) rather than senior (4), and "Senior Principal
# Engineer" is 5 rather than 4.

# "Distinguished Engineer" is unambiguous at every company that uses it.
_DISTINGUISHED_RE = _re(r"distinguished")

# "Fellow" splits hard on context, and the split is an ALLOWLIST because the
# measurement says so. Reading the bare word as rank 6 gave 4 rank-6 rows on
# the 2,000-record sample, and 3 of them were medical fellowships — "Women's
# Imaging Fellow", "Pediatric and Adolescent Gynecology Fellow", "Healthcare
# Leadership Fellow" — which are EARLY-career training posts, the opposite of
# what rank 6 means. A subtractive list of academic qualifiers cannot cover
# that: every medical specialty is its own qualifier. So the top IC rung is
# claimed only by the enumerated technical/institutional forms plus a title
# that is nothing but the word (the IBM/Microsoft/Sun "Fellow"), and a medical
# fellow falls through to whatever else the title says, usually NULL.
_FELLOW_RANK_RE = _re(
    r"(?:technical|technology|engineering|distinguished|principal|senior|"
    r"chief|ai|research\s+staff)\s+fellows?"
)
_BARE_FELLOW_RE = re.compile(r"^fellows?$")

# "principal" is rank 5 as an IC ladder step ("Principal Engineer"), but
# "Principal" in consulting, law, architecture and actuarial firms means
# partner-level MANAGEMENT. The collision is accepted and resolved in favour of
# the IC reading, for two reasons: the corpus is engineering-skewed, and a
# false IC-5 costs a wrong rank on a row the role facets will filter anyway,
# whereas a false mgmt-4 would put a consultant in front of a recruiter asking
# for eng leadership. Consulting principals are still reachable — their titles
# usually also carry "Partner" or a practice name for the title facet.
#
# "lead": matched as a word, plus "leader" — "Tech Lead", "Lead Engineer",
# "Team Leader". Two exclusions, both real title shapes: "Lead Generation
# Specialist" (a sales role where "lead" is a noun meaning prospect) and
# "Thought Leader" (a self-description, not a rung).
_STAFF_RE = _re(r"staff|principal|leads?|leaders?")
# Also subtracts "Chief of Staff": the whole phrase is one title and the "staff"
# in it is the exec's office, not an IC rung.
# ("staffing" needs no entry — the right token boundary already stops
# _STAFF_RE from firing inside it.)
_STAFF_NOT_A_RUNG_RE = _re(
    r"lead\s+gen(?:eration)?|thought\s+leaders?|chief\s+of\s+staff"
)

_SENIOR_RE = _re(r"senior|sr\.?|snr\.?")
_MID_RE = _re(r"mid[-\s]?level|midlevel|intermediate")

# "associate" is rank 2 as a bare modifier ("Associate Software Engineer"), but
# "Associate Director" / "Associate VP" / "Associate Professor" / "Associate
# Partner" are senior compounds where it is a *demotion of a high rung*, not an
# entry level. Those are excluded here and picked up by the mgmt ladder.
_JUNIOR_RE = _re(
    r"junior|jr\.?|entry[-\s]level|new\s+grad(?:uate)?|trainee|apprentice|"
    r"graduate|associate(?!\s+(?:director|vice|vp|partner|professor|dean|"
    r"principal|general\s+counsel))"
)
_INTERN_RE = _re(r"intern(?:ship)?s?|co[-\s]?op")

# Level suffixes. Roman and arabic both appear in the export ("Engineer III",
# "Software Engineer 2"). Anchored at the END of the title (optionally before
# trailing punctuation) because mid-string numerals are usually something else
# ("Tier 1 Support", "Level 3 Escalation Analyst" — where the numeral describes
# the queue, not the person). Applied ONLY when no ladder word matched, so
# "Senior Engineer II" stays 4 rather than being pulled down to 3.
#
# The mapping clamps at the middle of the band on purpose: numerals are
# company-specific (IV is senior at some shops, staff at others), so IV reads
# as senior rather than inventing a staff engineer.
_LEVEL_SUFFIX_RE = re.compile(
    r"(?<![a-z0-9])(?:level\s*)?(i{1,3}|iv|v|[1-5])(?![a-z0-9])[\s.,)\-]*$"
)
_LEVEL_SUFFIX_RANK = {
    "i": SENIORITY_ENTRY, "1": SENIORITY_ENTRY,
    "ii": SENIORITY_MID, "2": SENIORITY_MID,
    "iii": SENIORITY_SENIOR, "3": SENIORITY_SENIOR,
    "iv": SENIORITY_SENIOR, "4": SENIORITY_SENIOR,
    "v": SENIORITY_STAFF, "5": SENIORITY_STAFF,
}


def _ic_rank(text: str) -> int | None:
    if _DISTINGUISHED_RE.search(text):
        return SENIORITY_DISTINGUISHED
    if _FELLOW_RANK_RE.search(text) or _BARE_FELLOW_RE.match(text):
        return SENIORITY_DISTINGUISHED
    if _STAFF_RE.search(text) and not _only_excluded(text, _STAFF_RE, _STAFF_NOT_A_RUNG_RE):
        return SENIORITY_STAFF
    if _SENIOR_RE.search(text):
        return SENIORITY_SENIOR
    if _MID_RE.search(text):
        return SENIORITY_MID
    if _JUNIOR_RE.search(text):
        return SENIORITY_ENTRY
    if _INTERN_RE.search(text):
        return SENIORITY_INTERN
    suffix = _LEVEL_SUFFIX_RE.search(text)
    if suffix:
        return _LEVEL_SUFFIX_RANK.get(suffix.group(1))
    return None


def _only_excluded(
    text: str, hit_re: re.Pattern[str], exclusion_re: re.Pattern[str]
) -> bool:
    """True when every occurrence of ``hit_re`` sits inside an exclusion match.

    SPAN CONTAINMENT, not "does the exclusion appear anywhere", for two
    reasons. First, it excuses exactly the occurrences that are excused:
    "Lead Engineer, Lead Generation" still reads as a lead, because one of the
    two "lead" hits is outside every exclusion span. Second, containment works
    whichever side the qualifier is on — the exclusion may be a PREFIX of the
    token ("product manager" containing "manager") or a SUFFIX ("lead
    generation" containing "lead") — where an end-offset comparison would only
    have handled the prefix case and would have silently ranked every lead-gen
    salesperson as a staff engineer.
    """
    spans = [(m.start(), m.end()) for m in exclusion_re.finditer(text)]
    return all(
        any(start <= m.start() and m.end() <= end for start, end in spans)
        for m in hit_re.finditer(text)
    )


# ----------------------------------------------------- the management ladder --

# CxO. Acronyms are enumerated rather than matched as ``c\w\wo``, which would
# swallow "coo" inside real words and match nonsense like "cabo".
_CHIEF_ACRONYM_RE = _re(
    r"c(?:to|eo|fo|oo|mo|po|ro|io|do|so|lo|iso|hro|xo)"
)
# Bare "chief" covers "Chief Technology Officer", "Chief of Engineering",
# "Chief Scientist". Two subtractions: "Chief of Staff" is an individual
# contributor role attached to an exec, not the exec; and "Chief Engineer" /
# "Chief Mate" are maritime/aerospace IC titles. Both are common enough in the
# export to be worth naming.
_CHIEF_RE = _re(r"chief")
_CHIEF_NOT_EXEC_RE = _re(r"chief\s+(?:of\s+staff|engineer|mate|petty\s+officer|resident)")

# VP. "SVP"/"EVP" are higher rungs of the same ladder and share rank 3 —
# splitting them would need a fifth mgmt level for no filter anyone asks for.
# "AVP" (assistant vice president) is a *junior* banking rank that maps here
# too; accepted, and the same accepted collision as banking's "Vice President",
# which is an IC rank at every investment bank ("Vice President, Senior
# Business Analyst" is verbatim from the sample). This module reads titles, not
# org charts; the employer facet is what separates a bank VP from a tech VP.
_VP_RE = _re(r"[se]?vp|avp|v\.?p\.?|vice[-\s]pres(?:ident)?")

# Director, and "Head of X" -> the same rung. A head-of is scoped ownership of
# a function reporting to an exec, which is a director in every org chart we
# care about; giving it its own rung would make ``mgmt_rank >= 2`` mean
# different things in different companies.
#
# "Managing Director" is director (2) even though in banking it outranks VP;
# same accepted collision as above. "Art Director" / "Creative Director" /
# "Director of Photography" are IC design and film titles — accepted as mgmt 2,
# because the alternative is an exclusion list that grows forever and because
# the role facets are the right place to say "not a film crew".
_DIRECTOR_RE = _re(r"directors?|dir\.|head\s+of|department\s+head")

# Manager — the single biggest trap in this module. "Product Manager",
# "Program Manager", "Project Manager", "Account Manager" and friends are
# INDIVIDUAL CONTRIBUTOR roles: nobody reports to them. Treating them as
# management would put the entire PM population into ``is_manager``, which on
# this corpus is a large, wrong answer to "show me engineering leaders".
# So the qualifier is enumerated and the occurrence is position-matched:
# "Senior Product Manager" -> no mgmt rank, "Engineering Manager" -> 1,
# "Product Manager and Engineering Manager" -> 1.
_MANAGER_RE = _re(r"managers?|mgr\.?|supervisors?|superintendent|foreman")
_IC_MANAGER_RE = _re(
    r"(?:product|program|programme|project|account|marketing|brand|community|"
    r"content|social\s+media|category|portfolio|asset|property|case|talent|"
    r"hiring|recruiting|delivery|release|partner|channel|inventory|traffic|"
    r"campaign|event|office|business\s+development|customer\s+success|"
    r"technical\s+program|technical\s+project|scrum)\s+managers?"
)


def _mgmt_rank(text: str) -> int | None:
    if _CHIEF_ACRONYM_RE.search(text):
        return MGMT_CHIEF
    if _CHIEF_RE.search(text) and not _only_excluded(text, _CHIEF_RE, _CHIEF_NOT_EXEC_RE):
        return MGMT_CHIEF
    if _VP_RE.search(text):
        return MGMT_VP
    if _DIRECTOR_RE.search(text):
        return MGMT_DIRECTOR
    if _MANAGER_RE.search(text) and not _only_excluded(text, _MANAGER_RE, _IC_MANAGER_RE):
        return MGMT_MANAGER
    return None


_WS_RE = re.compile(r"\s+")


def parse_seniority(title: str | None) -> tuple[int | None, int | None, bool]:
    """``(seniority_rank, mgmt_rank, is_founder)`` from a job title.

    HYBRID TITLES set BOTH ordinals. "Senior Engineering Manager" returns
    ``(4, 1, False)``: mgmt_rank carries the management reading, and the IC
    rank is still recorded because the title genuinely states a level. The rule
    is uniform — *whenever a level word is present we record it, whenever a
    management word is present we record that too* — rather than a per-shape
    special case, so "Senior Director" is ``(4, 2, False)`` and "VP of
    Engineering" is ``(None, 3, False)`` with no IC claim invented for it.
    Consumers disambiguate: pure-IC searches add ``mgmt_rank is null``, and
    manager searches read ``mgmt_rank`` alone.

    ``(None, None, False)`` for None, empty or unreadable titles. Unknown must
    look unknown; see the est_experience_years lesson in enrich/derive.py.
    """
    if not title:
        return None, None, False
    text = _WS_RE.sub(" ", title.strip().lower())
    if not text:
        return None, None, False
    return _ic_rank(text), _mgmt_rank(text), _is_founder(text)


# ---------------------------------------------------- the GitHub-side proxy --

# Bands, as module constants so the eval and the parse echo can describe them.
# Chosen to line up with the IC ladder's own meaning rather than with any
# distribution: under two years is nobody's senior engineer; eight years is the
# earliest a staff title is common.
EXPERIENCE_BANDS: tuple[tuple[float, int], ...] = (
    (2.0, SENIORITY_ENTRY),
    (5.0, SENIORITY_MID),
    (8.0, SENIORITY_SENIOR),
)
EXPERIENCE_BAND_TOP = SENIORITY_STAFF


def seniority_from_experience(years: float | None) -> int | None:
    """Years of experience -> an IC rank. THIS IS A PROXY, NOT A TITLE READING.

    It exists for one measured reason: ``s.title`` is NULL on all 7,757 prod
    GitHub rows, so a title-only seniority column would make
    ``seniority_rank >= 4`` mean "and is a LinkedIn row" — the exact silent
    source-deletion that ``primary_language = any(...)`` already performs
    against the 196,778 LinkedIn rows. A facet must not become a source filter
    in disguise.

    It is a much weaker claim than a title. Ten years of GitHub account age is
    not a staff engineer; it is ten years of account age. That is WHY the
    ``seniority_source`` column exists: a consumer that must not conflate the
    two reads ``seniority_source = 'title'``, a consumer that wants recall
    accepts ``'experience_proxy'`` too, and neither has to guess. Never
    overwrite a ``'title'`` reading with this — see merge_signals.

    None in, None out: unknown experience yields unknown seniority. Mapping
    NULL to a rank here would recreate stage 2's unknown-as-zero bug (134,536
    rows storing 0 that every consumer read as "zero years") one layer up.
    """
    if years is None:
        return None
    for ceiling, rank in EXPERIENCE_BANDS:
        if years < ceiling:
            return rank
    return EXPERIENCE_BAND_TOP
