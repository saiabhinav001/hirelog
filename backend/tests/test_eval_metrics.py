"""Unit tests for the search-evaluation metrics (deterministic, no I/O)."""

from __future__ import annotations

import math

import pytest

from eval.metrics import mean, ndcg_at_k, recall_at_k, reciprocal_rank


def test_recall_at_k_partial_and_full():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, ["a", "c"], k=4) == 1.0
    assert recall_at_k(ranked, ["a", "z"], k=4) == 0.5  # only 1 of 2 relevant present
    assert recall_at_k(ranked, ["c"], k=2) == 0.0        # c is outside top-2
    assert recall_at_k(ranked, [], k=4) == 0.0           # no relevant → 0


def test_reciprocal_rank_positions():
    assert reciprocal_rank(["a", "b", "c"], ["a"]) == 1.0
    assert reciprocal_rank(["a", "b", "c"], ["b"]) == pytest.approx(0.5)
    assert reciprocal_rank(["a", "b", "c"], ["c"]) == pytest.approx(1 / 3)
    assert reciprocal_rank(["a", "b", "c"], ["z"]) == 0.0


def test_dcg_and_ndcg_ideal_ranking_is_one():
    ranked = ["a", "b", "c"]
    # Relevant docs already at the top → nDCG must be 1.0
    assert ndcg_at_k(ranked, ["a", "b"], k=3) == pytest.approx(1.0)


def test_ndcg_penalizes_lower_ranking():
    top = ndcg_at_k(["a", "x", "y"], ["a"], k=3)
    lower = ndcg_at_k(["x", "y", "a"], ["a"], k=3)
    assert top == pytest.approx(1.0)
    assert lower < top
    # exact value: 1/log2(3+1) normalized by ideal 1.0
    assert lower == pytest.approx(1.0 / math.log2(4))


def test_ndcg_no_relevant_is_zero():
    assert ndcg_at_k(["a", "b"], [], k=2) == 0.0


def test_mean_handles_empty():
    assert mean([]) == 0.0
    assert mean([1.0, 0.0]) == pytest.approx(0.5)
