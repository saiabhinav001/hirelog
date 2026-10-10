"""Student-facing exam service — scoped state/question/draft/submit/finish and
receipt reconciliation (Prompt 4). Thin transactional wiring only; all
conflict/idempotency/deadline logic lives in `decisions.py`.
"""

from __future__ import annotations

import logging
from typing import Optional

from firebase_admin import firestore

from app.core.config import settings
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.decisions import (
    AdvanceSlotOutcome,
    DraftSaveOutcome,
    SubmitOutcome,
    compute_deadline,
    decide_advance_slot,
    decide_save_draft,
    decide_submit,
    is_content_reachable,
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

    def get_run_cached_or_404(self, run_id: str):
        run = self.runs.get_cached(run_id)
        if run is None:
            raise LookupError(f"exam run {run_id} not found")
        return run

    # ── state ──────────────────────────────────────────────────────────────

    def get_state(self, run_id: str, uid: str) -> Attempt:
        run = self.get_run_cached_or_404(run_id)
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

        @firestore.transactional
        def _txn(transaction) -> Attempt:
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
            return new_attempt

        return _txn(transaction)

    # ── question delivery (allowlisted DTO only) ──────────────────────────

    def get_current_question(self, run_id: str, uid: str, revision: ExamRevision) -> PublicQuestionRef:
        run = self.get_run_cached_or_404(run_id)
        self._require_enrolled(run, uid)
        attempt = self.attempts.get(run_id, uid)
        if attempt is None:
            raise NotEnrolledError("attempt not admitted yet")

        now = self._clock.now()
        reason = is_content_reachable(run=run, attempt=attempt, now=now)
        if reason is not None:
            raise PermissionError(reason)

        # Only the CURRENT slot is ever constructed/returned — no prefetching
        # of future slots, no locked content.
        index = attempt.current_slot_index
        question_id = f"{revision.id}:slot:{index}"
        return PublicQuestionRef(
            question_id=question_id,
            slot_index=index,
            total_slots=revision.slot_count,
        )

    def advance_to_next_question(self, run_id: str, uid: str, revision: ExamRevision) -> AdvanceSlotOutcome:
        """Forward-only slot advance — see `decide_advance_slot` docstring.
        Transactional so a double-click/two-tab race can't skip two slots."""
        run = self.get_run_cached_or_404(run_id)  # status only; the attempt is read transactionally below
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> AdvanceSlotOutcome:
            now = self._clock.now()
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
        self, run_id: str, uid: str, question_id: str, *, expected_version: int, answer: dict
    ) -> DraftSaveOutcome:
        run = self.get_run_cached_or_404(run_id)  # status/policy only; attempt + draft are read transactionally
        self._require_enrolled(run, uid)
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> DraftSaveOutcome:
            now = self._clock.now()
            attempt = self.attempts.get_in_transaction(transaction, run_id, uid)
            if attempt is None:
                raise NotEnrolledError("attempt not admitted yet")

            existing = self.drafts.get_in_transaction(transaction, run_id, uid, question_id)
            current_version = existing.version if existing else 0

            outcome = decide_save_draft(
                attempt=attempt,
                run=run,
                now=now,
                expected_version=expected_version,
                current_version=current_version,
            )
            if outcome.accepted:
                draft = Draft(question_id=question_id, version=outcome.new_version, answer=answer, updated_at=now)
                self.drafts.set_in_transaction(transaction, run_id, uid, draft)
            return outcome

        return _txn(transaction)

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

            current_versions = self.drafts.all_versions(run_id, uid)
            # NOTE: this is a plain (non-transactional) collection read, not
            # part of this transaction's snapshot — see docs/adr/0008 "Known
            # limitations". A concurrent draft-save landing between this read
            # and the transaction's commit is a real race this phase does not
            # fully close; only a live Firestore transaction/emulator run can
            # validate the exact interleaving, per contract ("Validate
            # transaction races in real staging Firestore before claiming
            # production correctness").
            expected = expected_draft_versions if expected_draft_versions is not None else current_versions

            outcome = decide_submit(
                attempt=attempt,
                run=run,
                now=now,
                idempotency_key=idempotency_key,
                client_epoch=client_epoch,
                expected_draft_versions=expected,
                current_draft_versions=current_versions,
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

        return _txn(transaction)

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
        run = self.get_run_cached_or_404(run_id)
        self._require_enrolled(run, uid)
        attempt = self.attempts.get(run_id, uid)
        if attempt is None:
            return None
        return attempt.submit_receipt
