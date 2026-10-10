"""Transactional wiring for candidate review, edit, publication and variant assignment
(Prompt 5, ADR 0021). The decisions themselves are pure (`review.py`, `edit.py`,
`variants.py`); this module reads fresh state inside a transaction, decides, writes the
change and its audit event together.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from firebase_admin import firestore

from app.core.config import settings
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.models import ExamRevision, ExamRun
from app.domain.exams.repository import ExamDefinitionRepository
from app.domain.generation.edit import CandidateEditError, EditOutcome, decide_edit
from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.generation.review import ReviewOutcome, decide_review_candidate
from app.domain.generation.variants import (
    VariantReservationResult,
    reserve_variants,
    seeded_candidate_order,
    seeded_option_order,
)

logger = logging.getLogger("hirelog.exams.generation")

SERVABLE = (CandidateStatus.APPROVED, CandidateStatus.PUBLISHED)


@dataclass
class AssignmentReport:
    created: int = 0
    already_assigned: int = 0
    blocked_slots: dict[int, str] = field(default_factory=dict)  # slot_index -> reason (nothing assigned for it)

    def to_dict(self) -> dict:
        return {"created": self.created, "already_assigned": self.already_assigned, "blocked_slots": self.blocked_slots}


class GenerationCommandService:
    def __init__(self, db, clock: Clock = system_clock) -> None:
        self._db = db
        self._clock = clock
        self.repo = GenerationRepository(db)
        self.assignments = VariantAssignmentRepository(db)
        self._audit = AuthzAuditRepository(db)
        self._definitions = ExamDefinitionRepository(db)

    def _audit_in_txn(self, transaction, kind: str, *, actor: str, detail: dict, now) -> None:
        self._audit.append_in_transaction(
            transaction, build_audit_event(kind, actor_uid=actor, detail={k: v for k, v in detail.items() if v is not None}, now=now)
        )

    # -- review (approve / reject) ---------------------------------------------------------

    def review_candidate(self, candidate_id: str, *, decision, reviewer: str, rejection_reason: str | None = None) -> ReviewOutcome:
        """Compare-and-set in a transaction, so a review cannot race an edit or another review."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> ReviewOutcome:
            candidate = self.repo.get_candidate_in_transaction(transaction, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            now = self._clock.now()
            outcome = decide_review_candidate(
                candidate, decision=decision, reviewer=reviewer, now=now, rejection_reason=rejection_reason,
                require_execution_verified=settings.EXAM_REQUIRE_EXECUTED_CODING_VERIFICATION,
            )
            if outcome.accepted:
                self.repo.set_candidate_in_transaction(transaction, outcome.candidate)
                self._audit_in_txn(
                    transaction, "exam_candidate_reviewed", actor=reviewer, now=now,
                    detail={"candidate_id": candidate_id, "decision": decision, "from_status": candidate.status.value,
                            "to_status": outcome.candidate.status.value, "version": candidate.version, "reason": rejection_reason},
                )
            return outcome

        return _txn(transaction)

    # -- edit ---------------------------------------------------------------------------------

    def candidate_in_published_revision(self, candidate: GenerationCandidate) -> Optional[ExamRevision]:
        blueprint = self.repo.get_blueprint(candidate.blueprint_id)
        if blueprint is None:
            return None
        for revision in self._definitions.list_revisions(blueprint.exam_definition_id):
            if any(candidate.id in (slot.approved_candidate_ids or []) for slot in (revision.slots or [])):
                return revision
        return None

    def edit_candidate(self, candidate_id: str, changes: Mapping[str, Any], *, editor: str) -> EditOutcome:
        candidate = self.repo.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError(f"candidate {candidate_id} not found")
        request = self.repo.get_request(candidate.request_id)
        if request is None:
            raise LookupError(f"generation request {candidate.request_id} not found")
        revision = self.candidate_in_published_revision(candidate)
        if revision is not None:
            raise CandidateEditError(
                "candidate_in_published_revision",
                f"this question is part of published revision {revision.revision_number}; regenerate or edit a new one instead",
            )
        used = self.repo.previously_used_signatures(candidate.blueprint_id)
        prompts = self.repo.live_prompts_for_slot(candidate.blueprint_id, candidate.slot_index)
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> EditOutcome:
            current = self.repo.get_candidate_in_transaction(transaction, candidate_id)
            if current is None:
                raise LookupError(f"candidate {candidate_id} not found")
            now = self._clock.now()
            outcome = decide_edit(
                current, changes, editor=editor, now=now, request=request,
                previously_used_signatures=used, live_prompts=prompts,
            )
            if outcome.kind == "minor":
                self.repo.set_candidate_in_transaction(transaction, outcome.candidate)
            elif outcome.kind == "new_version":
                self.repo.set_candidate_in_transaction(transaction, outcome.superseded)
                self.repo.create_candidate_in_transaction(transaction, outcome.candidate)
            if outcome.kind != "unchanged":
                self._audit_in_txn(
                    transaction, "exam_candidate_edited", actor=editor, now=now,
                    detail={
                        "candidate_id": candidate_id, "new_candidate_id": outcome.candidate.id, "kind": outcome.kind,
                        "from_status": current.status.value, "to_status": outcome.candidate.status.value,
                        "fields": sorted(changes), "version": outcome.candidate.version,
                    },
                )
            return outcome

        return _txn(transaction)

    # -- blueprint policy -------------------------------------------------------------------

    def set_shortfall_acceptance(self, blueprint_id: str, *, accepted: bool, actor: str, reason: Optional[str]) -> None:
        """Explicit, audited acceptance of fewer approved variants than requested (publication gate)."""
        now = self._clock.now()
        self.repo.update_blueprint(blueprint_id, {
            "shortfall_accepted": accepted,
            "shortfall_accepted_by": actor if accepted else None,
            "shortfall_accepted_at": now.isoformat() if accepted else None,
            "shortfall_reason": reason if accepted else None,
            "updated_at": now.isoformat(),
        })
        self._audit.append(build_audit_event(
            "exam_shortfall_accepted" if accepted else "exam_shortfall_withdrawn", actor_uid=actor,
            detail={"blueprint_id": blueprint_id, "reason": reason} if reason else {"blueprint_id": blueprint_id}, now=now,
        ))

    # -- publication ------------------------------------------------------------------------

    def mark_published(self, candidate_ids: list[str], revision_id: str) -> int:
        """approved -> published for the candidates frozen into a revision. Idempotent; a crash midway
        leaves some candidates `approved`, which is still servable and gradable."""
        changed = 0
        for candidate_id in candidate_ids:
            transaction = self._db.transaction()

            @firestore.transactional
            def _txn(transaction, candidate_id=candidate_id) -> bool:
                candidate = self.repo.get_candidate_in_transaction(transaction, candidate_id)
                if candidate is None or candidate.status != CandidateStatus.APPROVED:
                    return False
                now = self._clock.now()
                self.repo.set_candidate_in_transaction(transaction, candidate.model_copy(update={
                    "status": CandidateStatus.PUBLISHED, "published_at": now, "published_revision_id": revision_id,
                }))
                return True

            changed += int(_txn(transaction))
        return changed

    # -- variant assignment ---------------------------------------------------------------------

    def _option_order(self, candidate: Optional[GenerationCandidate], run_id: str, uid: str, slot_index: int) -> Optional[list[str]]:
        if candidate is None or candidate.item_type not in (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT):
            return None
        keys = [str(o.get("key")) for o in candidate.options if isinstance(o, dict) and o.get("key") is not None]
        return seeded_option_order(run_id, uid, slot_index, keys) if len(keys) >= 2 else None

    def reserve_variants_for_slot(
        self, *, run_id: str, blueprint_id: str, slot_index: int, uids: list[str], allow_balanced_reuse: bool
    ) -> VariantReservationResult:
        """Legacy per-slot reservation from the LIVE approved pool. Assignments are create-only."""
        blueprint = self.repo.get_blueprint(blueprint_id)
        approved = self.repo.approved_candidates_for_slot(blueprint_id, slot_index)
        result = reserve_variants(
            uids=uids, approved_candidate_ids=[c.id for c in approved], allow_balanced_reuse=allow_balanced_reuse,
        )
        if not result.blocked:
            now = self._clock.now()
            by_id = {c.id: c for c in approved}
            final: dict[str, str] = {}
            for uid, candidate_id in result.assignments.items():
                order = self._option_order(by_id.get(candidate_id), run_id, uid, slot_index) if blueprint and blueprint.shuffle_options else None
                self.assignments.save_assignment(
                    run_id=run_id, uid=uid, slot_index=slot_index, candidate_id=candidate_id, now=now, option_order=order
                )
                final[uid] = self.assignments.get_assignment(run_id=run_id, uid=uid, slot_index=slot_index) or candidate_id
            result = VariantReservationResult(assignments=final, blocked=False)
        return result

    def assign_run_variants(
        self, *, run: ExamRun, revision: ExamRevision, uids: list[str], allow_balanced_reuse: bool, shuffle_options: bool, actor: str
    ) -> AssignmentReport:
        """Assigns every enrolled student a variant per slot from the candidates PINNED in the run's
        revision (not the live pool), before anyone starts. Create-only, so re-running only fills gaps
        and never moves a student who already has a question."""
        report = AssignmentReport()
        now = self._clock.now()
        cache: dict[str, Optional[GenerationCandidate]] = {}

        def candidate_for(candidate_id: str) -> Optional[GenerationCandidate]:
            if candidate_id not in cache:
                cache[candidate_id] = self.repo.get_candidate(candidate_id)
            return cache[candidate_id]

        for slot in sorted(revision.slots or [], key=lambda s: s.slot_index):
            existing = {a["uid"]: a for a in self.assignments.list_for_run_slot(run.id, slot.slot_index)}
            report.already_assigned += sum(1 for u in uids if u in existing)
            missing = [u for u in sorted(set(uids)) if u not in existing]
            if not missing:
                continue
            pool = [
                cid for cid in slot.approved_candidate_ids
                if (c := candidate_for(cid)) is not None and c.status in SERVABLE
            ]
            if not pool:
                report.blocked_slots[slot.slot_index] = "no approved candidates are available for this slot"
                continue
            ordered = seeded_candidate_order(run.id, slot.slot_index, pool)
            used = {a["candidate_id"] for a in existing.values()}
            unused = [c for c in ordered if c not in used]
            if len(unused) >= len(missing):
                picks = {uid: unused[i] for i, uid in enumerate(missing)}
            elif allow_balanced_reuse:
                offset = len(existing)
                picks = {uid: ordered[(offset + i) % len(ordered)] for i, uid in enumerate(missing)}
            else:
                report.blocked_slots[slot.slot_index] = (
                    f"insufficient verified pool: need {len(missing)} unique variant(s), have {len(unused)} unused approved"
                )
                continue
            for uid, candidate_id in picks.items():
                order = self._option_order(candidate_for(candidate_id), run.id, uid, slot.slot_index) if shuffle_options else None
                if self.assignments.save_assignment(
                    run_id=run.id, uid=uid, slot_index=slot.slot_index, candidate_id=candidate_id, now=now, option_order=order
                ):
                    report.created += 1
                else:
                    report.already_assigned += 1  # another request got there first

        self._audit.append(build_audit_event(
            "exam_variants_assigned", actor_uid=actor, institution_id=run.institution_id,
            detail={"run_id": run.id, "revision_id": revision.id, "created": report.created,
                    "already_assigned": report.already_assigned, "blocked_slots": sorted(report.blocked_slots)}, now=now,
        ))
        return report
