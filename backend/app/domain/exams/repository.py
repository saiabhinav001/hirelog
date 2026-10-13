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
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import uuid4

from firebase_admin import firestore

from app.core.config import settings
from app.core.read_cache import cache_for
from app.domain.exams.models import (
    Attempt,
    CommandReceipt,
    Draft,
    ExamDefinition,
    ExamRevision,
    ExamRun,
    OutboxEntry,
    RunStatus,
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


def _run_cache(db):
    return cache_for(db, "exam_runs", ttl_seconds=settings.EXAM_RUN_CACHE_SECONDS, max_entries=256)


def _definition_cache(db):
    return cache_for(db, "exam_definitions", ttl_seconds=settings.EXAM_DEFINITION_CACHE_SECONDS, max_entries=512)


def invalidate_run_cache(db, run_id: str) -> None:
    """Drop the cached run after any write of the run document."""
    _run_cache(db).invalidate(run_id)


def invalidate_definition_cache(db, definition_id: str) -> None:
    _definition_cache(db).invalidate(definition_id)


class ExamDefinitionRepository:
    """Draft definitions are mutable; revisions (once created) are immutable
    — never updated, only created and read."""

    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, definition_id: str):
        return self._db.collection(DEFINITIONS_COLLECTION).document(definition_id)

    def _revisions(self, definition_id: str):
        return self._ref(definition_id).collection(REVISIONS_SUBCOLLECTION)

    def _definition_cache(self):
        return _definition_cache(self._db)

    def _revision_cache(self):
        return cache_for(self._db, "exam_revisions", ttl_seconds=settings.EXAM_REVISION_CACHE_SECONDS, max_entries=256)

    def invalidate(self, definition_id: str) -> None:
        invalidate_definition_cache(self._db, definition_id)

    def _load(self, definition_id: str) -> Optional[ExamDefinition]:
        snap = self._ref(definition_id).get()
        if not snap.exists:
            return None
        return ExamDefinition.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get(self, definition_id: str) -> Optional[ExamDefinition]:
        definition = self._definition_cache().get_or_load(definition_id, lambda: self._load(definition_id))
        return None if definition is None else definition.model_copy(deep=True)

    def create(self, definition: ExamDefinition) -> None:
        self._ref(definition.id).set(_model_dict(definition))
        self.invalidate(definition.id)

    def _load_revision(self, definition_id: str, revision_id: str) -> Optional[ExamRevision]:
        snap = self._revisions(definition_id).document(revision_id).get()
        if not snap.exists:
            return None
        return ExamRevision.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_revision(self, definition_id: str, revision_id: str) -> Optional[ExamRevision]:
        """A published revision never changes, so it is cached for a long time (a missing one is never cached)."""
        revision = self._revision_cache().get_or_load(
            (definition_id, revision_id), lambda: self._load_revision(definition_id, revision_id)
        )
        return None if revision is None else revision.model_copy(deep=True)

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
        # `create` (not `set`): a revision id can only ever be written once, so
        # no code path can overwrite a published revision. Optional Prompt 3
        # fields are omitted when unset so legacy-shaped revisions stay identical.
        transaction.create(
            self._revisions(revision.definition_id).document(revision.id),
            revision.model_dump(mode="json", exclude_none=True),
        )

    def set_latest_revision_in_transaction(self, transaction, definition_id: str, revision_id: str, now: datetime) -> None:
        transaction.update(
            self._ref(definition_id),
            {"latest_published_revision_id": revision_id, "updated_at": now.isoformat()},
        )
        self.invalidate(definition_id)

    def list_revisions(self, definition_id: str) -> list[ExamRevision]:
        return [
            ExamRevision.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            for doc in self._revisions(definition_id).stream()
        ]

    def list_by_owner(self, owner_uid: str) -> list[ExamDefinition]:
        query = self._db.collection(DEFINITIONS_COLLECTION).where(
            filter=firestore.FieldFilter("owner_uid", "==", owner_uid)
        )
        return [ExamDefinition.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def list_by_instructor(self, uid: str) -> list[ExamDefinition]:
        query = self._db.collection(DEFINITIONS_COLLECTION).where(
            filter=firestore.FieldFilter("instructor_uids", "array_contains", uid)
        )
        return [ExamDefinition.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def set_instructors_in_transaction(
        self, transaction, definition_id: str, instructor_uids: list[str], now: datetime
    ) -> None:
        transaction.update(self._ref(definition_id), {"instructor_uids": instructor_uids, "updated_at": now.isoformat()})
        self.invalidate(definition_id)


class ExamRunRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, run_id: str):
        return self._db.collection(RUNS_COLLECTION).document(run_id)

    def _cache(self):
        return _run_cache(self._db)

    def invalidate(self, run_id: str) -> None:
        """Drop the cached run (the writers below already do)."""
        invalidate_run_cache(self._db, run_id)

    def _load(self, run_id: str) -> Optional[ExamRun]:
        snap = self._ref(run_id).get()
        if not snap.exists:
            return None
        return ExamRun.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get(self, run_id: str) -> Optional[ExamRun]:
        """Cached; anything that decides a write must still use `get_in_transaction`."""
        run = self._cache().get_or_load(run_id, lambda: self._load(run_id))
        return None if run is None else run.model_copy(deep=True)

    def get_in_transaction(self, transaction, run_id: str) -> Optional[ExamRun]:
        snap = self._ref(run_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return ExamRun.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def create(self, run: ExamRun) -> None:
        self._ref(run.id).set(_model_dict(run))
        self.invalidate(run.id)

    def create_in_transaction(self, transaction, run: ExamRun) -> None:
        transaction.create(self._ref(run.id), _model_dict(run))
        self.invalidate(run.id)

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
        self.invalidate(run_id)

    def set_enrolled_in_transaction(self, transaction, run_id: str, enrolled_uids: list[str], now: datetime) -> None:
        """Access grant/revoke. Deliberately does NOT bump `version`: version
        guards lifecycle commands, and enrollment edits must not invalidate a
        faculty member's in-flight pause/close."""
        transaction.update(self._ref(run_id), {"enrolled_uids": enrolled_uids, "updated_at": now.isoformat()})
        self.invalidate(run_id)

    def list_by_statuses(self, statuses: list[RunStatus]) -> list[ExamRun]:
        """Single-field `in` query (no composite index) - the sweeper's view of
        runs that may still hold unsealed attempts or a due schedule action."""
        query = self._db.collection(RUNS_COLLECTION).where(
            filter=firestore.FieldFilter("status", "in", [s.value for s in statuses])
        )
        return [ExamRun.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def list_ended_since(self, cutoff: datetime) -> list[ExamRun]:
        """Runs hard-ended at/after `cutoff` (single-field range query on the ISO
        UTC `ended_at` string the commands write)."""
        query = self._db.collection(RUNS_COLLECTION).where(
            filter=firestore.FieldFilter("ended_at", ">=", cutoff.isoformat())
        )
        return [ExamRun.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

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

    def list_all_for_run(self, run_id: str, *, cap: int) -> list[Attempt]:
        """Every attempt of the run in uid order, at most `cap` (callers pass limit+1 to detect an oversize run)."""
        query = self._db.collection(RUNS_COLLECTION).document(run_id).collection(ATTEMPTS_SUBCOLLECTION).order_by("uid").limit(cap)
        return [Attempt.model_validate(doc.to_dict() or {}) for doc in query.stream()]

    def get_many(self, run_id: str, uids: list[str]) -> dict[str, Attempt]:
        """Point reads (one batched request) of just these students' attempts; absent ones are left out."""
        if not uids:
            return {}
        found: dict[str, Attempt] = {}
        for snap in self._db.get_all([self._ref(run_id, uid) for uid in uids]):
            if snap.exists:
                attempt = Attempt.model_validate(snap.to_dict() or {})
                found[attempt.uid] = attempt
        return found

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


@dataclass(frozen=True)
class DraftSummary:
    """What the navigator needs about one saved answer - never the answer itself."""

    version: int
    answered: bool
    marked_for_review: bool


_summary_lock = threading.Lock()


class DraftRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _summary_cache(self):
        return cache_for(self._db, "draft_summaries", ttl_seconds=settings.EXAM_DRAFT_SUMMARY_CACHE_SECONDS, max_entries=4096)

    def _load_summaries(self, run_id: str, uid: str) -> dict[str, DraftSummary]:
        summaries: dict[str, DraftSummary] = {}
        for doc in self._collection(run_id, uid).stream():
            data = doc.to_dict() or {}
            summaries[doc.id] = DraftSummary(
                version=int(data.get("version", 0)),
                answered=bool(data.get("answer")),
                marked_for_review=bool(data.get("marked_for_review")),
            )
        return summaries

    def summaries(self, run_id: str, uid: str) -> dict[str, DraftSummary]:
        """Per-question draft summaries for the navigator; loaded once, then kept current by `record_saved`. Never decides a write."""
        loaded = self._summary_cache().get_or_load((run_id, uid), lambda: self._load_summaries(run_id, uid))
        return dict(loaded)

    def record_saved(self, run_id: str, uid: str, draft: Draft) -> None:
        """Write-through after a committed save; with nothing cached it only fences any load already in flight."""
        cache = self._summary_cache()
        key = (run_id, uid)
        with _summary_lock:
            current = cache.peek(key)
            if current is None:
                cache.invalidate(key)
                return
            updated = dict(current)
            updated[draft.question_id] = DraftSummary(
                version=draft.version, answered=bool(draft.answer), marked_for_review=bool(draft.marked_for_review)
            )
            cache.put(key, updated)

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

    def all_versions_in_transaction(self, transaction, run_id: str, uid: str) -> dict[str, int]:
        """Part of the caller's transaction, so a draft saved between this read and the commit
        (including the first save of a slot) aborts and re-runs the seal instead of being missed."""
        docs = self._collection(run_id, uid).stream(transaction=transaction)
        return {doc.id: int((doc.to_dict() or {}).get("version", 0)) for doc in docs}

    def list_all(self, run_id: str, uid: str) -> list[Draft]:
        return [
            Draft.model_validate({**(doc.to_dict() or {}), "question_id": doc.id})
            for doc in self._collection(run_id, uid).stream()
        ]

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
        transaction.set(ref, receipt.model_dump(mode="json", exclude_none=True))


_CLAIM_MAX_PAGES = 4


class OutboxRepository:
    def __init__(self, db) -> None:
        self._db = db

    def add_in_transaction(self, transaction, entry: OutboxEntry) -> None:
        ref = self._db.collection(OUTBOX_COLLECTION).document(entry.id)
        transaction.set(ref, _model_dict(entry))

    def claim_batch(
        self, *, limit: int, lease_seconds: int, worker_id: str, now: datetime,
        kinds: Optional[list[str]] = None, exclude_kinds: tuple[str, ...] = (),
    ) -> list[OutboxEntry]:
        """Best-effort claim (optimistic — mirrors the existing
        `SearchIndexQueue` Firestore-claim pattern for consistency). A crash
        between claim and mark-processed just means another worker retries
        after the lease expires, hence handlers MUST be idempotent.

        `kinds` selects entries by kind (server-side); `exclude_kinds` skips kinds a loop must
        not touch (slow jobs run in their own loop). The exclusion is filtered here, a page at a
        time, to avoid a composite index; a backlog of excluded entries can delay others by up to
        `_CLAIM_MAX_PAGES` pages."""
        query = self._db.collection(OUTBOX_COLLECTION).where(filter=firestore.FieldFilter("processed", "==", False))
        if kinds:
            query = query.where(filter=firestore.FieldFilter("kind", "in", list(kinds)))
        wanted = max(1, limit)
        claimed: list[OutboxEntry] = []
        last_doc = None
        for _ in range(_CLAIM_MAX_PAGES):
            page_query = query.limit(wanted)
            if last_doc is not None:
                page_query = page_query.start_after(last_doc)
            page = list(page_query.stream())
            for doc in page:
                if len(claimed) >= wanted:
                    break
                data = doc.to_dict() or {}
                if data.get("kind") in exclude_kinds:
                    continue
                claimed_at = data.get("claimed_at")
                if claimed_at:
                    try:
                        claimed_dt = datetime.fromisoformat(claimed_at)
                        if (now - claimed_dt).total_seconds() < lease_seconds:
                            continue  # still leased by another worker
                    except (TypeError, ValueError):
                        pass
                doc.reference.update({"claimed_at": now.isoformat(), "claimed_by": worker_id})
                claimed.append(OutboxEntry.model_validate({**data, "id": doc.id}))
            if len(claimed) >= wanted or len(page) < wanted:
                break
            last_doc = page[-1]
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
