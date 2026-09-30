"""Outbox handler wiring judge/MCQ/short-answer grading to attempt
finalization (Prompt 3/4's outbox, extended exactly at the seam ADR 0008
reserved — no changes to the outbox drain loop itself).
"""

from __future__ import annotations

import logging

from app.domain.exams.models import OutboxEntry
from app.domain.exams.repository import DraftRepository
from app.domain.generation.models import CandidateStatus, ItemType
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.judge.converters import to_test_groups
from app.domain.judge.service import JudgeService

logger = logging.getLogger("hirelog.judge.outbox")


def _parse_slot_index(question_id: str) -> int:
    # question_id format from student_service.py: f"{revision_id}:slot:{index}"
    return int(question_id.rsplit(":slot:", 1)[1])


class GradingTriggerOutboxHandler:
    """Dispatches on `entry.kind == "attempt_finalized"`. Any other kind is a
    no-op here — a `CompositeOutboxHandler` (see `outbox.py` docstring
    pattern) is expected to route other kinds elsewhere."""

    def __init__(self, db, judge_provider, allowlist) -> None:
        self._db = db
        self._judge = JudgeService(db, judge_provider)
        self._allowlist = allowlist
        self._generation = GenerationRepository(db)
        self._assignments = VariantAssignmentRepository(db)
        self._drafts = DraftRepository(db)

    def handle(self, entry: OutboxEntry) -> None:
        if entry.kind != "attempt_finalized":
            return

        run_id = entry.run_id
        uid = str(entry.payload.get("uid", ""))
        if not uid:
            return

        for question_id in self._drafts.all_versions(run_id, uid):
            self._grade_one_question(run_id, uid, question_id)

    def _grade_one_question(self, run_id: str, uid: str, question_id: str) -> None:
        slot_index = _parse_slot_index(question_id)
        candidate_id = self._assignments.get_assignment(run_id=run_id, uid=uid, slot_index=slot_index)
        if not candidate_id:
            logger.warning("no variant assignment for run=%s uid=%s question=%s — skipping grading", run_id, uid, question_id)
            return

        candidate = self._generation.get_candidate(candidate_id)
        if candidate is None or candidate.status not in (CandidateStatus.APPROVED, CandidateStatus.PUBLISHED):
            logger.warning("candidate %s not approved/published — skipping grading", candidate_id)
            return

        draft = self._drafts.get(run_id, uid, question_id)
        answer = draft.answer if draft else None
        if not answer:
            return  # blank — nothing to grade (counted as "blank" by the gradebook, Prompt 9)

        if candidate.item_type == ItemType.CODING:
            language_key = str(answer.get("language", ""))
            source_code = str(answer.get("source_code", ""))
            if not language_key or not source_code:
                return
            all_groups = to_test_groups(candidate.public_tests) + to_test_groups(candidate.hidden_test_groups)
            self._judge.submit_final_grade(
                candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
                language_key=language_key, source_code=source_code, test_suite_version=1,
                all_test_groups=all_groups, allowlist=self._allowlist,
            )
        elif candidate.item_type in (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT):
            student_answer = str(answer.get("selected", ""))
            self._judge.grade_mcq(
                candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
                student_answer=student_answer, expected_answer=str(candidate.private_expected_answer or ""),
            )
        elif candidate.item_type == ItemType.SHORT_ANSWER:
            student_answer = str(answer.get("text", ""))
            self._judge.create_short_answer_review(
                candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
                student_answer=student_answer, max_score=candidate.marks,
            )
