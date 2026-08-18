"""BM25-Okapi lexical scoring over an in-process inverted index.

Vectorised with numpy so a query over tens of thousands of profiles stays in the
low milliseconds. Swap this for Postgres FTS or OpenSearch by reimplementing
``score`` — nothing else in the engine touches the index internals.
"""

from __future__ import annotations

import math

import numpy as np


class BM25Index:
    K1 = 1.5
    B = 0.75

    def __init__(self, documents: list[list[str]]) -> None:
        self.doc_count = len(documents)
        self.doc_lengths = np.array([len(d) for d in documents], dtype=np.float32)
        self.avg_doc_length = float(self.doc_lengths.mean()) if self.doc_count else 0.0

        # term -> (doc indexes, term frequencies) as parallel arrays
        postings: dict[str, dict[int, int]] = {}
        for doc_index, tokens in enumerate(documents):
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, count in counts.items():
                postings.setdefault(token, {})[doc_index] = count

        self._postings: dict[str, tuple[np.ndarray, np.ndarray]] = {
            term: (
                np.fromiter(docs.keys(), dtype=np.int32, count=len(docs)),
                np.fromiter(docs.values(), dtype=np.float32, count=len(docs)),
            )
            for term, docs in postings.items()
        }
        self._idf: dict[str, float] = {}
        for term, (doc_ids, _freqs) in self._postings.items():
            df = len(doc_ids)
            # BM25+ style idf floor keeps very common terms from going negative.
            self._idf[term] = max(
                math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5)), 1e-6
            )

    @property
    def vocabulary_size(self) -> int:
        return len(self._postings)

    def score(self, query_tokens: list[str]) -> np.ndarray:
        """Return a BM25 score per document (unnormalised, >= 0)."""
        scores = np.zeros(self.doc_count, dtype=np.float32)
        if self.doc_count == 0 or not query_tokens or self.avg_doc_length == 0:
            return scores

        length_norm = self.K1 * (
            1 - self.B + self.B * (self.doc_lengths / self.avg_doc_length)
        )
        seen: set[str] = set()
        for term in query_tokens:
            if term in seen:
                continue
            seen.add(term)
            posting = self._postings.get(term)
            if posting is None:
                continue
            doc_ids, freqs = posting
            numerator = freqs * (self.K1 + 1.0)
            denominator = freqs + length_norm[doc_ids]
            scores[doc_ids] += self._idf[term] * (numerator / denominator)
        return scores
