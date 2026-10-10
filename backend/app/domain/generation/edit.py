"""Teacher edits of a candidate (Prompt 5, ADR 0021): pure decision logic.

An edit never mutates what a teacher approved. Anything a student sees or that affects
grading (the "substantive" fields) creates the NEXT VERSION as a new candidate: it is
re-validated, starts back at needs-review with approval and independent review cleared,
and the previous version is kept as SUPERSEDED for history. Edits to fields only the
teacher sees (explanation, rationale) update in place and keep the approval. Cosmetic
changes (whitespace) are not edits at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Optional
from uuid import uuid4

from pydantic import ValidationError

from app.domain.generation.models import CandidateStatus, GenerationCandidate, GenerationRequest
from app.domain.generation.validation import validate_candidate

SUBSTANTIVE_FIELDS = (
    "public_prompt", "constraints", "public_examples", "options", "private_expected_answer", "marking_rubric",
    "reference_solutions", "public_tests", "hidden_test_groups", "starter_code",
)
MINOR_FIELDS = ("private_explanation", "equivalence_rationale")
EDITABLE_FIELDS = SUBSTANTIVE_FIELDS + MINOR_FIELDS
NOT_EDITABLE = frozenset({CandidateStatus.PUBLISHED, CandidateStatus.SUPERSEDED, CandidateStatus.REJECTED})


class CandidateEditError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EditOutcome:
    kind: Literal["unchanged", "minor", "new_version"]
    candidate: GenerationCandidate  # the live candidate after the edit
    superseded: Optional[GenerationCandidate] = None  # the previous version, when one was replaced


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _root_signature(signature: str) -> str:
    return signature.split("~v")[0]


def decide_edit(
    candidate: GenerationCandidate,
    changes: Mapping[str, Any],
    *,
    editor: str,
    now: datetime,
    request: GenerationRequest,
    previously_used_signatures: set[str],
    live_prompts: Mapping[str, str],
) -> EditOutcome:
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise CandidateEditError("field_not_editable", f"these fields cannot be edited: {unknown}")
    if not changes:
        raise CandidateEditError("nothing_to_edit", "no changes were supplied")
    if candidate.status in NOT_EDITABLE:
        raise CandidateEditError("candidate_not_editable", f"a {candidate.status.value} candidate cannot be edited")

    changed = {f: v for f, v in changes.items() if _norm(v) != _norm(getattr(candidate, f))}
    if not changed:
        return EditOutcome("unchanged", candidate)

    # Re-validating through the model enforces types and length limits on the new values.
    base = candidate.model_dump()
    try:
        applied = GenerationCandidate.model_validate({**base, **changed})
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        raise CandidateEditError("invalid_value", f"the new value for {fields} is not valid") from exc

    if not any(f in SUBSTANTIVE_FIELDS for f in changed):
        updated = applied.model_copy(update={"edited_by": editor, "edited_at": now})
        return EditOutcome("minor", updated)

    version = candidate.version + 1
    fresh = applied.model_copy(
        update={
            "id": uuid4().hex,
            "version": version,
            "previous_version_id": candidate.id,
            "superseded_by_id": None,
            "uniqueness_signature": f"{_root_signature(candidate.uniqueness_signature)}~v{version}",
            "status": CandidateStatus.DRAFT,
            "validation_issues": [],
            "created_at": now,
            "reviewed_by": None,
            "reviewed_at": None,
            "rejection_reason": None,
            "published_at": None,
            "published_revision_id": None,
            "independent_review_agreement": None,
            "independent_review_issues": [],
            "independent_review_completed_at": None,
            "execution_verified": False,
            "edited_by": editor,
            "edited_at": now,
        }
    )
    others = {k: v for k, v in live_prompts.items() if k != candidate.id}
    validated, _ = validate_candidate(
        fresh, request, previously_used_signatures=previously_used_signatures - {candidate.uniqueness_signature},
        signatures_seen_this_batch=set(), previous_prompts=others,
    )
    superseded = candidate.model_copy(update={"status": CandidateStatus.SUPERSEDED, "superseded_by_id": validated.id})
    return EditOutcome("new_version", validated, superseded)
