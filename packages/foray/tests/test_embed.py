from __future__ import annotations

import pytest

from foray.embed.blob import build_blob


def _blob(**kw):
    base = dict(
        name="Ada",
        login="ada",
        bio="ML engineer",
        primary_language="python",
        language_mix={"python": 0.9, "cuda": 0.1},
        employer="Stripe",
        title=None,
        top_repos=[{"name": "fast-transformer", "description": "pytorch", "stars": 2100, "language": "Python"}],
        topics=["pytorch", "transformers"],
    )
    base.update(kw)
    return build_blob(**base)


def test_blob_leads_with_name_and_bio():
    blob = _blob()
    assert blob.splitlines()[0] == "Ada. ML engineer"
    assert "Primary: python. Also: cuda." in blob
    assert "Stripe" in blob
    assert "Notable work: fast-transformer" in blob
    assert "Topics: pytorch, transformers" in blob


def test_blob_is_stable_for_diffing():
    assert _blob() == _blob()


def test_blob_truncates():
    blob = _blob(bio="x" * 5000, max_chars=200)
    assert len(blob) == 200


def test_blob_handles_missing_fields():
    blob = build_blob(
        name=None,
        login="ghost",
        bio=None,
        primary_language=None,
        language_mix=None,
        employer=None,
        title=None,
        top_repos=None,
        topics=None,
    )
    assert blob == "ghost"


@pytest.mark.model
def test_embedding_similarity_similar_beats_dissimilar():
    # Uses the real model (downloaded once, cached). Similar profiles must be
    # closer in cosine space than dissimilar ones. CI skips this (no HF fetch);
    # the foray Docker image bakes the model and is built on every push.
    import numpy as np

    pytest.importorskip("sentence_transformers")

    from foray.embed.model import embed_documents

    ml1 = "ML engineer. Primary: python. Notable work: fast-transformer — pytorch training (2100 stars)"
    ml2 = "Machine learning researcher. Primary: python. Notable work: llm-trainer — transformers (900 stars)"
    fe = "Frontend engineer. Primary: typescript. Notable work: cool-ui — react component library (40 stars)"

    v_ml1, v_ml2, v_fe = embed_documents([ml1, ml2, fe])
    sim_similar = float(np.dot(v_ml1, v_ml2))
    sim_dissimilar = float(np.dot(v_ml1, v_fe))
    assert sim_similar > sim_dissimilar
