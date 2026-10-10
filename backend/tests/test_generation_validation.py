"""Structured validation tests — the exact scenarios from the Prompt 5 test
list: malformed provider JSON, nonexistent citations, incorrect
objective/difficulty/marks, duplicate variants, and structural coding checks.
A provider's own `status`/`approved` claims are always ignored (see module
docstring in validation.py) — this is verified directly here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.generation.models import CandidateStatus, GenerationRequest, ItemType
from app.domain.generation.validation import validate_candidate_dict

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _request(**overrides) -> GenerationRequest:
    defaults = dict(
        id="req-1",
        blueprint_id="bp-1",
        slot_index=0,
        objective_ids=["obj-1"],
        item_type=ItemType.SINGLE_CHOICE,
        difficulty="medium",
        marks=5.0,
        target_minutes=5,
        requested_variant_count=2,
        permitted_languages=[],
        approved_source_bundle=[
            {"source_id": "src-1", "source_version": 1, "anchor_id": "anchor-1", "excerpt": "excerpt text"}
        ],
        deterministic_seed=42,
        created_by="faculty-1",
        created_at=NOW,
    )
    defaults.update(overrides)
    return GenerationRequest(**defaults)


def _valid_raw_candidate(**overrides) -> dict:
    raw = {
        "family_id": "fam-1",
        "local_candidate_id": "cand-1",
        "item_type": "single_choice",
        "objective_ids": ["obj-1"],
        "difficulty": "medium",
        "marks": 5.0,
        "estimated_minutes": 5,
        "uniqueness_signature": "sig-1",
        "public_prompt": "What is 2+2?",
        "options": [{"key": "A", "text": "3"}, {"key": "B", "text": "4"}, {"key": "C", "text": "5"}, {"key": "D", "text": "6"}],
        "private_expected_answer": "B",
        "supporting_anchors": [{"source_id": "src-1", "source_version": 1, "anchor_id": "anchor-1"}],
    }
    raw.update(overrides)
    return raw


def test_valid_candidate_is_ready_for_human_review() -> None:
    candidate, issues = validate_candidate_dict(
        _valid_raw_candidate(), request=_request(), now=NOW,
        previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate is not None
    assert candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW
    assert issues == []


def test_malformed_provider_json_returns_none_with_blocker() -> None:
    """Missing a required key entirely — the provider sent garbage."""
    raw = _valid_raw_candidate()
    del raw["family_id"]
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate is None
    assert issues[0].severity == "blocker"
    assert issues[0].field == "schema"


def test_nonexistent_citation_is_blocked() -> None:
    raw = _valid_raw_candidate(
        supporting_anchors=[{"source_id": "src-1", "source_version": 1, "anchor_id": "made-up-anchor"}]
    )
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any("does not exist" in i.reason for i in issues)


def test_no_supporting_anchors_is_blocked() -> None:
    raw = _valid_raw_candidate(supporting_anchors=[])
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any(i.field == "supporting_anchors" for i in issues)


def test_duplicate_signature_against_history_is_blocked() -> None:
    candidate, issues = validate_candidate_dict(
        _valid_raw_candidate(uniqueness_signature="sig-old"), request=_request(), now=NOW,
        previously_used_signatures={"sig-old"}, signatures_seen_this_batch=set(),
    )
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any("duplicates a previously used" in i.reason for i in issues)


def test_duplicate_signature_within_batch_is_blocked() -> None:
    candidate, issues = validate_candidate_dict(
        _valid_raw_candidate(uniqueness_signature="sig-batch"), request=_request(), now=NOW,
        previously_used_signatures=set(), signatures_seen_this_batch={"sig-batch"},
    )
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any("duplicates another candidate" in i.reason for i in issues)


def test_objective_mismatch_is_flagged() -> None:
    raw = _valid_raw_candidate(objective_ids=["wrong-objective"])
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert any(i.field == "objective_ids" for i in issues)


def test_marks_mismatch_is_flagged() -> None:
    raw = _valid_raw_candidate(marks=999.0)
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert any(i.field == "marks" for i in issues)


def test_difficulty_mismatch_is_flagged() -> None:
    raw = _valid_raw_candidate(difficulty="impossible")
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert any(i.field == "difficulty" for i in issues)


def test_coding_item_missing_reference_solution_is_blocked() -> None:
    request = _request(item_type=ItemType.CODING, permitted_languages=["python", "java"])
    raw = _valid_raw_candidate(
        item_type="coding", options=[],
        supporting_anchors=[{"source_id": "src-1", "source_version": 1, "anchor_id": "anchor-1"}],
        reference_solutions={"python": "def solve(): pass"},  # missing java
        public_tests=[{"input": "1", "output": "1"}],
    )
    candidate, issues = validate_candidate_dict(
        raw, request=request, now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any("java" in i.reason for i in issues)


def test_single_choice_needs_at_least_two_options() -> None:
    raw = _valid_raw_candidate(options=["only-one"])
    candidate, issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any(i.field == "options" for i in issues)


def test_provider_cannot_self_approve() -> None:
    """Even if a compromised/malfunctioning provider includes an `approved`
    or `status: approved` field, the schema has no slot for it and the
    status is always recomputed here — never trusted from the provider."""
    raw = _valid_raw_candidate()
    raw["status"] = "approved"  # attempted self-approval
    raw["approved"] = True  # extra field a compromised provider might add
    candidate, _issues = validate_candidate_dict(
        raw, request=_request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )
    assert candidate.status in (CandidateStatus.READY_FOR_HUMAN_REVIEW, CandidateStatus.NEEDS_REVISION)
    assert candidate.status != CandidateStatus.APPROVED


# -- Backend prompt A: provider-reported notes, family constraints, item-type contract --------------

def _validate(raw: dict, request: GenerationRequest | None = None):
    return validate_candidate_dict(
        raw, request=request or _request(), now=NOW, previously_used_signatures=set(), signatures_seen_this_batch=set(),
    )


def test_provider_notes_are_kept_for_the_reviewer_and_surface_as_minor_issues() -> None:
    candidate, issues = _validate(_valid_raw_candidate(
        variant_parameters={"a": 2, "b": 2}, assumptions=["Decimal arithmetic"], validation_concerns=["Distractor C is close to B"],
    ))
    assert candidate.variant_parameters == {"a": 2, "b": 2}
    assert candidate.assumptions == ["Decimal arithmetic"]
    assert candidate.provider_concerns == ["Distractor C is close to B"]
    assert [(i.severity, i.field) for i in issues] == [("minor", "provider_concern")]
    assert candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW  # informational: never blocks


def test_provider_notes_are_bounded_and_ignore_wrong_types() -> None:
    candidate, _ = _validate(_valid_raw_candidate(
        assumptions="not a list", validation_concerns=["x" * 900] + [f"c{i}" for i in range(50)], variant_parameters=["nope"],
    ))
    assert candidate.assumptions == [] and candidate.variant_parameters == {}
    assert len(candidate.provider_concerns) == 20
    assert all(len(c) <= 500 for c in candidate.provider_concerns)


def test_forbidden_topic_in_prompt_or_options_is_a_major_issue() -> None:
    from app.domain.generation.models import FamilyConstraints

    request = _request(family_constraints=FamilyConstraints(forbidden_topics=["Recursion"]))
    candidate, issues = _validate(_valid_raw_candidate(public_prompt="Explain how recursion works in this loop."), request)
    assert any(i.severity == "major" and "Recursion" in i.reason for i in issues)
    candidate, issues = _validate(_valid_raw_candidate(
        options=[{"key": "A", "text": "3"}, {"key": "B", "text": "Use recursive calls"}, {"key": "C", "text": "RECURSION"}],
    ), request)
    assert any("Recursion" in i.reason for i in issues)
    clean, clean_issues = _validate(_valid_raw_candidate(), request)
    assert clean_issues == []


def test_multiple_select_key_is_normalised_and_all_options_correct_is_flagged() -> None:
    request = _request(item_type=ItemType.MULTIPLE_SELECT)
    candidate, issues = _validate(_valid_raw_candidate(item_type="multiple_select", private_expected_answer="C, A"), request)
    assert candidate.private_expected_answer == "A,C"
    assert issues == []
    _, issues = _validate(_valid_raw_candidate(item_type="multiple_select", private_expected_answer="A,B,C,D"), request)
    assert any(i.severity == "major" and "every option" in i.reason for i in issues)


def test_single_choice_with_two_correct_options_is_blocked() -> None:
    candidate, issues = _validate(_valid_raw_candidate(private_expected_answer="A,B"))
    assert candidate.status == CandidateStatus.NEEDS_REVISION
    assert any(i.severity == "blocker" and "exactly one" in i.reason for i in issues)


def test_short_answer_rubric_must_total_the_marks() -> None:
    request = _request(item_type=ItemType.SHORT_ANSWER, marks=4.0)
    good = {"criteria": [{"point": "definition", "points": 2, "acceptable_alternatives": ["meaning"]}, {"point": "example", "points": 2}]}
    candidate, issues = _validate(_valid_raw_candidate(item_type="short_answer", options=[], marks=4.0, marking_rubric=good), request)
    assert issues == [] and candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW
    bad = {"criteria": [{"point": "definition", "points": 1}]}
    _, issues = _validate(_valid_raw_candidate(item_type="short_answer", options=[], marks=4.0, marking_rubric=bad), request)
    assert any(i.field == "marking_rubric" for i in issues)


def test_coding_item_without_the_io_contract_gets_a_minor_note_only() -> None:
    request = _request(item_type=ItemType.CODING, permitted_languages=["python"])
    tests = [{"group_name": "basic", "weight": 1.0, "tests": [{"stdin": "1", "expected_output": "1"}]}]
    raw = _valid_raw_candidate(
        item_type="coding", options=[], reference_solutions={"python": "print(input())"},
        public_tests=tests, hidden_test_groups=tests,
    )
    candidate, issues = _validate(raw, request)
    assert [(i.severity, i.field) for i in issues] == [("minor", "constraints")]
    assert candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW

    full = dict(raw, constraints={"input_output_spec": "one int", "complexity": "O(1)"})
    candidate, issues = _validate(full, request)
    assert issues == []


def test_provider_notes_and_hidden_tests_never_reach_the_student_content() -> None:
    from app.domain.generation.public_content import build_public_content

    request = _request(item_type=ItemType.CODING, permitted_languages=["python"])
    tests = [{"group_name": "boundary", "weight": 1.0, "tests": [{"stdin": "SECRET-IN", "expected_output": "SECRET-OUT"}]}]
    candidate, _ = _validate(_valid_raw_candidate(
        item_type="coding", options=[], reference_solutions={"python": "print('REFERENCE')"}, public_tests=[],
        hidden_test_groups=tests, assumptions=["ASSUMPTION"], validation_concerns=["CONCERN"],
        variant_parameters={"k": "PARAM"}, private_explanation="EXPLAIN",
    ), request)
    rendered = str(build_public_content(candidate))
    for secret in ("SECRET-IN", "SECRET-OUT", "REFERENCE", "ASSUMPTION", "CONCERN", "PARAM", "EXPLAIN"):
        assert secret not in rendered, secret


def test_sql_items_are_not_accepted_until_a_sql_type_exists() -> None:
    candidate, issues = _validate(_valid_raw_candidate(item_type="sql"))
    assert candidate is None
    assert issues[0].severity == "blocker" and issues[0].field == "schema"
