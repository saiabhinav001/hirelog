"""Bounded, idempotent retention sweep — deletes evidence snapshots (storage
bytes + Firestore record together) once past the configured retention
window. Mirrors the exact bounded-batch pattern used by
`ExamFinalizationSweeper`/`JudgeService.sweep_pending_once`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.proctoring.decisions import is_past_retention
from app.domain.proctoring.evidence_service import EvidenceService


class RetentionSweeper:
    def __init__(self, evidence_service: EvidenceService) -> None:
        self._evidence = evidence_service

    def sweep_once(self, *, retention_days: int, limit: int = 100, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        deleted = 0
        for snapshot in self._evidence.snapshots.list_all(limit=limit):
            reference_time = snapshot.uploaded_at or snapshot.requested_at
            if is_past_retention(reference_time, now=now, retention_days=retention_days):
                self._evidence.delete_snapshot(snapshot)
                deleted += 1
        return deleted
