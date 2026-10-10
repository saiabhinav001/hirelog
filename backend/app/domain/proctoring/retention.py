"""Bounded, idempotent retention sweep — deletes evidence snapshots (storage bytes + Firestore record together) once past
the retention window, and marks upload requests that can no longer complete as missing. Mirrors the bounded-batch pattern
of `ExamFinalizationSweeper` / `JudgeService.sweep_pending_once`.

Behaviour worth knowing:
- The scan runs oldest-first in pages, so the snapshots that are due are reached before newer ones.
- The window is the run's own (`proctoring_profile.evidence_retention_days`) or the deployment default.
- Evidence is HELD, not deleted, while the attempt is blocked for review or a student's appeal is pending: it is what
  the reviewer needs. Once the review is decided the normal window applies again.
- One snapshot that cannot be deleted never stops the others; storage is deleted before the record, so a failure is
  retried on the next sweep. There are no derivatives (thumbnails, exports) of a snapshot in this codebase; if one is
  ever added it must be deleted here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.domain.exams.models import AttemptStatus
from app.domain.proctoring.decisions import effective_proctoring, is_past_retention
from app.domain.proctoring.evidence_service import EvidenceService
from app.domain.proctoring.models import AppealStatus

logger = logging.getLogger("hirelog.exams.proctoring.retention")


class RetentionSweeper:
    def __init__(self, evidence_service: EvidenceService, *, runs=None, attempts=None, appeals=None) -> None:
        self._evidence = evidence_service
        self._runs = runs
        self._attempts = attempts
        self._appeals = appeals

    def sweep_once(self, *, retention_days: int, limit: int = 100, now: datetime | None = None, scan_pages: int = 5) -> int:
        now = now or datetime.now(timezone.utc)
        deleted = 0
        cursor = None
        run_days: dict[str, int] = {}
        holds: dict[tuple[str, str], bool] = {}
        pending_appeals: dict[str, set[str]] = {}
        for _ in range(scan_pages):
            page = self._evidence.snapshots.list_oldest(limit=limit, after_requested_at=cursor)
            if not page:
                break
            for snapshot in page:
                try:
                    days = self._days_for(snapshot.run_id, retention_days, run_days)
                    if not is_past_retention(snapshot.uploaded_at or snapshot.requested_at, now=now, retention_days=days):
                        continue
                    if self._held(snapshot.run_id, snapshot.uid, holds, pending_appeals):
                        continue
                    self._evidence.delete_snapshot(snapshot)
                    deleted += 1
                except Exception as exc:  # noqa: BLE001 - leave it for the next sweep
                    logger.warning("evidence retention: snapshot %s not deleted: %s", snapshot.id, type(exc).__name__)
            if len(page) < limit:
                break
            cursor = page[-1].model_dump(mode="json")["requested_at"]
        return deleted

    def expire_stale_requests(self, *, upload_ticket_ttl_seconds: float, now: datetime | None = None, grace_seconds: float = 60.0) -> int:
        """Upload requests whose ticket has lapsed (plus a small grace) become `missing / upload_not_completed`."""
        return self._evidence.expire_stale_requests(
            now=now or datetime.now(timezone.utc), older_than_seconds=upload_ticket_ttl_seconds + grace_seconds,
        )

    def _days_for(self, run_id: str, default_days: int, cache: dict[str, int]) -> int:
        if run_id not in cache:
            run = self._runs.get(run_id) if self._runs is not None else None
            cache[run_id] = (
                effective_proctoring(run.policy, default_grace=0.0, default_cooldown=0.0, default_retention_days=default_days).retention_days
                if run is not None else default_days
            )
        return cache[run_id]

    def _held(self, run_id: str, uid: str, cache: dict[tuple[str, str], bool], pending: dict[str, set[str]]) -> bool:
        key = (run_id, uid)
        if key not in cache:
            held = False
            if self._attempts is not None:
                attempt = self._attempts.get(run_id, uid)
                held = attempt is not None and attempt.status == AttemptStatus.BLOCKED
            if not held and self._appeals is not None:
                if run_id not in pending:
                    pending[run_id] = {a.uid for a in self._appeals.list_for_run(run_id) if a.status == AppealStatus.PENDING}
                held = uid in pending[run_id]
            cache[key] = held
        return cache[key]
