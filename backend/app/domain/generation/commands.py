"""Thin transactional wiring for candidate review and variant reservation for
a run. See `review.py`/`variants.py` for the pure decision logic.
"""

from __future__ import annotations

from app.domain.exams.clock import Clock, system_clock
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.generation.review import ReviewOutcome, decide_review_candidate
from app.domain.generation.variants import VariantReservationResult, reserve_variants


class GenerationCommandService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.repo = GenerationRepository(db)
        self.assignments = VariantAssignmentRepository(db)

    def review_candidate(self, candidate_id: str, *, decision, reviewer: str, rejection_reason: str | None = None) -> ReviewOutcome:
        candidate = self.repo.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError(f"candidate {candidate_id} not found")
        now = self._clock.now()
        outcome = decide_review_candidate(
            candidate, decision=decision, reviewer=reviewer, now=now, rejection_reason=rejection_reason
        )
        if outcome.accepted:
            self.repo.save_candidate(outcome.candidate)
        return outcome

    def reserve_variants_for_slot(
        self, *, run_id: str, blueprint_id: str, slot_index: int, uids: list[str], allow_balanced_reuse: bool
    ) -> VariantReservationResult:
        approved = self.repo.approved_candidates_for_slot(blueprint_id, slot_index)
        result = reserve_variants(
            uids=uids,
            approved_candidate_ids=[c.id for c in approved],
            allow_balanced_reuse=allow_balanced_reuse,
        )
        if not result.blocked:
            now = self._clock.now()
            for uid, candidate_id in result.assignments.items():
                self.assignments.save_assignment(
                    run_id=run_id, uid=uid, slot_index=slot_index, candidate_id=candidate_id, now=now
                )
        return result
