"""Voice intake: what a call must collect, and how a transcript becomes data.

``schema`` owns the field set and the gate that decides when a call may end.
``extract`` turns a transcript into a preference record. ``phones`` turns caller
ID into something safe to look a person up by.

The output is deliberately shaped like the job side: the preference ``spec`` uses
``ParsedQuery``'s vocabulary, normalised through ``enrich.taxonomy``, so a
candidate's stated wants and a posting's stated requirements can be scored
against each other instead of merely sitting next to each other.
"""

from .extract import (
    EXTRACTOR_HEURISTIC,
    EXTRACTOR_LLM,
    ExtractionResult,
    diff_records,
    extract_intake,
    known_context,
    normalize_record,
)
from .phones import lookup_key, lookup_keys
from .schema import (
    ENRICHMENT_KEYS,
    FIELDS,
    FIELDS_BY_KEY,
    REQUIRED_KEYS,
    SCHEMA_VERSION,
    SPEC_FIELDS,
    Coverage,
    IntakeField,
    coverage,
    empty_record,
    next_questions,
)

__all__ = [
    "ENRICHMENT_KEYS",
    "EXTRACTOR_HEURISTIC",
    "EXTRACTOR_LLM",
    "FIELDS",
    "FIELDS_BY_KEY",
    "REQUIRED_KEYS",
    "SCHEMA_VERSION",
    "SPEC_FIELDS",
    "Coverage",
    "ExtractionResult",
    "IntakeField",
    "coverage",
    "diff_records",
    "empty_record",
    "extract_intake",
    "known_context",
    "lookup_key",
    "lookup_keys",
    "next_questions",
    "normalize_record",
]
