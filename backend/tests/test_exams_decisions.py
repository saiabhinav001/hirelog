"""Pure decision-layer tests: commands, admission, final submission, drafts,
and the bounded finalization sweep. No Firestore — every scenario from the
Prompt 3/4 test list is exercised directly against plain models.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.exams.decisions import (
    check_epoch,
    compute_deadline,
    decide_finalization_batch,
    decide_run_command,
    decide_save_draft,
    decide_seal_on_expiry,
    decide_submit,
    is_content_reachable,
)
from app.domain.exams.errors import UnresolvedAttemptsError, VersionConflictError
from app.domain.exams.models import (
    Attempt,
    AttemptStatus,
    CommandReceipt,
    ExamRun,
    RunStatus,
)
from app.domain.exams.policy import ExamPolicy

NOW = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def _run(status: RunStatus = RunStatus.RELEASED, version: int = 1, **overrides) -> ExamRun:
    defaults = dict(
        id="run-1",
        definition_id="def-1",
        revision_id="rev-1",
        status=status,
        version=version,
        policy=ExamPolicy(duration_minutes=60),
        enrolled_uids=["u1"],
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return ExamRun(**defaults)


def _attempt(status: AttemptStatus = AttemptStatus.IN_PROGRESS, **overrides) -> Attempt:
    defaults = dict(
        run_id="run-1",
        uid="u1",
        status=status,
        version=1,
        epoch=1,
        admitted_at=NOW,
        deadline=NOW + timedelta(minutes=60),
    )
    defaults.update(overrides)
    return Attempt(**defaults)


# ── Faculty commands ──────────────────────────────────────────────────────────

def test_command_applies_on_legal_transition_with_matching_version() -> None:
    run = _run(status=RunStatus.PUBLISHED, version=3)
    outcome = decide_run_command(
        run=run, command="release", expected_version=3, idempotency_key="k1", now=NOW, existing_receipt=None
    )
    assert outcome.applied
    assert outcome.receipt.resulting_status == RunStatus.RELEASED
    assert outcome.receipt.resulting_version == 4


def test_command_rejects_stale_expected_version() -> None:
    run = _run(status=RunStatus.PUBLISHED, version=3)
    with pytest.raises(VersionConflictError) as exc:
        decide_run_command(
            run=run, command="release", expected_version=2, idempotency_key="k1", now=NOW, existing_receipt=None
        )
    assert exc.value.expected == 2 and exc.value.actual == 3


def test_duplicate_command_returns_existing_receipt_without_reapplying() -> None:
    run = _run(status=RunStatus.PUBLISHED, version=3)
    existing = CommandReceipt(
        run_id="run-1", command="release", idempotency_key="k1", applied_at=NOW,
        resulting_status=RunStatus.RELEASED, resulting_version=4,
    )
    outcome = decide_run_command(
        run=run, command="release", expected_version=3, idempotency_key="k1", now=NOW, existing_receipt=existing
    )
    assert outcome.applied is False
    assert outcome.reused_existing_receipt is True
    assert outcome.receipt is existing


def test_close_with_unresolved_attempts_warns_without_force() -> None:
    run = _run(status=RunStatus.RELEASED, version=1)
    with pytest.raises(UnresolvedAttemptsError) as exc:
        decide_run_command(
            run=run, command="close", expected_version=1, idempotency_key="k2", now=NOW,
            existing_receipt=None, unresolved_attempt_count=3, force=False,
        )
    assert exc.value.count == 3


def test_close_with_unresolved_attempts_succeeds_with_force() -> None:
    run = _run(status=RunStatus.RELEASED, version=1)
    outcome = decide_run_command(
        run=run, command="close", expected_version=1, idempotency_key="k2", now=NOW,
        existing_receipt=None, unresolved_attempt_count=3, force=True,
    )
    assert outcome.applied


def test_illegal_command_transition_raises() -> None:
    run = _run(status=RunStatus.DRAFT, version=1)
    from app.domain.exams.errors import IllegalTransitionError

    with pytest.raises(IllegalTransitionError):
        decide_run_command(
            run=run, command="close", expected_version=1, idempotency_key="k3", now=NOW, existing_receipt=None
        )


# ── Deadlines / accommodations ────────────────────────────────────────────────

def test_compute_deadline_adds_accommodation_and_extension() -> None:
    admitted_at = NOW
    deadline = compute_deadline(
        admitted_at=admitted_at, duration_minutes=60, accommodation_extra_minutes=15, manual_extension_minutes=5
    )
    assert deadline == admitted_at + timedelta(minutes=80)


# ── Final submission ─────────────────────────────────────────────────────────

def test_submit_accepted_when_versions_match() -> None:
    run = _run()
    attempt = _attempt()
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={"q1": 2}, current_draft_versions={"q1": 2},
    )
    assert outcome.accepted
    assert outcome.receipt.idempotency_key == "s1"


def test_submit_double_click_same_key_returns_identical_receipt() -> None:
    """Identical retry (double click / lost response) returns the SAME
    receipt, not a new one, and does not require re-matching draft versions."""
    run = _run()
    first = decide_submit(
        attempt=_attempt(), run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={"q1": 2}, current_draft_versions={"q1": 2},
    )
    finalized_attempt = _attempt(status=AttemptStatus.FINALIZED, submit_receipt=first.receipt)
    second = decide_submit(
        attempt=finalized_attempt, run=run, now=NOW + timedelta(seconds=5), idempotency_key="s1",
        client_epoch=1, expected_draft_versions={}, current_draft_versions={"q1": 99},
    )
    assert second.accepted
    assert second.reused_existing_receipt
    assert second.receipt.receipt_id == first.receipt.receipt_id


def test_submit_different_key_after_finalized_is_explicit_conflict() -> None:
    run = _run()
    first = decide_submit(
        attempt=_attempt(), run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={}, current_draft_versions={},
    )
    finalized_attempt = _attempt(status=AttemptStatus.FINALIZED, submit_receipt=first.receipt)
    second = decide_submit(
        attempt=finalized_attempt, run=run, now=NOW, idempotency_key="DIFFERENT", client_epoch=1,
        expected_draft_versions={}, current_draft_versions={},
    )
    assert not second.accepted
    assert second.conflict_reason == "already_finalized"


def test_submit_rejects_stale_epoch() -> None:
    run = _run()
    attempt = _attempt(epoch=5)
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=4,
        expected_draft_versions={}, current_draft_versions={},
    )
    assert not outcome.accepted
    assert outcome.conflict_reason == "stale_epoch"


def test_submit_rejects_after_deadline() -> None:
    run = _run()
    attempt = _attempt(deadline=NOW - timedelta(seconds=1))
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={}, current_draft_versions={},
    )
    assert not outcome.accepted
    assert outcome.conflict_reason == "deadline_passed"


def test_submit_rejects_draft_version_conflict() -> None:
    """Changed payload/key conflict is explicit — never a silent overwrite."""
    run = _run()
    attempt = _attempt()
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={"q1": 1}, current_draft_versions={"q1": 2},
    )
    assert not outcome.accepted
    assert outcome.conflict_reason == "draft_version_conflict"


def test_submit_rejects_when_blocked() -> None:
    run = _run()
    attempt = _attempt(status=AttemptStatus.BLOCKED)
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={}, current_draft_versions={},
    )
    assert not outcome.accepted
    assert outcome.conflict_reason == "attempt_blocked"


def test_submit_rejects_when_run_ended() -> None:
    run = _run(status=RunStatus.ENDED)
    attempt = _attempt()
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={}, current_draft_versions={},
    )
    assert not outcome.accepted
    assert outcome.conflict_reason == "run_ended"


def test_submit_rejects_when_run_paused() -> None:
    run = _run(status=RunStatus.PAUSED)
    attempt = _attempt()
    outcome = decide_submit(
        attempt=attempt, run=run, now=NOW, idempotency_key="s1", client_epoch=1,
        expected_draft_versions={}, current_draft_versions={},
    )
    assert not outcome.accepted
    assert outcome.conflict_reason == "run_paused"


def test_check_epoch_helper() -> None:
    attempt = _attempt(epoch=3)
    assert check_epoch(attempt, 3) is None
    assert check_epoch(attempt, 2) == "stale_epoch"


# ── Draft save ─────────────────────────────────────────────────────────────

def test_save_draft_accepted_bumps_version() -> None:
    run = _run()
    attempt = _attempt()
    outcome = decide_save_draft(attempt=attempt, run=run, now=NOW, expected_version=2, current_version=2)
    assert outcome.accepted
    assert outcome.new_version == 3


def test_save_draft_stale_autosave_is_explicit_conflict() -> None:
    run = _run()
    attempt = _attempt()
    outcome = decide_save_draft(attempt=attempt, run=run, now=NOW, expected_version=1, current_version=2)
    assert not outcome.accepted
    assert outcome.conflict_reason == "draft_version_conflict"


def test_save_draft_rejected_after_deadline() -> None:
    run = _run()
    attempt = _attempt(deadline=NOW - timedelta(seconds=1))
    outcome = decide_save_draft(attempt=attempt, run=run, now=NOW, expected_version=0, current_version=0)
    assert not outcome.accepted
    assert outcome.conflict_reason == "deadline_passed"


# ── Content reachability (question delivery gating) ──────────────────────────

def test_content_unreachable_before_release() -> None:
    run = _run(status=RunStatus.PUBLISHED)
    attempt = _attempt(status=AttemptStatus.ADMITTED)
    assert is_content_reachable(run=run, attempt=attempt, now=NOW) == "run_not_released"


def test_content_reachable_while_paused_for_accommodated_student() -> None:
    """Pausing/closing a cohort must not cut off an accommodated, still
    in-progress student from their own current question."""
    run = _run(status=RunStatus.PAUSED)
    attempt = _attempt(status=AttemptStatus.IN_PROGRESS, deadline=NOW + timedelta(minutes=30))
    assert is_content_reachable(run=run, attempt=attempt, now=NOW) is None


def test_content_unreachable_once_run_ended() -> None:
    run = _run(status=RunStatus.ENDED)
    attempt = _attempt()
    assert is_content_reachable(run=run, attempt=attempt, now=NOW) == "run_ended"


def test_content_unreachable_when_blocked() -> None:
    run = _run()
    attempt = _attempt(status=AttemptStatus.BLOCKED)
    assert is_content_reachable(run=run, attempt=attempt, now=NOW) == "attempt_blocked"


# ── Bounded finalization sweep / accommodation preservation ──────────────────

def test_seal_on_expiry_leaves_accommodated_student_alone() -> None:
    attempt = _attempt(deadline=NOW + timedelta(minutes=30))  # still has time
    assert decide_seal_on_expiry(attempt=attempt, now=NOW, current_draft_versions={}) is None


def test_seal_on_expiry_seals_past_deadline_attempt() -> None:
    attempt = _attempt(deadline=NOW - timedelta(seconds=1))
    decision = decide_seal_on_expiry(attempt=attempt, now=NOW, current_draft_versions={"q1": 4})
    assert decision is not None
    assert decision.new_status == AttemptStatus.EXPIRED
    assert decision.receipt.sealed_draft_versions == {"q1": 4}


def test_seal_on_expiry_skips_already_resolved_attempts() -> None:
    attempt = _attempt(status=AttemptStatus.FINALIZED, deadline=NOW - timedelta(seconds=1))
    assert decide_seal_on_expiry(attempt=attempt, now=NOW, current_draft_versions={}) is None


def test_finalization_batch_is_bounded() -> None:
    """Never processes the whole run in one call — batch_size caps it."""
    attempts = [
        _attempt(uid=f"u{i}", deadline=NOW - timedelta(seconds=1)) for i in range(10)
    ]
    result = decide_finalization_batch(
        attempts=attempts, now=NOW, draft_versions_by_uid={}, batch_size=4
    )
    assert result.sealed_count == 4
    assert result.skipped == 6


def test_finalization_batch_preserves_accommodated_students_mixed_in() -> None:
    expired = [_attempt(uid=f"e{i}", deadline=NOW - timedelta(seconds=1)) for i in range(3)]
    accommodated = [_attempt(uid="acc-1", deadline=NOW + timedelta(minutes=45))]
    result = decide_finalization_batch(
        attempts=expired + accommodated, now=NOW, draft_versions_by_uid={}, batch_size=10
    )
    sealed_uids = {d.uid for d in result.sealed}
    assert sealed_uids == {"e0", "e1", "e2"}
    assert "acc-1" not in sealed_uids
