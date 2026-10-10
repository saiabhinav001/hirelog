"""Prompt 10: exact scoring semantics for choice questions (no partial credit, exact set)."""

from __future__ import annotations

import pytest

from app.domain.judge.grading import grade_mcq_exact_match


@pytest.mark.parametrize("selected, key, correct", [
    ("A", "A", True),
    (" A ", "A", True),
    ("B", "A", False),
    ("A,B", "A", False),               # single choice: two selections are never right
    (["A"], "A", True),
    (["A", "C"], "A,C", True),
    (["C", "A"], "A,C", True),         # order is irrelevant
    (["A", "A", "C"], "A,C", True),    # a repeated option is the same selection
    ("A,C", "C, A", True),             # comma form, whitespace tolerated
    (["A"], "A,C", False),             # incomplete set: wrong, no partial credit
    (["A", "C", "B"], "A,C", False),   # extra option: wrong
    (["a", "c"], "A,C", False),        # keys are case-sensitive
    ([], "A,C", False),
    ("", "A", False),
    (None, "A", False),
    (["A"], "", False),                # no answer key: never correct
    (["A"], None, False),
    ([1, "A"], "A", False),            # non-string members are not a valid answer
    ({"selected": "A"}, "A", False),
    (42, "A", False),
])
def test_choice_answers_are_graded_as_an_exact_set(selected, key, correct) -> None:
    assert grade_mcq_exact_match(selected, key) is correct
