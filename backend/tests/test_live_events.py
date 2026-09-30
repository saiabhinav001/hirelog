"""`LiveEvent` builder tests — asserts the exact allowlisted payload shape
for each event kind and the broadcast-vs-targeted scoping contract."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.exams.models import Attempt, AttemptStatus, ExamRun, RunStatus
from app.domain.exams.policy import ExamPolicy
from app.domain.live.events import attempt_status_changed_event, run_state_changed_event, snapshot_event
from app.domain.live.models import LiveEventKind

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _run(**overrides) -> ExamRun:
    defaults = dict(
        id="run-1", definition_id="def-1", revision_id="rev-1", status=RunStatus.RELEASED, version=3,
        policy=ExamPolicy(duration_minutes=60), created_at=NOW, updated_at=NOW,
    )
    defaults.update(overrides)
    return ExamRun(**defaults)


def _attempt(**overrides) -> Attempt:
    defaults = dict(run_id="run-1", uid="u1", status=AttemptStatus.IN_PROGRESS, version=2, warning_count=1, current_slot_index=3)
    defaults.update(overrides)
    return Attempt(**defaults)


def test_snapshot_event_carries_no_payload() -> None:
    event = snapshot_event(run_id="run-1", seq=5, state_version=3, now=NOW)
    assert event.kind == LiveEventKind.SNAPSHOT
    assert event.payload == {}
    assert event.target_uid is None


def test_run_state_changed_event_is_broadcast_scoped() -> None:
    event = run_state_changed_event(_run(), seq=1, now=NOW)
    assert event.target_uid is None
    assert event.payload == {"status": "released", "version": 3}
    assert event.state_version == 3


def test_attempt_status_changed_event_is_scoped_to_that_uid() -> None:
    event = attempt_status_changed_event(_attempt(), seq=1, now=NOW)
    assert event.target_uid == "u1"
    assert event.payload == {
        "uid": "u1", "status": "in_progress", "version": 2, "warning_count": 1, "current_slot_index": 3,
    }


def test_attempt_status_changed_event_never_includes_answers_or_drafts() -> None:
    """The Attempt model has no answer content on it at all, but this is a
    regression guard on the event's own allowlist in case that ever changes."""
    event = attempt_status_changed_event(_attempt(), seq=1, now=NOW)
    assert "answer" not in event.payload
    assert "draft" not in str(event.payload).lower()
