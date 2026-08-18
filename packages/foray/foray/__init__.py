"""Foray sourcing engine.

A candidate search engine over US software engineers, built from public GitHub
data, with precomputed archetype fit scores so query latency is sub-second.

The critical property: nothing in the query path calls an external API. All
expensive work happens offline (harvest -> enrich -> embed -> score), and the
query path is an indexed SQL filter + ORDER BY over precomputed scores.
"""

__version__ = "0.1.0"
