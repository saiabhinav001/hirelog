"""Pure decision logic for applying an independent review result to a
candidate. Separate from `review.py` (faculty approve/reject) — this can
only move a candidate from READY_FOR_HUMAN_REVIEW back to NEEDS_REVISION; it
can never approve, reject or publish (contract: "never published or
verified" — enforced here by construction, not just by the schema).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.domain.generation.independent_review_provider import IndependentReviewResult
from app.domain.generation.models import CandidateStatus, GenerationCandidate, IndependentReviewIssue, ItemType, SourceAnchorRef


@dataclass(frozen=True)
class IndependentReviewApplication:
    candidate: GenerationCandidate
    fabricated_anchor_warnings: list[str]


def apply_independent_review(
    candidate: GenerationCandidate,
    result: IndependentReviewResult,
    *,
    known_anchors: set[tuple[str, int, str]],
    now: datetime,
) -> IndependentReviewApplication:
    """`known_anchors` is the set of (source_id, source_version, anchor_id)
    triples actually supplied to the provider — any `source_anchor` an issue
    cites that is NOT in this set is a fabricated citation (same anti-
    fabrication contract as `validation.py::validate_candidate_dict`). Such
    anchors are stripped (replaced with `None`) rather than trusted, and
    reported back so the caller/job runner can log it."""
    if result.candidate_id != candidate.id:
        raise ValueError(f"review result candidate_id {result.candidate_id!r} does not match {candidate.id!r}")

    fabricated_warnings: list[str] = []
    sanitized_issues = []
    for issue in result.issues:
        anchor: Optional[SourceAnchorRef] = issue.source_anchor
        if anchor is not None:
            key = (anchor.source_id, anchor.source_version, anchor.anchor_id)
            if key not in known_anchors:
                fabricated_warnings.append(
                    f"independent review cited a source anchor not in the supplied bundle: {anchor.anchor_id!r}"
                )
                issue = issue.model_copy(update={"source_anchor": None})
        sanitized_issues.append(issue)

    # Deterministic answer-key check for choice items: the backend compares the reviewer's BLIND
    # answer with the key itself, so a provider that says "agree" while answering differently
    # (or not answering) cannot wave an item through.
    agreement = result.agreement
    force_downgrade = False
    if candidate.item_type in (ItemType.SINGLE_CHOICE, ItemType.MULTIPLE_SELECT):
        expected = _key_set(candidate.private_expected_answer)
        given = _key_set(result.independent_answer)
        if not given:
            sanitized_issues.append(IndependentReviewIssue(
                severity="blocker", field="independent_answer",
                reason="the reviewer gave no answer, so the answer key could not be verified"))
            agreement, force_downgrade = "uncertain", True
        elif given != expected:
            sanitized_issues.append(IndependentReviewIssue(
                severity="blocker", field="private_expected_answer",
                reason=f"the reviewer solved it independently as {sorted(given)} but the key says {sorted(expected)}",
                counterexample=f"independent answer: {result.independent_answer}"))
            agreement, force_downgrade = "disagree", True

    # Never upgrades: a candidate not currently READY_FOR_HUMAN_REVIEW is
    # left exactly as-is (e.g. already NEEDS_REVISION, APPROVED, REJECTED,
    # PUBLISHED) - this review can only ever pull a ready candidate back.
    new_status = candidate.status
    pulls_back = (
        result.recommended_status == "needs_revision" or force_downgrade
        or any(issue.severity == "blocker" for issue in sanitized_issues)
    )
    if candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW and pulls_back:
        new_status = CandidateStatus.NEEDS_REVISION

    updated = candidate.model_copy(
        update={
            "status": new_status,
            "independent_review_agreement": agreement,
            "independent_review_issues": sanitized_issues,
            "independent_review_completed_at": now,
        }
    )
    return IndependentReviewApplication(candidate=updated, fabricated_anchor_warnings=fabricated_warnings)


def _key_set(value: Optional[str]) -> set[str]:
    return {part.strip() for part in (value or "").split(",") if part.strip()}