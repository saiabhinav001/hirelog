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
