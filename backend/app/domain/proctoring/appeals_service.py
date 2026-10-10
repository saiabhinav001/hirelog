"""Student appeal workflow — a separate, explicit channel from faculty incident resolution
(`ProctoringLedgerService.resolve_block`). An appeal can be filed regardless of current attempt status (even after the
exam window has closed) and never itself mutates the attempt; only a reviewer's separate `resolve_block` does that.

Every reviewer decision is APPENDED to the appeal's history inside a transaction together with an audit event, so a
reversal keeps what was decided before and two reviewers cannot overwrite each other. A student has at most one pending
appeal per attempt: filing again returns it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from firebase_admin import firestore

from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.proctoring.models import Appeal, AppealDecision, AppealStatus
from app.domain.proctoring.repository import AppealRepository

DECIDING_STATUSES = frozenset({AppealStatus.UPHELD, AppealStatus.OVERTURNED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppealsService:
    def __init__(self, db) -> None:
        self._db = db
        self.appeals = AppealRepository(db)
        self._audit = AuthzAuditRepository(db)

    def submit_appeal(self, *, run_id: str, uid: str, student_statement: str) -> Appeal:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> Appeal:
            for existing in self.appeals.list_for_attempt_in_transaction(transaction, run_id, uid):
                if existing.status == AppealStatus.PENDING:
                    return existing
            appeal = Appeal(id=uuid4().hex, run_id=run_id, uid=uid, student_statement=student_statement, created_at=_utc_now())
            self.appeals.create_in_transaction(transaction, appeal)
            return appeal

        return _txn(transaction)

    def resolve_appeal(self, *, appeal_id: str, resolved_by: str, status: AppealStatus, resolution_notes: str) -> Appeal:
        if status not in DECIDING_STATUSES:
            raise ValueError("an appeal is resolved as upheld or overturned")
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> Appeal:
            appeal = self.appeals.get_in_transaction(transaction, appeal_id)
            if appeal is None:
                raise LookupError(f"appeal {appeal_id} not found")
            now = _utc_now()
            is_reversal = appeal.status != AppealStatus.PENDING and appeal.status != status
            decision = AppealDecision(status=status, decided_by=resolved_by, decided_at=now, notes=resolution_notes)
            updated = appeal.model_copy(update={
                "status": status, "resolved_by": resolved_by, "resolved_at": now, "resolution_notes": resolution_notes,
                "version": appeal.version + 1 if is_reversal else appeal.version,
                "decisions": [*appeal.decisions, decision],
            })
            self.appeals.update_in_transaction(transaction, appeal_id, updated.model_dump(mode="json"))
            self._audit.append_in_transaction(transaction, build_audit_event(
                "exam_appeal_resolved", uid=appeal.uid, actor_uid=resolved_by,
                detail={"run_id": appeal.run_id, "appeal_id": appeal_id, "status": status.value, "reversal": is_reversal},
                now=now,
            ))
            return updated

        return _txn(transaction)
