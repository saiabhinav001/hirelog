"""Pure `LiveEvent` payload builders. Every builder here takes an
already-safe domain object and extracts ONLY summary/version fields — never
question content, answers, rubrics or evidence (contract #3). This is the
single place that decides what a live event may contain, so a future
addition can't accidentally widen it per-callsite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.domain.exams.models import Attempt, ExamRun
from app.domain.live.models import LiveEvent, LiveEventKind


def snapshot_event(*, run_id: str, seq: int, state_version: int, now: datetime) -> LiveEvent:
    return LiveEvent(seq=seq, kind=LiveEventKind.SNAPSHOT, run_id=run_id, state_version=state_version, created_at=now)


def run_state_changed_event(run: ExamRun, *, seq: int, now: datetime) -> LiveEvent:
    return LiveEvent(
        seq=seq, kind=LiveEventKind.RUN_STATE_CHANGED, run_id=run.id, state_version=run.version,
        payload={"status": run.status.value, "version": run.version},
        target_uid=None,  # broadcast — every connection for this run
        created_at=now,
    )


def attempt_status_changed_event(attempt: Attempt, *, seq: int, now: datetime) -> LiveEvent:
    return LiveEvent(
        seq=seq, kind=LiveEventKind.ATTEMPT_STATUS_CHANGED, run_id=attempt.run_id, state_version=attempt.version,
        payload={
            "uid": attempt.uid,
            "status": attempt.status.value,
            "version": attempt.version,
            "warning_count": attempt.warning_count,
            "current_slot_index": attempt.current_slot_index,
        },
        target_uid=attempt.uid,  # faculty room + this student's own connection(s) only
        created_at=now,
    )


def heartbeat_event(*, run_id: str, seq: int, state_version: int, now: datetime, target_uid: Optional[str] = None) -> LiveEvent:
    return LiveEvent(
        seq=seq, kind=LiveEventKind.HEARTBEAT, run_id=run_id, state_version=state_version,
        target_uid=target_uid, created_at=now,
    )
