"""Publication preview and validation (Prompt 3, ADR 0019).

Pure functions: given a blueprint and its candidates, compute the resolved
counts / total marks a teacher must see BEFORE publishing, plus the blockers
that make publishing unsafe and the warnings that merely deserve a look.
No I/O, no clock — the route/command layer loads the inputs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.domain.exams.models import RevisionSlot
from app.domain.generation.models import CandidateStatus, ExamBlueprint, GenerationCandidate

MAX_PUBLISHED_SLOTS = 500
KNOWN_DIFFICULTIES = ("easy", "medium", "hard")


@dataclass
class SlotPreview:
    slot_index: int
    item_type: str
    difficulty: str
    marks: float
    negative_marks: float
    target_minutes: int
    requested_variant_count: int
    approved_count: int
    objective_ids: list[str]
    permitted_languages: list[str]


@dataclass
class PublicationPreview:
    blueprint_id: str
    slot_count: int
    total_marks: float
    max_negative_marks: float
    total_target_minutes: int
    counts_by_type: dict[str, int]
    marks_by_type: dict[str, float]
    difficulty_distribution: dict[str, int]
    languages: list[str]
    objective_ids: list[str]
    slots: list[SlotPreview] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "blueprint_id": self.blueprint_id,
            "slot_count": self.slot_count,
            "total_marks": self.total_marks,
            "max_negative_marks": self.max_negative_marks,
            "total_target_minutes": self.total_target_minutes,
            "counts_by_type": self.counts_by_type,
            "marks_by_type": self.marks_by_type,
            "difficulty_distribution": self.difficulty_distribution,
            "languages": self.languages,
            "objective_ids": self.objective_ids,
            "slots": [s.__dict__ for s in self.slots],
            "blockers": self.blockers,
            "warnings": self.warnings,
            "publishable": self.publishable,
        }


def _round_marks(value: float) -> float:
    return round(float(value), 6)


def group_approved_by_slot(candidates: Iterable[GenerationCandidate]) -> dict[int, list[GenerationCandidate]]:
    grouped: dict[int, list[GenerationCandidate]] = {}
    for candidate in candidates:
        if candidate.status == CandidateStatus.APPROVED:
            grouped.setdefault(candidate.slot_index, []).append(candidate)
    for items in grouped.values():
        items.sort(key=lambda c: c.id)  # deterministic order for the snapshot
    return grouped


def build_preview(
    blueprint: ExamBlueprint,
    approved_by_slot: dict[int, list[GenerationCandidate]],
    *,
    duration_minutes: Optional[int] = None,
    negative_marking_enabled: bool = False,
) -> PublicationPreview:
    blockers: list[str] = []
    warnings: list[str] = []
    slots = sorted(blueprint.slots, key=lambda s: s.slot_index)

    indexes = [s.slot_index for s in slots]
    if len(slots) > MAX_PUBLISHED_SLOTS:
        blockers.append(f"a revision supports at most {MAX_PUBLISHED_SLOTS} slots (blueprint has {len(slots)})")
    if len(set(indexes)) != len(indexes):
        duplicated = sorted(i for i, n in Counter(indexes).items() if n > 1)
        blockers.append(f"duplicate slot indexes: {duplicated}")
    elif indexes != list(range(len(slots))):
        blockers.append("slot indexes must be contiguous starting at 0 (question ids are positional)")

    slot_previews: list[SlotPreview] = []
    type_counts: Counter[str] = Counter()
    type_marks: Counter[str] = Counter()
    difficulty_counts: Counter[str] = Counter()
    languages: set[str] = set()
    objectives: set[str] = set()
    total_marks = 0.0
    total_negative = 0.0
    total_minutes = 0

    for slot in slots:
        approved = approved_by_slot.get(slot.slot_index, [])
        item_type = slot.item_type.value
        negative = float(getattr(slot, "negative_marks", 0.0) or 0.0)
        slot_previews.append(
            SlotPreview(
                slot_index=slot.slot_index, item_type=item_type, difficulty=slot.difficulty,
                marks=_round_marks(slot.marks), negative_marks=_round_marks(negative),
                target_minutes=slot.target_minutes, requested_variant_count=slot.requested_variant_count,
                approved_count=len(approved), objective_ids=list(slot.objective_ids),
                permitted_languages=list(slot.permitted_languages),
            )
        )
        type_counts[item_type] += 1
        type_marks[item_type] += slot.marks
        difficulty_counts[slot.difficulty] += 1
        languages.update(slot.permitted_languages)
        objectives.update(slot.objective_ids)
        total_marks += slot.marks
        total_negative += negative
        total_minutes += slot.target_minutes

        label = f"slot {slot.slot_index}"
        if not approved:
            blockers.append(f"{label}: no approved candidate (nothing can be assigned to students)")
        elif len(approved) < slot.requested_variant_count:
            warnings.append(
                f"{label}: {len(approved)} approved of {slot.requested_variant_count} requested variants"
            )
        if negative > slot.marks:
            blockers.append(f"{label}: negative_marks ({negative}) exceed marks ({slot.marks})")
        if negative > 0 and not negative_marking_enabled:
            blockers.append(
                f"{label}: negative marking is not enabled on this deployment "
                "(grading does not apply it until the scoring semantics are decided)"
            )
        if item_type == "coding" and not slot.permitted_languages:
            warnings.append(f"{label}: coding slot has no permitted languages")
        if slot.difficulty.strip().lower() not in KNOWN_DIFFICULTIES:
            warnings.append(f"{label}: unrecognised difficulty {slot.difficulty!r}")

    if duration_minutes is not None and total_minutes > duration_minutes:
        warnings.append(
            f"sum of slot target minutes ({total_minutes}) exceeds the exam duration ({duration_minutes})"
        )

    return PublicationPreview(
        blueprint_id=blueprint.id,
        slot_count=len(slots),
        total_marks=_round_marks(total_marks),
        max_negative_marks=_round_marks(total_negative),
        total_target_minutes=total_minutes,
        counts_by_type=dict(sorted(type_counts.items())),
        marks_by_type={k: _round_marks(v) for k, v in sorted(type_marks.items())},
        difficulty_distribution=dict(sorted(difficulty_counts.items())),
        languages=sorted(languages),
        objective_ids=sorted(objectives),
        slots=slot_previews,
        blockers=blockers,
        warnings=warnings,
    )


def snapshot_slots(
    blueprint: ExamBlueprint, approved_by_slot: dict[int, list[GenerationCandidate]]
) -> list[RevisionSlot]:
    """Freeze the blueprint slots + the approved candidate ids into the revision."""
    snapshot: list[RevisionSlot] = []
    for slot in sorted(blueprint.slots, key=lambda s: s.slot_index):
        approved = approved_by_slot.get(slot.slot_index, [])
        refs = sorted(
            {
                f"{a.source_id}:{a.source_version}:{a.anchor_id}"
                for candidate in approved
                for a in candidate.supporting_anchors
            }
        )
        snapshot.append(
            RevisionSlot(
                slot_index=slot.slot_index,
                item_type=slot.item_type.value,
                difficulty=slot.difficulty,
                marks=slot.marks,
                negative_marks=float(getattr(slot, "negative_marks", 0.0) or 0.0),
                target_minutes=slot.target_minutes,
                objective_ids=list(slot.objective_ids),
                permitted_languages=list(slot.permitted_languages),
                requested_variant_count=slot.requested_variant_count,
                approved_candidate_ids=[c.id for c in approved],
                source_refs=refs,
            )
        )
    return snapshot
