"""Deterministic, explainable fit scoring.

Each requirement in the parsed query becomes a `Criterion` with a status, a
score and a piece of evidence pulled from the profile. The weighted mean is the
heuristic score; the criteria list is what the UI renders as "why this person".

This layer runs with no model available, which is what keeps the product honest:
the LLM reranker adjusts the ordering, it does not invent the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..enrich import taxonomy as tx
from .index import CandidateDoc
from .parser import ParsedQuery

Status = str  # "strong" | "partial" | "miss" | "unknown"

WEIGHTS = {
    "title": 2.0,
    "must_skill": 2.0,
    "nice_skill": 0.8,
    "seniority": 1.2,
    "experience": 1.2,
    "location": 1.5,
    "industry": 0.9,
    "company_stage": 0.6,
    "company_size": 0.5,
    "target_company": 1.0,
    "education": 0.6,
    "keyword": 0.5,
    "freeform": 1.0,
}


@dataclass
class Criterion:
    name: str
    label: str
    kind: str
    weight: float
    status: Status
    score: float
    evidence: str = ""
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "weight": self.weight,
            "status": self.status,
            "score": round(self.score, 4),
            "evidence": self.evidence,
            "sources": self.sources,
        }


@dataclass
class FitAssessment:
    score: float
    criteria: list[Criterion]

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "criteria": [c.to_dict() for c in self.criteria],
        }

    @property
    def misses(self) -> list[Criterion]:
        return [c for c in self.criteria if c.status == "miss"]


def _status_for(score: float) -> Status:
    if score >= 0.8:
        return "strong"
    if score >= 0.4:
        return "partial"
    return "miss"


# --------------------------------------------------------------------------
# individual criteria
# --------------------------------------------------------------------------


def _score_title(doc: CandidateDoc, wanted: list[str]) -> Criterion | None:
    if not wanted:
        return None
    best_score = 0.0
    best_evidence = ""
    for target in wanted:
        current = tx.title_similarity(doc.current_title_normalized, target)
        if current > best_score:
            best_score = current
            best_evidence = f"Current title “{doc.current_title or '—'}” vs “{target}”"
        for entry in doc.companies:
            past = tx.title_similarity(entry.get("title_normalized", ""), target) * 0.85
            if past > best_score:
                best_score = past
                best_evidence = (
                    f"Held “{entry.get('title', '')}” at {entry.get('company', '')}"
                )
    return Criterion(
        name="title",
        label=f"Title: {', '.join(wanted)}",
        kind="title",
        weight=WEIGHTS["title"],
        status=_status_for(best_score),
        score=best_score,
        evidence=best_evidence or "No comparable title found",
    )


def _score_skills(doc: CandidateDoc, skills: list[str], *, required: bool) -> list[Criterion]:
    kind = "must_skill" if required else "nice_skill"
    weight = WEIGHTS[kind]
    criteria: list[Criterion] = []
    for skill in skills:
        strength = tx.skill_match_strength(skill, doc.skills, doc.haystack)
        if strength >= 1.0:
            evidence = f"Lists {skill} as a skill"
        elif strength >= 0.7:
            neighbours = tx.SKILL_NEIGHBOURS.get(tx.normalize_skill(skill), set()) & doc.skills
            evidence = f"Adjacent experience: {', '.join(sorted(neighbours))}"
        elif strength > 0:
            evidence = f"“{skill}” appears in their profile text"
        else:
            evidence = f"No evidence of {skill}"
        criteria.append(
            Criterion(
                name=f"{kind}:{skill}",
                label=("Must have " if required else "Nice to have ") + skill,
                kind=kind,
                weight=weight,
                status=_status_for(strength),
                score=strength,
                evidence=evidence,
            )
        )
    return criteria


def _score_seniority(doc: CandidateDoc, wanted: list[str]) -> Criterion | None:
    if not wanted:
        return None
    best = 0.0
    evidence = f"Seniority read as {doc.seniority}"
    for target in wanted:
        distance = tx.seniority_distance(doc.seniority, target)
        if distance is None:
            score = 0.3
        elif distance == 0:
            score = 1.0
        elif distance > 0:
            # Over-levelled is a smaller problem than under-levelled: a principal
            # answering a senior req is a stretch, not a mismatch.
            score = {1: 0.85, 2: 0.6, 3: 0.35}.get(distance, 0.15)
        else:
            score = {1: 0.55, 2: 0.25}.get(-distance, 0.0)
        if score > best:
            best = score
            evidence = f"{doc.seniority} vs requested {target}"
    return Criterion(
        name="seniority",
        label=f"Seniority: {', '.join(wanted)}",
        kind="seniority",
        weight=WEIGHTS["seniority"],
        status=_status_for(best),
        score=best,
        evidence=evidence,
    )


def _score_experience(doc: CandidateDoc, parsed: ParsedQuery) -> Criterion | None:
    low, high = parsed.min_years_experience, parsed.max_years_experience
    if low is None and high is None:
        return None
    years = doc.years_experience
    score = 1.0
    if low is not None and years < low:
        deficit = low - years
        score = max(0.0, 1.0 - deficit / max(low, 1.0))
    if high is not None and years > high:
        surplus = years - high
        score = min(score, max(0.0, 1.0 - surplus / max(high, 1.0)))
    band = (
        f"{low:g}+" if low is not None and high is None
        else f"up to {high:g}" if low is None
        else f"{low:g}-{high:g}"
    )
    return Criterion(
        name="experience",
        label=f"Experience: {band} years",
        kind="experience",
        weight=WEIGHTS["experience"],
        status=_status_for(score),
        score=score,
        evidence=f"{years:g} years of tracked experience",
    )


def _score_location(doc: CandidateDoc, parsed: ParsedQuery) -> Criterion | None:
    if not parsed.locations and parsed.remote_ok is None:
        return None
    if not parsed.locations:
        # remote_ok=False means "in the office", not "no preference". Scoring the
        # candidate's flag without reading the request handed a perfect 1.0 to
        # the remote-only people an onsite search is trying to rule out.
        wants_remote = bool(parsed.remote_ok)
        score = 1.0 if doc.open_to_remote is wants_remote else 0.35
        if wants_remote:
            evidence = "Flagged open to remote" if doc.open_to_remote else "No remote signal"
        else:
            evidence = (
                "Flagged open to remote; the role is onsite"
                if doc.open_to_remote
                else "No remote flag on file"
            )
        return Criterion(
            name="location",
            label="Remote friendly" if wants_remote else "Onsite",
            kind="location",
            weight=WEIGHTS["location"],
            status=_status_for(score),
            score=score,
            evidence=evidence,
        )

    where = ", ".join(doc.location_parts) or "location not stated"
    best = 0.0
    best_target = parsed.locations[0]
    for wanted in parsed.locations:
        score = tx.location_match(doc.location_parts, wanted)
        if score > best:
            best, best_target = score, wanted
    evidence = f"Based in {where} (target {best_target})"
    if best < 0.75 and parsed.remote_ok and doc.open_to_remote:
        best = max(best, 0.7)
        evidence += " — open to remote"
    return Criterion(
        name="location",
        label=f"Location: {', '.join(parsed.locations)}",
        kind="location",
        weight=WEIGHTS["location"],
        status=_status_for(best),
        score=best,
        evidence=evidence,
    )


def _score_industry(doc: CandidateDoc, wanted: list[str]) -> Criterion | None:
    if not wanted:
        return None
    hits = {i for i in wanted if i in doc.industries}
    if hits:
        score, evidence = 1.0, f"Worked in {', '.join(sorted(hits))}"
    else:
        loose = [i for i in wanted if f" {i} " in f" {doc.haystack} "]
        score = 0.5 if loose else 0.0
        evidence = (
            f"“{', '.join(loose)}” mentioned in profile" if loose else "No matching industry"
        )
    return Criterion(
        name="industry",
        label=f"Industry: {', '.join(wanted)}",
        kind="industry",
        weight=WEIGHTS["industry"],
        status=_status_for(score),
        score=score,
        evidence=evidence,
    )


def _score_company_attribute(
    doc: CandidateDoc, wanted: list[str], *, key: str, kind: str, label: str
) -> Criterion | None:
    if not wanted:
        return None
    targets = set(wanted)
    matches = [
        entry for entry in doc.companies if entry.get(key) and entry.get(key) in targets
    ]
    if matches:
        score = 1.0 if any(e.get("is_current") for e in matches) else 0.8
        evidence = "; ".join(
            f"{e.get('company')} ({e.get(key)})" for e in matches[:3]
        )
    else:
        score, evidence = 0.0, f"No employer matched {label.lower()}"
    return Criterion(
        name=kind,
        label=f"{label}: {', '.join(wanted)}",
        kind=kind,
        weight=WEIGHTS[kind],
        status=_status_for(score),
        score=score,
        evidence=evidence,
    )


def _score_target_companies(doc: CandidateDoc, wanted: list[str]) -> Criterion | None:
    if not wanted:
        return None
    targets = {tx.normalize_company(c) for c in wanted}
    matches = [
        entry for entry in doc.companies if entry.get("company_normalized") in targets
    ]
    if matches:
        current = any(e.get("is_current") for e in matches)
        score = 1.0 if current else 0.85
        evidence = ("Currently at " if current else "Previously at ") + ", ".join(
            e.get("company", "") for e in matches[:3]
        )
    else:
        score, evidence = 0.0, "Has not worked at the named companies"
    return Criterion(
        name="target_company",
        label=f"Company: {', '.join(wanted)}",
        kind="target_company",
        weight=WEIGHTS["target_company"],
        status=_status_for(score),
        score=score,
        evidence=evidence,
    )


def _score_education(doc: CandidateDoc, parsed: ParsedQuery) -> Criterion | None:
    wanted_any = parsed.schools + parsed.degrees + parsed.fields_of_study
    if not wanted_any:
        return None
    school_hit = [s for s in parsed.schools if any(s in school for school in doc.schools)]
    # Canonical tokens on both sides. The query says "ms" and LinkedIn says
    # "master of science", and a substring test between those two is a miss --
    # so asking for a master's demoted everyone who holds one.
    held = {tx.normalize_degree(degree) for degree in doc.degrees}
    degree_hit = [d for d in parsed.degrees if tx.normalize_degree(d) in held]
    field_hit = [
        f for f in parsed.fields_of_study if any(f in field for field in doc.fields_of_study)
    ]
    matched = school_hit + degree_hit + field_hit
    required_groups = sum(
        1 for group in (parsed.schools, parsed.degrees, parsed.fields_of_study) if group
    )
    matched_groups = sum(1 for group in (school_hit, degree_hit, field_hit) if group)
    score = matched_groups / required_groups if required_groups else 0.0
    return Criterion(
        name="education",
        label=f"Education: {', '.join(wanted_any)}",
        kind="education",
        weight=WEIGHTS["education"],
        status=_status_for(score),
        score=score,
        evidence=(
            f"Matched {', '.join(matched)}" if matched else "No matching education on file"
        ),
    )


def _score_keywords(doc: CandidateDoc, keywords: list[str]) -> list[Criterion]:
    criteria: list[Criterion] = []
    haystack = f" {doc.haystack} "
    for keyword in keywords:
        present = f" {keyword} " in haystack
        criteria.append(
            Criterion(
                name=f"keyword:{keyword}",
                label=f"Mentions {keyword}",
                kind="keyword",
                weight=WEIGHTS["keyword"],
                status="strong" if present else "miss",
                score=1.0 if present else 0.0,
                evidence=(
                    f"“{keyword}” appears in their profile" if present else f"No mention of {keyword}"
                ),
            )
        )
    return criteria


def _freeform_placeholders(parsed: ParsedQuery) -> list[Criterion]:
    """Qualitative asks the deterministic layer cannot verify — the model fills these in."""
    return [
        Criterion(
            name=f"freeform:{index}",
            label=text,
            kind="freeform",
            weight=WEIGHTS["freeform"],
            status="unknown",
            score=0.5,
            evidence="Needs review — qualitative requirement",
        )
        for index, text in enumerate(parsed.freeform_criteria)
    ]


# --------------------------------------------------------------------------
# aggregate
# --------------------------------------------------------------------------


def assess(doc: CandidateDoc, parsed: ParsedQuery) -> FitAssessment:
    criteria: list[Criterion] = []

    for maybe in (
        _score_title(doc, parsed.job_titles),
        _score_seniority(doc, parsed.seniority),
        _score_experience(doc, parsed),
        _score_location(doc, parsed),
        _score_industry(doc, parsed.industries),
        _score_target_companies(doc, parsed.target_companies),
        _score_company_attribute(
            doc, parsed.company_stages, key="company_stage", kind="company_stage",
            label="Company stage",
        ),
        _score_company_attribute(
            doc, parsed.company_sizes, key="company_size", kind="company_size",
            label="Company size",
        ),
        _score_education(doc, parsed),
    ):
        if maybe is not None:
            criteria.append(maybe)

    criteria.extend(_score_skills(doc, parsed.must_have_skills, required=True))
    criteria.extend(_score_skills(doc, parsed.nice_to_have_skills, required=False))
    criteria.extend(_score_keywords(doc, parsed.keywords))
    criteria.extend(_freeform_placeholders(parsed))

    if not criteria:
        # Nothing structured to check — fall back to retrieval ordering alone.
        return FitAssessment(score=0.5, criteria=[])

    total_weight = sum(c.weight for c in criteria)
    weighted = sum(c.score * c.weight for c in criteria)
    score = weighted / total_weight if total_weight else 0.0

    # Missing a hard requirement should visibly cost the candidate.
    hard_misses = [c for c in criteria if c.kind == "must_skill" and c.status == "miss"]
    if hard_misses:
        score *= max(0.45, 1.0 - 0.18 * len(hard_misses))

    return FitAssessment(score=max(0.0, min(1.0, score)), criteria=criteria)


def requirement_list(parsed: ParsedQuery) -> list[str]:
    """Human-readable requirements handed to the reranker."""
    items: list[str] = []
    if parsed.job_titles:
        items.append(f"Currently or recently works as: {', '.join(parsed.job_titles)}")
    if parsed.seniority:
        items.append(f"Seniority level: {', '.join(parsed.seniority)}")
    if parsed.min_years_experience is not None:
        items.append(f"At least {parsed.min_years_experience:g} years of experience")
    if parsed.max_years_experience is not None:
        items.append(f"At most {parsed.max_years_experience:g} years of experience")
    for skill in parsed.must_have_skills:
        items.append(f"Hands-on experience with {skill}")
    for skill in parsed.nice_to_have_skills:
        items.append(f"Nice to have: {skill}")
    if parsed.locations:
        items.append(f"Based in {', '.join(parsed.locations)}")
    if parsed.industries:
        items.append(f"Industry background in {', '.join(parsed.industries)}")
    if parsed.company_stages:
        items.append(f"Worked at {', '.join(parsed.company_stages)} stage companies")
    if parsed.company_sizes:
        items.append(f"Worked at companies sized {', '.join(parsed.company_sizes)}")
    if parsed.target_companies:
        items.append(f"Worked at {', '.join(parsed.target_companies)}")
    for degree in parsed.degrees:
        items.append(f"Holds a {degree.upper()}")
    for field_of_study in parsed.fields_of_study:
        items.append(f"Studied {field_of_study}")
    items.extend(parsed.freeform_criteria)
    return items
