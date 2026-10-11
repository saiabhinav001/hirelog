"""Operator-only queue health (Prompt 11, OPS-05): one read-only endpoint whose figures the alert thresholds in
docs/exams/release-readiness.md are written against. Super-admin only, never cached."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.exams_dependencies import require_exam_super_admin, require_exams_enabled
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.domain.exams.clock import system_clock
from app.domain.exams.ops_health import collect_queue_health

router = APIRouter(prefix="/api/exams/v1/ops", tags=["exams-ops"], dependencies=[Depends(require_exams_enabled)])


@router.get("/queue-health")
def queue_health(response: Response, user: dict = Depends(require_exam_super_admin)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return collect_queue_health(db, now=system_clock.now(), outbox_max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS)
