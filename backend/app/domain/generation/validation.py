"""Structured validation for provider-returned candidates.

This is where every non-negotiable from contract #7 is enforced in code, not
just prompt text: source anchors must actually exist in the supplied bundle,
duplicates are rejected, objective/difficulty/marks must match the request,
and — critically — whatever `status` field a (possibly compromised or
malfunctioning) provider claims is IGNORED. The status returned here is
always recomputed from the issues found; a provider cannot self-approve.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from pydantic import ValidationError

from app.domain.generation.models import (
    CandidateStatus,
    GenerationCandidate,
    GenerationRequest,
    ValidationIssue,
)


def _blocker(field: str, reason: str) -> ValidationIssue:
    return ValidationIssue(severity="blocker", field=field, reason=reason)


def _major(field: str, reason: str) -> ValidationIssue:
    return ValidationIssue(severity="major", field=field, reason=reason)


def validate_candidate_dict(
    raw: dict,
    *,
    request: GenerationRequest,
    now: datetime,
    previously_used_signatures: set[str],
    signatures_seen_this_batch: set[str],
) -> tuple[Optional[GenerationCandidate], list[ValidationIssue]]:
    """Returns (candidate_or_none, issues). `candidate` is None only when the
    raw dict fails basic schema validation (malformed provider JSON) —
    everything else produces a candidate with status=NEEDS_REVISION and the
    issues attached, so faculty can see exactly what a provider got wrong.
    """

    issues: list[ValidationIssue] = []

    try:
        candidate = GenerationCandidate(
            id=uuid4().hex,
            request_id=request.id,
            blueprint_id=request.blueprint_id,
            slot_index=request.slot_index,
            family_id=str(raw["family_id"]),
            local_candidate_id=str(raw["local_candidate_id"]),
            item_type=raw["item_type"],
            objective_ids=list(raw.get("objective_ids") or []),
            difficulty=str(raw.get("difficulty", "")),
            marks=float(raw.get("marks", 0)),
            estimated_minutes=int(raw.get("estimated_minutes", 0)),
            uniqueness_signature=str(raw.get("uniqueness_signature", "")),
            public_prompt=str(raw.get("public_prompt", "")),
            constraints=dict(raw.get("constraints") or {}),
            public_examples=list(raw.get("public_examples") or []),
            options=list(raw.get("options") or []),
            private_expected_answer=raw.get("private_expected_answer"),
            private_explanation=raw.get("private_explanation"),
            marking_rubric=dict(raw.get("marking_rubric") or {}),
            reference_solutions=dict(raw.get("reference_solutions") or {}),
            public_tests=list(raw.get("public_tests") or []),
            supporting_anchors=[
                {
                    "source_id": a.get("source_id"),
                    "source_version": a.get("source_version"),
                    "anchor_id": a.get("anchor_id"),
                }
                for a in (raw.get("supporting_anchors") or [])
            ],
            equivalence_rationale=str(raw.get("equivalence_rationale", "")),
            status=CandidateStatus.DRAFT,  # NEVER read from `raw` — see module docstring
            created_at=now,
        )
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        return None, [_blocker("schema", f"malformed provider candidate: {exc}")]

    # Source-anchor checks: every cited anchor must exist in the bundle we
    # actually supplied — a provider cannot invent a citation.
    bundle_keys = {(a.source_id, a.source_version, a.anchor_id) for a in request.approved_source_bundle}
    if not candidate.supporting_anchors:
        issues.append(_blocker("supporting_anchors", "no supporting anchors cited"))
    for anchor in candidate.supporting_anchors:
        key = (anchor.source_id, anchor.source_version, anchor.anchor_id)
        if key not in bundle_keys:
            issues.append(
                _blocker(
                    "supporting_anchors",
                    f"cited anchor {anchor.anchor_id!r} does not exist in the supplied source bundle",
                )
            )

    # Duplicate checks.
    if not candidate.uniqueness_signature:
        issues.append(_blocker("uniqueness_signature", "missing"))
    elif candidate.uniqueness_signature in previously_used_signatures:
        issues.append(_blocker("uniqueness_signature", "duplicates a previously used signature"))
    elif candidate.uniqueness_signature in signatures_seen_this_batch:
        issues.append(_blocker("uniqueness_signature", "duplicates another candidate in this batch"))

    # Objective/difficulty/marks must match the request — a provider cannot
    # silently substitute an easier item to reach the requested count.
    if set(candidate.objective_ids) != set(request.objective_ids):
        issues.append(_major("objective_ids", "does not match the requested objectives"))
    if candidate.difficulty != request.difficulty:
        issues.append(_major("difficulty", f"expected {request.difficulty!r}, got {candidate.difficulty!r}"))
    if abs(candidate.marks - request.marks) > 1e-6:
        issues.append(_major("marks", f"expected {request.marks}, got {candidate.marks}"))
    if candidate.item_type.value != request.item_type.value:
        issues.append(_blocker("item_type", "does not match the requested item type"))

    # Reference-solution/test validation — STRUCTURAL only. This does not
    # execute anything; execution belongs to the isolated judge (Prompt 6).
    if candidate.item_type.value == "coding":
        for language in request.permitted_languages:
            if not candidate.reference_solutions.get(language, "").strip():
                issues.append(_blocker("reference_solutions", f"missing reference solution for {language!r}"))
        if not candidate.public_tests:
            issues.append(_major("public_tests", "no public tests supplied"))
    if candidate.item_type.value == "single_choice" and len(candidate.options) < 2:
        issues.append(_blocker("options", "single_choice item needs at least 2 options"))

    has_blocker = any(issue.severity == "blocker" for issue in issues)
    candidate = candidate.model_copy(
        update={
            "validation_issues": issues,
            "status": CandidateStatus.NEEDS_REVISION if has_blocker else CandidateStatus.READY_FOR_HUMAN_REVIEW,
        }
    )
    return candidate, issues
