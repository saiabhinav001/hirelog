"""Prompt 3 (ADR 0019): invalid-policy validation, blueprint negative-marks
validation and the pure publication preview. No Firestore involved."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain.exams.policy import MAX_ACCOMMODATIONS, AccommodationWindow, ExamPolicy
from app.domain.exams.publication import build_preview, group_approved_by_slot, snapshot_slots
from app.domain.generation.models import (
    BlueprintSlot,
    CandidateStatus,
    ExamBlueprint,
    GenerationCandidate,
    ItemType,
    SourceAnchorRef,
)

NOW = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


# -- policy: defaults must reproduce pre-Prompt-3 behaviour -----------------------

def test_a_legacy_policy_payload_still_validates_with_neutral_defaults() -> None:
    policy = ExamPolicy.model_validate({"duration_minutes": 90, "warning_threshold": 3, "accommodations": {}})
    assert policy.timezone is None
    assert policy.scheduled_start is None and policy.scheduled_end is None
    assert policy.variant_policy == "unique"


@pytest.mark.parametrize("duration", [0, -5, 24 * 60 + 1])
def test_duration_out_of_range_is_rejected(duration: int) -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=duration)


@pytest.mark.parametrize("field,value", [
    ("warning_threshold", 0), ("warning_threshold", 51), ("late_admission_grace_seconds", -1),
    ("late_admission_grace_seconds", 3601),
])
def test_threshold_and_grace_bounds_are_enforced(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, **{field: value})


@pytest.mark.parametrize("name", ["Not/AZone", "", "  ", "UTC+5", "../etc/passwd", "x" * 65])
def test_unknown_or_malformed_timezones_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, timezone=name)


def test_a_real_iana_timezone_is_accepted_and_trimmed() -> None:
    assert ExamPolicy(duration_minutes=60, timezone=" Asia/Kolkata ").timezone == "Asia/Kolkata"


def test_naive_schedule_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        ExamPolicy(duration_minutes=60, scheduled_start=datetime(2026, 6, 1, 9, 0))


def test_schedule_timestamps_are_normalised_to_utc() -> None:
    from datetime import timezone as tz

    ist = tz(timedelta(hours=5, minutes=30))
    policy = ExamPolicy(duration_minutes=60, scheduled_start=datetime(2026, 6, 1, 14, 30, tzinfo=ist))
    assert policy.scheduled_start == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert policy.scheduled_start.utcoffset() == timedelta(0)


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-1)])
def test_schedule_end_must_be_after_start(delta: timedelta) -> None:
    with pytest.raises(ValidationError, match="after scheduled_start"):
        ExamPolicy(duration_minutes=60, scheduled_start=NOW, scheduled_end=NOW + delta)


def test_unknown_variant_policy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, variant_policy="reuse_everything")


def test_unknown_policy_fields_are_still_forbidden() -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, negative_marking=0.25)


def test_accommodation_count_is_capped() -> None:
    too_many = {f"u{i}": AccommodationWindow(extra_minutes=1) for i in range(MAX_ACCOMMODATIONS + 1)}
    with pytest.raises(ValidationError, match="at most"):
        ExamPolicy(duration_minutes=60, accommodations=too_many)


def test_accommodation_extra_minutes_are_bounded() -> None:
    with pytest.raises(ValidationError):
        AccommodationWindow(extra_minutes=24 * 60 + 1)
    with pytest.raises(ValidationError):
        AccommodationWindow(extra_minutes=-1)


def test_policy_round_trips_through_json_without_changing_the_schedule() -> None:
    policy = ExamPolicy(duration_minutes=60, timezone="Asia/Kolkata", scheduled_start=NOW,
                        scheduled_end=NOW + timedelta(hours=2), variant_policy="balanced_reuse")
    again = ExamPolicy.model_validate(policy.model_dump(mode="json"))
    assert again == policy


# -- blueprint negative marks ----------------------------------------------------

def _slot(index: int = 0, **overrides) -> BlueprintSlot:
    data = dict(slot_index=index, item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy",
                marks=2.0, target_minutes=5, permitted_languages=[], requested_variant_count=2)
    data.update(overrides)
    return BlueprintSlot(**data)


def test_negative_marks_default_to_zero_and_cannot_exceed_marks() -> None:
    assert _slot().negative_marks == 0
    assert _slot(negative_marks=0.5).negative_marks == 0.5
    with pytest.raises(ValidationError, match="cannot exceed"):
        _slot(marks=1.0, negative_marks=1.5)
    with pytest.raises(ValidationError):
        _slot(negative_marks=-0.25)


# -- publication preview ---------------------------------------------------------

def _blueprint(slots: list[BlueprintSlot]) -> ExamBlueprint:
    return ExamBlueprint(id="bp1", exam_definition_id="def-1", title="Midterm", slots=slots,
                         created_by="t1", created_at=NOW, updated_at=NOW)


def _candidate(cid: str, slot_index: int, status=CandidateStatus.APPROVED, anchors=()) -> GenerationCandidate:
    return GenerationCandidate(
        id=cid, request_id="r1", blueprint_id="bp1", slot_index=slot_index, family_id="f", local_candidate_id=cid,
        item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy", marks=2.0, estimated_minutes=5,
        uniqueness_signature=cid, public_prompt="q", status=status, created_at=NOW,
        supporting_anchors=[SourceAnchorRef(source_id=s, source_version=1, anchor_id=a) for s, a in anchors],
    )


def _approved(*pairs: tuple[str, int], **kw) -> dict[int, list[GenerationCandidate]]:
    return group_approved_by_slot([_candidate(cid, idx, **kw) for cid, idx in pairs])


def test_preview_resolves_counts_marks_and_distribution() -> None:
    blueprint = _blueprint([
        _slot(0, marks=2, difficulty="easy"),
        _slot(1, marks=3, difficulty="hard", item_type=ItemType.CODING, permitted_languages=["python3.11"],
              target_minutes=20, requested_variant_count=1),
        _slot(2, marks=1, difficulty="easy"),
    ])
    approved = _approved(("a", 0), ("b", 0), ("c", 1), ("d", 2), ("e", 2))
    preview = build_preview(blueprint, approved, duration_minutes=60)

    assert preview.publishable
    assert preview.slot_count == 3
    assert preview.total_marks == 6.0
    assert preview.counts_by_type == {"coding": 1, "single_choice": 2}
    assert preview.marks_by_type == {"coding": 3.0, "single_choice": 3.0}
    assert preview.difficulty_distribution == {"easy": 2, "hard": 1}
    assert preview.total_target_minutes == 30
    assert preview.languages == ["python3.11"]
    assert preview.warnings == []
    assert [s.approved_count for s in preview.slots] == [2, 1, 2]
    assert preview.to_dict()["publishable"] is True


def test_a_slot_without_an_approved_candidate_blocks_publication() -> None:
    preview = build_preview(_blueprint([_slot(0), _slot(1)]), _approved(("a", 0)))
    assert not preview.publishable
    assert any("slot 1" in b and "no approved candidate" in b for b in preview.blockers)


def test_non_approved_candidates_do_not_count() -> None:
    preview = build_preview(
        _blueprint([_slot(0)]), _approved(("a", 0), status=CandidateStatus.READY_FOR_HUMAN_REVIEW)
    )
    assert not preview.publishable


def test_a_short_pool_is_a_warning_not_a_blocker() -> None:
    preview = build_preview(_blueprint([_slot(0, requested_variant_count=5)]), _approved(("a", 0)))
    assert preview.publishable
    assert any("1 approved of 5" in w for w in preview.warnings)


def test_gaps_and_duplicates_in_slot_indexes_block_publication() -> None:
    gap = build_preview(_blueprint([_slot(0), _slot(2)]), _approved(("a", 0), ("b", 2)))
    assert any("contiguous" in b for b in gap.blockers)
    duplicate = build_preview(_blueprint([_slot(0), _slot(0)]), _approved(("a", 0)))
    assert any("duplicate slot indexes" in b for b in duplicate.blockers)


def test_negative_marks_block_publication_until_the_deployment_enables_them() -> None:
    blueprint = _blueprint([_slot(0, marks=2, negative_marks=0.5)])
    off = build_preview(blueprint, _approved(("a", 0)))
    assert any("negative marking is not enabled" in b for b in off.blockers)
    on = build_preview(blueprint, _approved(("a", 0)), negative_marking_enabled=True)
    assert on.publishable and on.max_negative_marks == 0.5


def test_duration_shorter_than_the_slot_targets_is_a_warning() -> None:
    preview = build_preview(_blueprint([_slot(0, target_minutes=40), _slot(1, target_minutes=40)]),
                            _approved(("a", 0), ("b", 1)), duration_minutes=60)
    assert preview.publishable
    assert any("exceeds the exam duration" in w for w in preview.warnings)


def test_coding_slot_without_languages_and_odd_difficulty_are_warnings() -> None:
    preview = build_preview(
        _blueprint([_slot(0, item_type=ItemType.CODING, difficulty="legendary")]), _approved(("a", 0))
    )
    assert preview.publishable
    assert any("no permitted languages" in w for w in preview.warnings)
    assert any("unrecognised difficulty" in w for w in preview.warnings)


def test_snapshot_freezes_candidate_ids_sources_and_is_deterministic() -> None:
    blueprint = _blueprint([_slot(0)])
    candidates = [
        _candidate("b", 0, anchors=[("s1", "a2"), ("s1", "a1")]),
        _candidate("a", 0, anchors=[("s1", "a1")]),
    ]
    first = snapshot_slots(blueprint, group_approved_by_slot(candidates))
    second = snapshot_slots(blueprint, group_approved_by_slot(list(reversed(candidates))))
    assert first == second
    assert first[0].approved_candidate_ids == ["a", "b"]
    assert first[0].source_refs == ["s1:1:a1", "s1:1:a2"]
