"""The title -> level parser. Pure: no DB, no fixtures, no settings.

Table-driven over real-shaped titles, because every trap in foray/seniority.py
is a title someone actually holds. The table is the specification: a change
that "improves" a regex and flips one of these rows has changed what a stored
seniority_rank means for 200k rows, and should have to say so out loud.
"""

from __future__ import annotations

import pytest

from foray.seniority import (
    MGMT_CHIEF,
    MGMT_DIRECTOR,
    MGMT_MANAGER,
    MGMT_VP,
    SENIORITY_DISTINGUISHED,
    SENIORITY_ENTRY,
    SENIORITY_INTERN,
    SENIORITY_MID,
    SENIORITY_SENIOR,
    SENIORITY_STAFF,
    parse_seniority,
    seniority_from_experience,
)

# (title, seniority_rank, mgmt_rank, is_founder)
TITLES: list[tuple[str | None, int | None, int | None, bool]] = [
    # --- the plain IC ladder ---------------------------------------------
    ("Software Engineering Intern", SENIORITY_INTERN, None, False),
    ("Data Science Co-op", SENIORITY_INTERN, None, False),
    ("Junior Frontend Developer", SENIORITY_ENTRY, None, False),
    ("Jr. Data Analyst", SENIORITY_ENTRY, None, False),
    ("Associate Software Engineer", SENIORITY_ENTRY, None, False),
    ("New Grad Software Engineer", SENIORITY_ENTRY, None, False),
    ("Mid-Level Java Developer", SENIORITY_MID, None, False),
    ("Senior Software Engineer", SENIORITY_SENIOR, None, False),
    ("Sr. Backend Engineer", SENIORITY_SENIOR, None, False),
    ("Snr Platform Developer", SENIORITY_SENIOR, None, False),
    ("Staff Software Engineer", SENIORITY_STAFF, None, False),
    ("Principal Engineer", SENIORITY_STAFF, None, False),
    ("Tech Lead", SENIORITY_STAFF, None, False),
    ("Distinguished Engineer", SENIORITY_DISTINGUISHED, None, False),
    ("Technical Fellow", SENIORITY_DISTINGUISHED, None, False),
    # No level word at all is the common case, and must read as UNKNOWN
    # rather than as the bottom of the ladder.
    ("Software Engineer", None, None, False),
    ("Data Scientist", None, None, False),
    # --- most-specific-first ordering ------------------------------------
    # "Senior Staff" is staff, not senior; "Senior Principal" likewise.
    ("Senior Staff Engineer", SENIORITY_STAFF, None, False),
    ("Senior Principal Machine Learning Engineer", SENIORITY_STAFF, None, False),
    # --- seniority is a LEVEL, not a role (the LA Fitness incident) -------
    # Both of these carry a real level word and get a real rank. Keeping them
    # out of an engineering search is the titles/markers facets' job. The
    # counselor is the verbatim distractor from the incident that started this
    # project; if this row ever asserts None, relevance logic has leaked in
    # here and the fix is in the wrong module.
    ("Staff Accountant", SENIORITY_STAFF, None, False),
    ("Senior Personal Training Counselor", SENIORITY_SENIOR, None, False),
    # --- founder -----------------------------------------------------------
    ("Founder", None, None, True),
    ("Co-Founder", None, None, True),
    ("Cofounder", None, None, True),
    ("co founder", None, None, True),
    ("Founding Engineer", None, None, True),
    ("Founding Member of Technical Staff", SENIORITY_STAFF, None, True),
    ("Co-Founder & CEO", None, MGMT_CHIEF, True),
    # "founding" ALONE is not enough. Both of these are real LinkedIn titles
    # and neither is a startup founder.
    ("Founding member of the choir", None, None, False),
    ("Founding Board Member", None, None, False),
    # --- management --------------------------------------------------------
    ("Engineering Manager", None, MGMT_MANAGER, False),
    ("General Manager", None, MGMT_MANAGER, False),
    ("Director of Engineering", None, MGMT_DIRECTOR, False),
    ("Head of Data Platform", None, MGMT_DIRECTOR, False),
    ("VP of Engineering", None, MGMT_VP, False),
    ("SVP, Global Sales", None, MGMT_VP, False),
    ("Chief Technology Officer", None, MGMT_CHIEF, False),
    ("CTO", None, MGMT_CHIEF, False),
    ("CISO", None, MGMT_CHIEF, False),
    # --- hybrids: BOTH ordinals are recorded ------------------------------
    # mgmt_rank carries the management reading; the IC rank is kept because
    # the title genuinely states a level. A pure-IC consumer adds
    # `mgmt_rank is null`.
    ("Senior Engineering Manager", SENIORITY_SENIOR, MGMT_MANAGER, False),
    ("Senior Director, Platform", SENIORITY_SENIOR, MGMT_DIRECTOR, False),
    # Verbatim from the real export: banking's "Vice President" is an IC rank,
    # which this module cannot know. Accepted collision, asserted so it is a
    # decision rather than a surprise.
    ("Vice President, Senior Business Analyst", SENIORITY_SENIOR, MGMT_VP, False),
    # --- the manager traps -------------------------------------------------
    # Nobody reports to a Product/Program/Project Manager. Calling them
    # management would hand `is_manager` the entire PM population.
    ("Product Manager", None, None, False),
    ("Senior Product Manager", SENIORITY_SENIOR, None, False),
    ("Technical Program Manager", None, None, False),
    ("Project Manager", None, None, False),
    # ...but one real management occurrence is enough, even alongside a PM one.
    ("Product Manager and Engineering Manager", None, MGMT_MANAGER, False),
    # --- other accepted collisions, asserted -------------------------------
    # "Principal" in consulting means partner-level management; resolved in
    # favour of the IC reading, deliberately.
    ("Principal, Deloitte Consulting", SENIORITY_STAFF, None, False),
    # "Chief of Staff" is an IC attached to an exec, not the exec. It must
    # claim neither MGMT_CHIEF nor SENIORITY_STAFF off the word "staff".
    ("Chief of Staff", None, None, False),
    # Academic and medical fellowships are early-career, not rank 6.
    ("Research Fellow", None, None, False),
    ("Pediatric and Adolescent Gynecology Fellow", None, None, False),
    # "lead" as a sales noun, and a self-description, are not rungs.
    ("Lead Generation Specialist", None, None, False),
    ("Thought Leader", None, None, False),
    # Word boundaries: "sr" inside "user", "intern" inside "international",
    # "vp" inside "vpn". These fired before the _re() grouping fix.
    ("User Researcher", None, None, False),
    ("International Sales Representative", None, None, False),
    ("VPN Support Technician", None, None, False),
    # --- numeric / roman level suffixes ------------------------------------
    ("Analyst I", SENIORITY_ENTRY, None, False),
    ("Software Engineer II", SENIORITY_MID, None, False),
    ("Engineer III", SENIORITY_SENIOR, None, False),
    ("Software Engineer 2", SENIORITY_MID, None, False),
    ("Engineer, Level 5", SENIORITY_STAFF, None, False),
    # A ladder WORD outranks a numeral: this stays senior, not mid.
    ("Senior Engineer II", SENIORITY_SENIOR, None, False),
    # Mid-string numerals describe the queue, not the person.
    ("Tier 1 Support Specialist", None, None, False),
    # --- degenerate input --------------------------------------------------
    (None, None, None, False),
    ("", None, None, False),
    ("   ", None, None, False),
    ("--", None, None, False),
]


@pytest.mark.parametrize("title,rank,mgmt,founder", TITLES)
def test_parse_seniority(title, rank, mgmt, founder):
    assert parse_seniority(title) == (rank, mgmt, founder)


# ------------------------------------------------------- the GitHub proxy --


@pytest.mark.parametrize(
    "years,rank",
    [
        (None, None),  # unknown in, unknown out -- never a rank
        (0.0, SENIORITY_ENTRY),  # a brand-new account is not a mid-level eng
        (1.99, SENIORITY_ENTRY),
        (2.0, SENIORITY_MID),  # band edges are closed on the left
        (4.99, SENIORITY_MID),
        (5.0, SENIORITY_SENIOR),
        (7.99, SENIORITY_SENIOR),
        (8.0, SENIORITY_STAFF),
        (25.0, SENIORITY_STAFF),  # clamped: no proxy ever claims rank 6
    ],
)
def test_seniority_from_experience(years, rank):
    assert seniority_from_experience(years) == rank
