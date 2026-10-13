"""Student-facing exam service — scoped state/question/draft/submit/finish and
receipt reconciliation (Prompt 4). Thin transactional wiring only; all
conflict/idempotency/deadline logic lives in `decisions.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from firebase_admin import firestore

from app.core import background
from app.core.config import settings
from app.domain.exams import sweep_hints
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.decisions import (
    AdvanceSlotOutcome,
    DraftSaveOutcome,
    SubmitOutcome,
    compute_deadline,
    decide_advance_slot,
    decide_save_draft,
    decide_submit,
    draft_content_hash,
    is_content_reachable,
    resolve_released_slot,
    submit_request_hash,
)
from app.domain.exams.errors import NotEnrolledError
from app.domain.exams.models import (
    Attempt,
    AttemptStatus,
    Draft,
    ExamRevision,
    PublicQuestionRef,
    RunStatus,
    SubmitReceipt,
)
from app.domain.exams.repository import (
    AttemptRepository,
    DraftRepository,
    ExamRunRepository,
    OutboxRepository,
    new_outbox_entry,
)
from app.domain.exams.state_machine import require_attempt_transition

logger = logging.getLogger("hirelog.exams.student")


class ExamStudentService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.runs = ExamRunRepository(db)
        self.attempts = AttemptRepository(db)
        self.drafts = DraftRepository(db)
        self._outbox = OutboxRepository(db)

    # ── ownership / enrollment ────────────────────────────────────────────

    def _require_enrolled(self, run, uid: str) -> None:
        if uid not in run.enrolled_uids:
            raise NotEnrolledError(f"uid {uid} is not enrolled in run {run.id}")

    def get_run_or_404(self, run_id: str):
        run = self.runs.get(run_id)
        if run is None:
            raise LookupError(f"exam run {run_id} not found")
        return run

    def now(self):
        """The server's clock reading, sent to clients so their timer never trusts their own wall clock."""
        return self._clock.now()

    # ── state ──────────────────────────────────────────────────────────────

    def get_state(self, run_id: str, uid: str) -> Attempt:
        run = self.get_run_or_404(run_id)
        self._require_enrolled(run, uid)
        attempt = self.attempts.get(run_id, uid)
        if attempt is None:
            return Attempt(run_id=run_id, uid=uid, status=AttemptStatus.NOT_STARTED)
        return attempt

    # ── admission ──────────────────────────────────────────────────────────

    def admit(self, run_id: str, uid: str) -> Attempt:
        """NOT_STARTED -> ADMITTED -> IN_PROGRESS, computing the server-derived
        deadline once, here, from policy + accommodation. Idempotent: calling
        admit() again on an already-admitted attempt just returns it."""
        transaction = self._db.transaction()
        created: list[Attempt] = []

        @firestore.transactional
        def _txn(transaction) -> Attempt:
            created.clear()  # the callback re-runs when the transaction is retried
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            self._require_enrolled(run, uid)
            if run.status != RunStatus.RELEASED:
                raise PermissionError("run_not_released")

            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is not None and attempt.status != AttemptStatus.NOT_STARTED:
                return attempt  # already admitted — idempotent no-op

            # Rollback lever (contract #12, docs/adr/0016): blocks only this
            # brand-new NOT_STARTED -> ADMITTED transition. Checked AFTER the
            # idempotent-return above, so an already-admitted/in-progress
            # student replaying this call (page refresh, retry) is never
            # affected — "preserving safe completion" for anyone already in.
            if settings.EXAM_NEW_ADMISSIONS_PAUSED:
                raise PermissionError("new_admissions_paused")

            require_attempt_transition(AttemptStatus.NOT_STARTED, AttemptStatus.ADMITTED)
            deadline = compute_deadline(
                admitted_at=now,
                duration_minutes=run.policy.duration_minutes,
                accommodation_extra_minutes=run.policy.extra_minutes_for(uid),
            )
            new_attempt = Attempt(
                run_id=run_id,
                uid=uid,
                status=AttemptStatus.IN_PROGRESS,
                version=1,
                epoch=1,
                admitted_at=now,
                deadline=deadline,
                accommodation_extra_minutes=run.policy.extra_minutes_for(uid),
                updated_at=now,
            )
            self.attempts.create_in_transaction(transaction, new_attempt)
            created.append(new_attempt)
            return new_attempt

        attempt = _txn(transaction)
        if created:
            # A live attempt now has a deadline the sweeper must watch; it only wakes if this one is the earliest.
            sweep_hints.note_attempt_started(run_id, attempt.deadline)
        return attempt

    # ── question delivery (allowlisted DTO only) ──────────────────────────

    def get_current_question(self, run_id: str, uid: str, revision: ExamRevision) -> PublicQuestionRef:
        return self.get_question_for_slot(run_id, uid, revision, None)

    def _reachable_attempt(self, run_id: str, uid: str):
        run = self.get_run_or_404(run_id)
        self._require_enrolled(run, uid)
        attempt = self.attempts.get(run_id, uid)
        if attempt is None:
            raise NotEnrolledError("attempt not admitted yet")
        reason = is_content_reachable(run=run, attempt=attempt, now=self._clock.now())
        if reason is not None:
            raise PermissionError(reason)
        return run, attempt

    def get_question_for_slot(
        self, run_id: str, uid: str, revision: ExamRevision, slot_index: Optional[int]
    ) -> PublicQuestionRef:
        """`slot_index=None` is the current slot. Only slots the student has already been shown can be
        built: the current one always, earlier ones only when revisiting is enabled. Future slots never."""
        _, attempt = self._reachable_attempt(run_id, uid)
        current = attempt.current_slot_index
        index = current if slot_index is None else slot_index
        if index < 0 or index >= revision.slot_count:
            raise LookupError("question not found")
        if index > current:
            raise PermissionError("slot_not_released")
        if index < current and not settings.EXAM_STUDENT_REVISIT_RELEASED_SLOTS:
            raise PermissionError("slot_revisit_disabled")
        return PublicQuestionRef(
            question_id=f"{revision.id}:slot:{index}",
            slot_index=index,
            total_slots=revision.slot_count,
        )

    def list_released_slots(self, run_id: str, uid: str, revision: ExamRevision) -> dict:
        """Navigator data for released slots only - ids and answer state, never content."""
        _, attempt = self._reachable_attempt(run_id, uid)
        summaries = self.drafts.summaries(run_id, uid)
        slots = []
        for index in range(0, attempt.current_slot_index + 1):
            question_id = f"{revision.id}:slot:{index}"
            summary = summaries.get(question_id)
            slots.append({
                "slot_index": index,
                "question_id": question_id,
                "answered": bool(summary and summary.answered),
                "marked_for_review": bool(summary and summary.marked_for_review),
                "version": summary.version if summary else 0,
            })
        return {
            "current_slot_index": attempt.current_slot_index,
            "server_time": self._clock.now().isoformat(),
            "total_slots": revision.slot_count,
            "locked_count": max(0, revision.slot_count - (attempt.current_slot_index + 1)),
            "revisit_enabled": settings.EXAM_STUDENT_REVISIT_RELEASED_SLOTS,
            "slots": slots,
        }

    def authorize_released_question(self, run_id: str, uid: str, question_id: str, *, require_active: bool) -> int:
        """For routes that take a client-supplied question id (e.g. the public test run): returns the slot
        index only when the id belongs to this run's revision and the student has already been shown that
        slot. `require_active` also requires an attempt that can still be worked on."""
        run = self.get_run_or_404(run_id)
        self._require_enrolled(run, uid)
        attempt = self.attempts.get(run_id, uid)
        if attempt is None:
            raise NotEnrolledError("attempt not admitted yet")
        if require_active:
            reason = is_content_reachable(run=run, attempt=attempt, now=self._clock.now())
            if reason is not None:
                raise PermissionError(reason)
        resolution = resolve_released_slot(
            revision_id=run.revision_id, question_id=question_id, current_slot_index=attempt.current_slot_index
        )
        if resolution.conflict_reason == "question_not_in_run":
            raise LookupError("question not found")
        if resolution.conflict_reason is not None:
            raise PermissionError(resolution.conflict_reason)
        return resolution.slot_index

    def get_draft(self, run_id: str, uid: str, question_id: str) -> Optional[Draft]:
        """Own draft of a slot the student has been shown (also after sealing, for the receipt view)."""
        self.authorize_released_question(run_id, uid, question_id, require_active=False)
        return self.drafts.get(run_id, uid, question_id)

    def advance_to_next_question(self, run_id: str, uid: str, revision: ExamRevision) -> AdvanceSlotOutcome:
        """Forward-only slot advance — see `decide_advance_slot` docstring.
        Transactional so a double-click/two-tab race can't skip two slots."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> AdvanceSlotOutcome:
            now = self._clock.now()
            run = self.runs.get(run_id)  # only gates (paused/ended); same cached read as the save path
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            self._require_enrolled(run, uid)
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise NotEnrolledError("attempt not admitted yet")

            outcome = decide_advance_slot(attempt=attempt, run=run, total_slots=revision.slot_count, now=now)
            if outcome.accepted:
                self.attempts.update_in_transaction(
                    transaction, run_id, uid,
                    {"current_slot_index": outcome.new_slot_index, "version": attempt.version + 1, "updated_at": now.isoformat()},
                )
            return outcome

        return _txn(transaction)

    # ── drafts ─────────────────────────────────────────────────────────────

    def save_draft(
        self,
        run_id: str,
        uid: str,
        question_id: str,
        *,
        expected_version: int,
        answer: dict,
        marked_for_review: Optional[bool] = None,
        client_epoch: Optional[int] = None,
        client_save_id: Optional[str] = None,
    ) -> DraftSaveOutcome:
        if len(json.dumps(answer, separators=(",", ":"), default=str).encode("utf-8")) > settings.EXAM_DRAFT_MAX_BYTES:
            return DraftSaveOutcome(False, conflict_reason="answer_too_large")
        transaction = self._db.transaction()
        written: list[Draft] = []

        @firestore.transactional
        def _txn(transaction) -> DraftSaveOutcome:
            written.clear()  # the callback re-runs when the transaction is retried
            now = self._clock.now()
            # The cached run only gates (paused/ended); the save decides on the attempt and the draft read below.
            run = self.runs.get(run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            self._require_enrolled(run, uid)
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise NotEnrolledError("attempt not admitted yet")

            # A guessed or future question id must not create a draft document.
            resolution = resolve_released_slot(
                revision_id=run.revision_id, question_id=question_id, current_slot_index=attempt.current_slot_index
            )
            if resolution.conflict_reason is not None:
                return DraftSaveOutcome(False, conflict_reason=resolution.conflict_reason)

            existing = self.drafts.get_in_transaction(transaction, run_id, uid, question_id)
            current_version = existing.version if existing else 0
            marked = marked_for_review if marked_for_review is not None else bool(existing and existing.marked_for_review)
            content_hash = draft_content_hash(answer, marked)

            outcome = decide_save_draft(
                attempt=attempt,
                run=run,
                now=now,
                expected_version=expected_version,
                current_version=current_version,
                client_epoch=client_epoch,
                client_save_id=client_save_id,
                content_hash=content_hash,
                last_save_id=existing.last_save_id if existing else None,
                last_content_hash=existing.last_content_hash if existing else None,
            )
            if outcome.accepted and not outcome.replayed:
                draft = Draft(
                    question_id=question_id, version=outcome.new_version, answer=answer, updated_at=now,
                    marked_for_review=marked, last_save_id=client_save_id, last_content_hash=content_hash,
                )
                self.drafts.set_in_transaction(transaction, run_id, uid, draft)
                written.append(draft)
            return outcome

        outcome = _txn(transaction)
        if written and outcome.accepted and not outcome.replayed:
            self.drafts.record_saved(run_id, uid, written[-1])
        return outcome

    # ── final submit (the bounded transaction) ────────────────────────────

    def _submit_or_finish(
        self,
        run_id: str,
        uid: str,
        *,
        idempotency_key: str,
        client_epoch: int,
        expected_draft_versions: Optional[dict[str, int]],
    ) -> SubmitOutcome:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> SubmitOutcome:
            # Fresh server acceptance time on EVERY transaction retry —
            # `now` is read here, inside the callback, not by the caller.
            now = self._clock.now()
            run = self.runs.get_in_transaction(transaction, run_id)
            if run is None:
                raise LookupError(f"exam run {run_id} not found")
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise NotEnrolledError("attempt not admitted yet")

            current_versions = self.drafts.all_versions_in_transaction(transaction, run_id, uid)
            # Read inside this transaction: a draft saved (or first created) concurrently makes the
            # commit fail and this callback re-run against the new state, so the receipt always
            # lists exactly the versions that exist at the instant of sealing.
            expected = expected_draft_versions if expected_draft_versions is not None else current_versions

            outcome = decide_submit(
                attempt=attempt,
                run=run,
                now=now,
                idempotency_key=idempotency_key,
                client_epoch=client_epoch,
                expected_draft_versions=expected,
                current_draft_versions=current_versions,
                request_hash=submit_request_hash("finish" if expected_draft_versions is None else "submit", expected_draft_versions),
            )

            if outcome.accepted and not outcome.reused_existing_receipt:
                require_attempt_transition(attempt.status, AttemptStatus.SUBMITTED)
                update = {
                    "status": AttemptStatus.SUBMITTED.value,
                    "version": attempt.version + 1,
                    "submit_receipt": outcome.receipt.model_dump(mode="json"),
                    "updated_at": now.isoformat(),
                }
                self.attempts.update_in_transaction(transaction, run_id, uid, update)
                entry = new_outbox_entry(
                    kind="attempt_finalized",
                    run_id=run_id,
                    payload={"uid": uid, "receipt_id": outcome.receipt.receipt_id},
                    now=now,
                )
                self._outbox.add_in_transaction(transaction, entry)

            return outcome

        outcome = _txn(transaction)
        if outcome.accepted and not outcome.reused_existing_receipt:
            background.wake("outbox")  # the grading handler consumes the attempt_finalized entry
        return outcome

    def submit(
        self, run_id: str, uid: str, *, idempotency_key: str, client_epoch: int, expected_draft_versions: dict[str, int]
    ) -> SubmitOutcome:
        """Explicit submit — caller states the draft versions it believes are
        current; a mismatch is an explicit conflict, never a silent overwrite."""
        return self._submit_or_finish(
            run_id, uid, idempotency_key=idempotency_key, client_epoch=client_epoch,
            expected_draft_versions=expected_draft_versions,
        )

    def finish(self, run_id: str, uid: str, *, idempotency_key: str, client_epoch: int) -> SubmitOutcome:
        """"I'm done" — seal whatever the server currently holds, with no
        client-declared expectation (used for the "close/expiry seals last
        acknowledged draft" path and a plain student-initiated finish)."""
        return self._submit_or_finish(
            run_id, uid, idempotency_key=idempotency_key, client_epoch=client_epoch,
            expected_draft_versions=None,
        )

    # ── receipt reconciliation ─────────────────────────────────────────────

    def get_receipt(self, run_id: str, uid: str) -> Optional[SubmitReceipt]:
        """Read-only lookup. Works even after the deadline has passed — it
        never authorizes new work, it only returns what was already sealed."""
        run = self.get_run_or_404(run_id)
        self._require_enrolled(run, uid)
        attempt = self.attempts.get(run_id, uid)
        if attempt is None:
            return None
        return attempt.submit_receipt
