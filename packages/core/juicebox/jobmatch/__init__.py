"""Candidate ↔ job-posting matching.

Scoring reuses ``search.scoring.assess`` against a posting compiled into a
``ParsedQuery``. Retrieval (``SearchEngine.search``) is deliberately not used:
hard filters would drop people the scorer should only rank down, and the two
directions would not agree.
"""
