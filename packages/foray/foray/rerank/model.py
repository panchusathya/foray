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
