"""Firestore repositories for the exam domain.

Thin wiring only — all interesting logic (legality, idempotency, conflicts)
lives in `state_machine.py`/`decisions.py` and is called from here with data
freshly read inside a transaction, so retries on contention always decide
against the current server state. No repository method here does a network
call to anything other than Firestore, and none call outbox handlers directly
(contract: "No external service calls or event publishing inside transaction
callbacks.").
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from firebase_admin import firestore

from app.domain.exams.models import (
    Attempt,
    CommandReceipt,
    Draft,
    ExamDefinition,
    ExamRevision,
    ExamRun,
    OutboxEntry,
)

logger = logging.getLogger("hirelog.exams")

DEFINITIONS_COLLECTION = "exam_definitions"
REVISIONS_SUBCOLLECTION = "revisions"
RUNS_COLLECTION = "exam_runs"
ATTEMPTS_SUBCOLLECTION = "attempts"
DRAFTS_SUBCOLLECTION = "drafts"
COMMAND_RECEIPTS_COLLECTION = "exam_command_receipts"
OUTBOX_COLLECTION = "exam_outbox"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class ExamDefinitionRepository:
    """Draft definitions are mutable; revisions (once created) are immutable
    — never updated, only created and read."""

    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, definition_id: str):
        return self._db.collection(DEFINITIONS_COLLECTION).document(definition_id)

    def _revisions(self, definition_id: str):
        return self._ref(definition_id).collection(REVISIONS_SUBCOLLECTION)

    def get(self, definition_id: str) -> Optional[ExamDefinition]:
        snap = self._ref(definition_id).get()
        if not snap.exists:
            return None
        return ExamDefinition.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def create(self, definition: ExamDefinition) -> None:
        self._ref(definition.id).set(_model_dict(definition))

    def get_revision(self, definition_id: str, revision_id: str) -> Optional[ExamRevision]:
        snap = self._revisions(definition_id).document(revision_id).get()
        if not snap.exists:
            return None
        return ExamRevision.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_revision_in_transaction(self, transaction, definition_id: str, revision_id: str) -> Optional[ExamRevision]:
        snap = self._revisions(definition_id).document(revision_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return ExamRevision.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_in_transaction(self, transaction, definition_id: str) -> Optional[ExamDefinition]:
        snap = self._ref(definition_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return ExamDefinition.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def create_revision_in_transaction(self, transaction, revision: ExamRevision) -> None:
        transaction.set(self._revisions(revision.definition_id).document(revision.id), _model_dict(revision))

    def set_latest_revision_in_transaction(self, transaction, definition_id: str, revision_id: str, now: datetime) -> None:
        transaction.update(
            self._ref(definition_id),
            {"latest_published_revision_id": revision_id, "updated_at": now.isoformat()},
        )

    def list_by_owner(self, owner_uid: str) -> list[ExamDefinition]:
        query = self._db.collection(DEFINITIONS_COLLECTION).where(
            filter=firestore.FieldFilter("owner_uid", "==", owner_uid)
        )
        return [ExamDefinition.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]


class ExamRunRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, run_id: str):
        return self._db.collection(RUNS_COLLECTION).document(run_id)

    def get(self, run_id: str) -> Optional[ExamRun]:
        snap = self._ref(run_id).get()
        if not snap.exists:
            return None
        return ExamRun.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_in_transaction(self, transaction, run_id: str) -> Optional[ExamRun]:
        snap = self._ref(run_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return ExamRun.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def create(self, run: ExamRun) -> None:
        self._ref(run.id).set(_model_dict(run))

    def apply_command_in_transaction(
        self, transaction, run_id: str, *, new_status, new_version: int, now: datetime, extra: dict
    ) -> None:
        ref = self._ref(run_id)
        update = {
            "status": new_status.value,
            "version": new_version,
            "updated_at": now.isoformat(),
            **extra,
        }
        transaction.update(ref, update)

    def list_by_definition(self, definition_id: str) -> list[ExamRun]:
        query = self._db.collection(RUNS_COLLECTION).where(
            filter=firestore.FieldFilter("definition_id", "==", definition_id)
        )
        return [ExamRun.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def list_for_student(self, uid: str) -> list[ExamRun]:
        """'Assigned exams' screen data. Draft runs are never enrolled, so
        this can never surface a run before it has been published/scheduled."""
        query = self._db.collection(RUNS_COLLECTION).where(
            filter=firestore.FieldFilter("enrolled_uids", "array_contains", uid)
        )
        return [ExamRun.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]


class AttemptRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, run_id: str, uid: str):
        return (
            self._db.collection(RUNS_COLLECTION)
            .document(run_id)
            .collection(ATTEMPTS_SUBCOLLECTION)
            .document(uid)
        )

    def get(self, run_id: str, uid: str) -> Optional[Attempt]:
        snap = self._ref(run_id, uid).get()
        if not snap.exists:
            return None
        return Attempt.model_validate(snap.to_dict() or {})

    def get_in_transaction(self, transaction, run_id: str, uid: str) -> Optional[Attempt]:
        snap = self._ref(run_id, uid).get(transaction=transaction)
        if not snap.exists:
            return None
        return Attempt.model_validate(snap.to_dict() or {})

    def create_in_transaction(self, transaction, attempt: Attempt) -> None:
        transaction.set(self._ref(attempt.run_id, attempt.uid), _model_dict(attempt))

    def update_in_transaction(self, transaction, run_id: str, uid: str, update: dict) -> None:
        transaction.update(self._ref(run_id, uid), update)

    def list_in_progress(self, run_id: str) -> list[Attempt]:
        """Non-transactional read used only to BUILD a finalization batch —
        each attempt in the batch is then sealed in its own small transaction
        by the caller, never all together (bounded finalization jobs)."""
        query = (
            self._db.collection(RUNS_COLLECTION)
            .document(run_id)
            .collection(ATTEMPTS_SUBCOLLECTION)
            .where(filter=firestore.FieldFilter("status", "==", "in_progress"))
        )
        return [Attempt.model_validate(doc.to_dict() or {}) for doc in query.stream()]

    def list_blocked(self, run_id: str) -> list[Attempt]:
        """Same bounded-batch contract as `list_in_progress` — BLOCKED
        attempts are also seal-eligible once their deadline passes
        (Prompt 8: a review-blocked attempt is not exempt from expiry)."""
        query = (
            self._db.collection(RUNS_COLLECTION)
            .document(run_id)
            .collection(ATTEMPTS_SUBCOLLECTION)
            .where(filter=firestore.FieldFilter("status", "==", "blocked"))
        )
        return [Attempt.model_validate(doc.to_dict() or {}) for doc in query.stream()]

    def list_page_for_run(self, run_id: str, *, limit: int, cursor_uid: Optional[str] = None) -> tuple[list[Attempt], Optional[str]]:
        """Candidate grid data, paginated by uid. Fetches one extra row to
        decide whether a next page exists without a separate count query."""
        query = (
            self._db.collection(RUNS_COLLECTION)
            .document(run_id)
            .collection(ATTEMPTS_SUBCOLLECTION)
            .order_by("uid")
            .limit(limit + 1)
        )
        if cursor_uid:
            query = query.start_after({"uid": cursor_uid})
        docs = list(query.stream())
        attempts = [Attempt.model_validate(d.to_dict() or {}) for d in docs[:limit]]
        next_cursor = attempts[-1].uid if len(docs) > limit else None
        return attempts, next_cursor


class DraftRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _collection(self, run_id: str, uid: str):
        return (
            self._db.collection(RUNS_COLLECTION)
            .document(run_id)
            .collection(ATTEMPTS_SUBCOLLECTION)
            .document(uid)
            .collection(DRAFTS_SUBCOLLECTION)
        )

    def get(self, run_id: str, uid: str, question_id: str) -> Optional[Draft]:
        snap = self._collection(run_id, uid).document(question_id).get()
        if not snap.exists:
            return None
        return Draft.model_validate({**(snap.to_dict() or {}), "question_id": snap.id})

    def get_in_transaction(self, transaction, run_id: str, uid: str, question_id: str) -> Optional[Draft]:
        snap = self._collection(run_id, uid).document(question_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return Draft.model_validate({**(snap.to_dict() or {}), "question_id": snap.id})

    def all_versions(self, run_id: str, uid: str) -> dict[str, int]:
        docs = self._collection(run_id, uid).stream()
        return {doc.id: int((doc.to_dict() or {}).get("version", 0)) for doc in docs}

    def set_in_transaction(self, transaction, run_id: str, uid: str, draft: Draft) -> None:
        ref = self._collection(run_id, uid).document(draft.question_id)
        transaction.set(ref, _model_dict(draft))


class CommandReceiptRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _doc_id(self, run_id: str, command: str, idempotency_key: str) -> str:
        return f"{run_id}__{command}__{idempotency_key}"

    def get_in_transaction(
        self, transaction, run_id: str, command: str, idempotency_key: str
    ) -> Optional[CommandReceipt]:
        ref = self._db.collection(COMMAND_RECEIPTS_COLLECTION).document(
            self._doc_id(run_id, command, idempotency_key)
        )
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        return CommandReceipt.model_validate(snap.to_dict() or {})

    def create_in_transaction(self, transaction, receipt: CommandReceipt) -> None:
        ref = self._db.collection(COMMAND_RECEIPTS_COLLECTION).document(
            self._doc_id(receipt.run_id, receipt.command, receipt.idempotency_key)
        )
        transaction.set(ref, _model_dict(receipt))


class OutboxRepository:
    def __init__(self, db) -> None:
        self._db = db

    def add_in_transaction(self, transaction, entry: OutboxEntry) -> None:
        ref = self._db.collection(OUTBOX_COLLECTION).document(entry.id)
        transaction.set(ref, _model_dict(entry))

    def claim_batch(self, *, limit: int, lease_seconds: int, worker_id: str, now: datetime) -> list[OutboxEntry]:
        """Best-effort claim (optimistic — mirrors the existing
        `SearchIndexQueue` Firestore-claim pattern for consistency). A crash
        between claim and mark-processed just means another worker retries
        after the lease expires, hence handlers MUST be idempotent."""
        query = (
            self._db.collection(OUTBOX_COLLECTION)
            .where(filter=firestore.FieldFilter("processed", "==", False))
            .limit(max(1, limit))
        )
        claimed: list[OutboxEntry] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            claimed_at = data.get("claimed_at")
            if claimed_at:
                try:
                    claimed_dt = datetime.fromisoformat(claimed_at)
                    if (now - claimed_dt).total_seconds() < lease_seconds:
                        continue  # still leased by another worker
                except (TypeError, ValueError):
                    pass
            doc.reference.update({"claimed_at": now.isoformat(), "claimed_by": worker_id})
            entry = OutboxEntry.model_validate({**data, "id": doc.id})
            claimed.append(entry)
        return claimed

    def mark_processed(self, entry_id: str, *, now: datetime) -> None:
        self._db.collection(OUTBOX_COLLECTION).document(entry_id).update(
            {"processed": True, "processed_at": now.isoformat()}
        )

    def mark_failed(self, entry_id: str, *, error: str, attempts: int) -> None:
        self._db.collection(OUTBOX_COLLECTION).document(entry_id).update(
            {"attempts": attempts, "last_error": str(error)[:500]}
        )


def new_outbox_entry(*, kind: str, run_id: str, payload: dict, now: datetime) -> OutboxEntry:
    return OutboxEntry(id=uuid4().hex, kind=kind, run_id=run_id, payload=payload, created_at=now)
