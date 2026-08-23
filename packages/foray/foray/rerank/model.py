"""Local cross-encoder load. Importing this module never drags torch in.

sentence_transformers is imported inside the loader so API cold start and
CI (which installs foray with --no-deps and no torch) stay cheap.
"""

from __future__ import annotations

from functools import lru_cache

from ..config import settings

RERANK_MODEL_VERSION = "ms-marco-MiniLM-L-6-v2"


@lru_cache(maxsize=1)
def get_cross_encoder():
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(settings.rerank_model, max_length=settings.rerank_max_length)
    try:
        import torch

        torch.set_num_threads(max(1, settings.rerank_torch_threads))
    except Exception:  # noqa: BLE001 - thread cap is best-effort
        pass
    return model


def warm_cross_encoder() -> None:
    """Load the weights AND run one forward pass.

    ``get_cross_encoder()`` alone is not a warmup. It constructs the model, but
    torch defers a great deal to the first ``predict`` — kernel selection,
    graph/allocator setup, tokenizer compilation — and that cost lands on
    whoever calls first. Measured on prod with warmup as it was (weights only):
    the first search spent 1287 ms in rank, blew the 1200 ms budget, logged
    ``rerank.timeout`` and returned unreranked; its work kept the single
    threadpool worker busy, so the NEXT search got ``queue_full``. Two
    degraded searches per deploy. Once warm the same query spends ~85 ms and
    demotes 11-43 rows, so the model is worth having — it just must not meet
    its first pair on the request path.

    The pair is a throwaway; only the side effect matters. Never raise: the
    caller treats warmup as best-effort and a failed warmup must not stop the
    API from serving (rerank itself degrades to unreranked).
    """
    model = get_cross_encoder()
    model.predict(
        [("warmup query", "warmup document")],
        batch_size=1,
        show_progress_bar=False,
        convert_to_numpy=True,
    )


def predict_logits(pairs: list[tuple[str, str]]) -> list[float]:
    """Return raw logits. The sigmoid lives in rank.py where it is testable."""
    if not pairs:
        return []
    model = get_cross_encoder()
    scores = model.predict(
        pairs,
        batch_size=settings.rerank_batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [float(s) for s in scores]
