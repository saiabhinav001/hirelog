"""Allowlisted student-facing question content — the ONLY place that decides
what of a `GenerationCandidate` is safe to serialize into `PublicQuestionRef
.public_content` (contract #3: never locked/private content to a student).

Only fields already named `public_*`/`options`/`constraints`/`estimated_minutes`
/`marks` are ever read; `private_expected_answer`, `private_explanation`,
`marking_rubric`, `reference_solutions` (values), and `hidden_test_groups`
are never touched here. For coding items, only the reference_solutions'
LANGUAGE KEYS are exposed (so the student knows what they may submit in) —
never the solution source code itself.
"""

from __future__ import annotations

from app.domain.generation.models import GenerationCandidate, ItemType


def build_public_content(candidate: GenerationCandidate) -> dict:
    content: dict = {
        "item_type": candidate.item_type.value,
        "marks": candidate.marks,
        "estimated_minutes": candidate.estimated_minutes,
        "prompt": candidate.public_prompt,
        "constraints": candidate.constraints,
        "examples": candidate.public_examples,
    }
    if candidate.item_type in (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT):
        content["options"] = candidate.options
    elif candidate.item_type == ItemType.CODING:
        content["permitted_languages"] = sorted(candidate.reference_solutions.keys())
        content["public_tests"] = candidate.public_tests
    return content
