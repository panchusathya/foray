"""Test configuration. DB-backed tests skip themselves when Postgres is not
reachable (see ``tests.dbutil``), so offline unit tests always run.

Search tests inject the hashing embedder so CI (no torch) can exercise
``run_search`` without loading sentence-transformers.
"""

from __future__ import annotations

import pytest

from foray.embed.hashing import hashing_embed


@pytest.fixture(autouse=True)
def _hashing_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    import foray.api.search as search_module

    monkeypatch.setattr(search_module, "embed_query", hashing_embed)
