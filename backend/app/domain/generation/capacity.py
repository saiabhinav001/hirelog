"""Generation capacity: how many variants were requested, generated, valid and approved per
slot, computed from the stored candidates (never from what a provider claims) - Prompt 5,
ADR 0021. Publication uses it: a slot without enough APPROVED variants blocks publishing
unless the teacher has explicitly accepted the shortfall on the blueprint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.domain.generation.models import CandidateStatus, ExamBlueprint, GenerationCandidate, GenerationRequest

VALID_STATUSES = frozenset({CandidateStatus.READY_FOR_HUMAN_REVIEW, CandidateStatus.APPROVED, CandidateStatus.PUBLISHED})
APPROVED_STATUSES = frozenset({CandidateStatus.APPROVED, CandidateStatus.PUBLISHED})


@dataclass
class SlotCapacity:
    slot_index: int
    item_type: str
    requested: int
    generated: int = 0  # every current (non-superseded) candidate, whatever its state
    valid: int = 0  # passed validation: awaiting review, approved or published
    approved: int = 0
    pending_review: int = 0
    needs_revision: int = 0
    rejected: int = 0
    failed_requests: int = 0
    last_error: str | None = None

    @property
    def shortfall(self) -> int:
        return max(0, self.requested - self.approved)

    def to_dict(self) -> dict:
        return {**self.__dict__, "shortfall": self.shortfall}


@dataclass
class BlueprintCapacity:
    blueprint_id: str
    slots: list[SlotCapacity] = field(default_factory=list)
    shortfall_accepted: bool = False

    @property
    def totals(self) -> dict[str, int]:
        keys = ("requested", "generated", "valid", "approved", "pending_review", "needs_revision", "rejected")
        return {k: sum(getattr(s, k) for s in self.slots) for k in keys} | {"shortfall": sum(s.shortfall for s in self.slots)}

    @property
    def empty_slots(self) -> list[int]:
        return [s.slot_index for s in self.slots if s.approved == 0]

    @property
    def short_slots(self) -> list[int]:
        return [s.slot_index for s in self.slots if 0 < s.approved < s.requested]

    @property
    def meets_requirement(self) -> bool:
        return all(s.approved >= s.requested for s in self.slots)

    @property
    def publishable(self) -> bool:
        """Every slot has at least one approved variant, and either the full requested pool or an accepted shortfall."""
        return not self.empty_slots and (self.meets_requirement or self.shortfall_accepted)

    def to_dict(self) -> dict:
        return {
            "blueprint_id": self.blueprint_id, "slots": [s.to_dict() for s in self.slots], "totals": self.totals,
            "empty_slots": self.empty_slots, "short_slots": self.short_slots,
            "meets_requirement": self.meets_requirement, "shortfall_accepted": self.shortfall_accepted,
            "publishable": self.publishable,
        }


def compute_capacity(
    blueprint: ExamBlueprint, candidates: Iterable[GenerationCandidate], requests: Iterable[GenerationRequest] = ()
) -> BlueprintCapacity:
    by_slot = {
        slot.slot_index: SlotCapacity(slot_index=slot.slot_index, item_type=slot.item_type.value, requested=slot.requested_variant_count)
        for slot in blueprint.slots
    }
    for candidate in candidates:
        cap = by_slot.get(candidate.slot_index)
        if cap is None or candidate.status == CandidateStatus.SUPERSEDED:
            continue
        cap.generated += 1
        if candidate.status in VALID_STATUSES:
            cap.valid += 1
        if candidate.status in APPROVED_STATUSES:
            cap.approved += 1
        elif candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW:
            cap.pending_review += 1
        elif candidate.status in (CandidateStatus.NEEDS_REVISION, CandidateStatus.DRAFT, CandidateStatus.INSUFFICIENT_SOURCE):
            cap.needs_revision += 1
        elif candidate.status == CandidateStatus.REJECTED:
            cap.rejected += 1
    for request in requests:
        cap = by_slot.get(request.slot_index)
        if cap is not None and request.status == "failed":
            cap.failed_requests += 1
            cap.last_error = request.last_error
    return BlueprintCapacity(
        blueprint_id=blueprint.id, slots=[by_slot[i] for i in sorted(by_slot)], shortfall_accepted=blueprint.shortfall_accepted
    )
