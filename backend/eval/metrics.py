"""Ranking-quality metrics for the search evaluation harness.

Pure functions over ranked id lists and relevance judgments — no I/O — so they
are deterministic and unit-testable. Relevance is treated as binary (a doc id is
relevant or not), which matches how the golden set is labelled.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    """Fraction of relevant documents retrieved within the top ``k``."""
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    top_k = set(ranked_ids[:k])
    return len(top_k & relevant) / len(relevant)


def reciprocal_rank(ranked_ids: Sequence[str], relevant_ids: Iterable[str]) -> float:
    """1 / rank of the first relevant document (0 if none retrieved)."""
    relevant = set(relevant_ids)
    for index, doc_id in enumerate(ranked_ids, start=1):
        if doc_id in relevant:
            return 1.0 / index
    return 0.0


def dcg_at_k(ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    relevant = set(relevant_ids)
    dcg = 0.0
    for index, doc_id in enumerate(ranked_ids[:k], start=1):
        if doc_id in relevant:
            dcg += 1.0 / math.log2(index + 1)
    return dcg


def ndcg_at_k(ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    """Normalized DCG@k — DCG divided by the ideal DCG for this query."""
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg_at_k(ranked_ids, relevant, k) / idcg


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0
