"""`build_public_content` is the sole allowlist deciding what of a
GenerationCandidate a student ever sees — this asserts private fields
(answer key, explanation, rubric, reference solutions, hidden tests) can
never leak into it, for every item type.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.generation.models import GenerationCandidate, ItemType
from app.domain.generation.public_content import build_public_content

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
SECRET = "SECRET_PRIVATE_MARKER_9f2a"


def _candidate(item_type: ItemType, **overrides) -> GenerationCandidate:
    defaults = dict(
        id="cand-1", request_id="req-1", blueprint_id="bp-1", slot_index=0, family_id="fam-1",
        local_candidate_id="local-1", item_type=item_type, objective_ids=["obj-1"], difficulty="medium",
        marks=5.0, estimated_minutes=5, uniqueness_signature="sig-1", public_prompt="What is 2+2?",
        private_expected_answer=SECRET, private_explanation=SECRET, marking_rubric={"note": SECRET},
        created_at=NOW,
    )
    defaults.update(overrides)
    return GenerationCandidate(**defaults)


def test_single_choice_exposes_options_but_never_private_fields() -> None:
    candidate = _candidate(ItemType.SINGLE_CHOICE, options=[{"key": "A", "text": "4"}, {"key": "B", "text": "5"}])
    content = build_public_content(candidate)
    assert content["options"] == [{"key": "A", "text": "4"}, {"key": "B", "text": "5"}]
    assert SECRET not in str(content)
    assert "private_expected_answer" not in content
    assert "marking_rubric" not in content


def test_coding_exposes_only_language_keys_never_reference_solution_source() -> None:
    candidate = _candidate(
        ItemType.CODING,
        reference_solutions={"python3.11": f"print('{SECRET}')", "java17": f"// {SECRET}"},
        public_tests=[{"group_name": "basic", "weight": 10.0, "tests": [{"stdin": "1 2", "expected_output": "3"}]}],
        hidden_test_groups=[{"group_name": "hidden", "weight": 5.0, "tests": [{"stdin": "9 9", "expected_output": SECRET}]}],
    )
    content = build_public_content(candidate)
    assert content["permitted_languages"] == ["java17", "python3.11"]
    assert content["public_tests"] == candidate.public_tests
    assert "hidden_test_groups" not in content
    assert SECRET not in str(content)


def test_short_answer_never_leaks_rubric_or_expected_answer() -> None:
    candidate = _candidate(ItemType.SHORT_ANSWER)
    content = build_public_content(candidate)
    assert SECRET not in str(content)
    assert set(content.keys()) == {"item_type", "marks", "estimated_minutes", "prompt", "constraints", "examples"}
