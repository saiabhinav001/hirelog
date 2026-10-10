"""Blueprint specification (Prompt 5, ADR 0021): a compact way for a teacher to say what an
exam should contain - question count, item types, marks, difficulty distribution, topics,
languages, variants - that the server expands deterministically into `BlueprintSlot`s.

The expansion is pure and validated: counts must add up, percentages must total 100, coding
needs languages. A teacher who wants finer control still posts `slots` directly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.domain.generation.models import BlueprintSlot, ItemType

TYPE_ORDER = (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT, ItemType.SHORT_ANSWER, ItemType.CODING)
DEFAULT_MINUTES = {ItemType.SINGLE_CHOICE: 2, ItemType.MULTIPLE_SELECT: 3, ItemType.SHORT_ANSWER: 6, ItemType.CODING: 20}
DIFFICULTY_ORDER = ("easy", "medium", "hard")
MAX_SPEC_QUESTIONS = 200


class BlueprintSpec(BaseModel):
    question_count: int = Field(ge=1, le=MAX_SPEC_QUESTIONS)
    type_counts: dict[ItemType, int]
    marks_per_type: dict[ItemType, float] = Field(default_factory=dict)
    minutes_per_type: dict[ItemType, int] = Field(default_factory=dict)
    negative_marks_per_type: dict[ItemType, float] = Field(default_factory=dict)
    # Percentages by difficulty label; must total 100 (+/- 0.5).
    difficulty_distribution: dict[str, float] = Field(default_factory=lambda: {"medium": 100.0})
    topics: list[str] = Field(default_factory=list, max_length=50)
    languages: list[str] = Field(default_factory=list)
    variants_per_slot: int = Field(default=4, ge=1, le=50)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _consistent(self):
        if any(count < 0 for count in self.type_counts.values()):
            raise ValueError("type counts cannot be negative")
        if sum(self.type_counts.values()) != self.question_count:
            raise ValueError(f"type counts add up to {sum(self.type_counts.values())}, not question_count ({self.question_count})")
        if any(v <= 0 for v in self.marks_per_type.values()):
            raise ValueError("marks must be positive")
        if any(v <= 0 or v > 180 for v in self.minutes_per_type.values()):
            raise ValueError("minutes per question must be between 1 and 180")
        if any(p < 0 for p in self.difficulty_distribution.values()) or abs(sum(self.difficulty_distribution.values()) - 100.0) > 0.5:
            raise ValueError("difficulty percentages must be non-negative and total 100")
        if self.type_counts.get(ItemType.CODING, 0) > 0 and not self.languages:
            raise ValueError("coding questions need at least one language")
        for topic in self.topics:
            if not topic.strip() or len(topic) > 120:
                raise ValueError("topics must be 1-120 characters")
        return self


def _largest_remainder(total: int, weights: dict[str, float]) -> dict[str, int]:
    raw = {k: total * w / 100.0 for k, w in weights.items()}
    counts = {k: int(v) for k, v in raw.items()}
    leftover = total - sum(counts.values())
    for k in sorted(raw, key=lambda key: (-(raw[key] - counts[key]), key))[:leftover]:
        counts[k] += 1
    return counts


def expand_spec(spec: BlueprintSpec) -> list[BlueprintSlot]:
    counts = _largest_remainder(spec.question_count, spec.difficulty_distribution)
    queues = {d: counts.get(d, 0) for d in sorted(counts, key=lambda d: (DIFFICULTY_ORDER.index(d) if d in DIFFICULTY_ORDER else 99, d))}
    difficulties: list[str] = []
    while any(queues.values()):  # interleave so no type gets all the easy (or hard) questions
        for name in queues:
            if queues[name]:
                queues[name] -= 1
                difficulties.append(name)

    slots: list[BlueprintSlot] = []
    index = 0
    for item_type in TYPE_ORDER:
        for _ in range(spec.type_counts.get(item_type, 0)):
            topic = spec.topics[index % len(spec.topics)] if spec.topics else "core"
            slots.append(BlueprintSlot(
                slot_index=index, item_type=item_type, objective_ids=[topic], difficulty=difficulties[index],
                marks=spec.marks_per_type.get(item_type, 1.0),
                negative_marks=spec.negative_marks_per_type.get(item_type, 0.0),
                target_minutes=spec.minutes_per_type.get(item_type, DEFAULT_MINUTES[item_type]),
                permitted_languages=list(spec.languages) if item_type == ItemType.CODING else [],
                requested_variant_count=spec.variants_per_slot,
            ))
            index += 1
    return slots
