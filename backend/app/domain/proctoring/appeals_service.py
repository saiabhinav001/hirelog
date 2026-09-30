"""Student appeal workflow — a separate, explicit channel from faculty
incident resolution (`ProctoringLedgerService.resolve_block`). An appeal
can be filed regardless of current attempt status (even after the exam
window has closed) and never itself mutates the attempt; only a faculty
resolution that separately calls `resolve_block` does that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.domain.proctoring.models import Appeal, AppealStatus
from app.domain.proctoring.repository import AppealRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppealsService:
    def __init__(self, db) -> None:
        self.appeals = AppealRepository(db)

    def submit_appeal(self, *, run_id: str, uid: str, student_statement: str) -> Appeal:
        appeal = Appeal(id=uuid4().hex, run_id=run_id, uid=uid, student_statement=student_statement, created_at=_utc_now())
        self.appeals.create(appeal)
        return appeal

    def resolve_appeal(self, *, appeal_id: str, resolved_by: str, status: AppealStatus, resolution_notes: str) -> Appeal:
        appeal = self.appeals.get(appeal_id)
        if appeal is None:
            raise LookupError(f"appeal {appeal_id} not found")
        now = _utc_now()
        is_reversal = appeal.status != AppealStatus.PENDING
        self.appeals.update(
            appeal_id,
            {
                "status": status.value, "resolved_by": resolved_by, "resolved_at": now.isoformat(),
                "resolution_notes": resolution_notes, "version": appeal.version + 1 if is_reversal else appeal.version,
            },
        )
        return self.appeals.get(appeal_id)
