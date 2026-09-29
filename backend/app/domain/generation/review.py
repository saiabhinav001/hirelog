"""Faculty review (approve/reject) and immutable publish. The generation
model's own `status` claim was already discarded in `validation.py` — this
module is the ONLY code path that can move a candidate to APPROVED, and
`publish.py`/immutable-publish is the only path to PUBLISHED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from app.domain.generation.models import CandidateStatus, GenerationCandidate


@dataclass(frozen=True)
class ReviewOutcome:
    accepted: bool
    candidate: Optional[GenerationCandidate]
    reason: Optional[str] = None


def decide_review_candidate(
    candidate: GenerationCandidate,
    *,
    decision: Literal["approve", "reject"],
    reviewer: str,
    now: datetime,
    rejection_reason: Optional[str] = None,
) -> ReviewOutcome:
    if candidate.status == CandidateStatus.PUBLISHED:
        return ReviewOutcome(False, None, reason="cannot review an already-published candidate")

    if decision == "approve":
        if candidate.status != CandidateStatus.READY_FOR_HUMAN_REVIEW:
            return ReviewOutcome(
                False, None,
                reason=f"cannot approve a candidate in status={candidate.status.value} — it must clear validation first",
            )
        updated = candidate.model_copy(
            update={"status": CandidateStatus.APPROVED, "reviewed_by": reviewer, "reviewed_at": now}
        )
        return ReviewOutcome(True, updated)

    if candidate.status not in (CandidateStatus.READY_FOR_HUMAN_REVIEW, CandidateStatus.NEEDS_REVISION):
        return ReviewOutcome(False, None, reason=f"cannot reject a candidate in status={candidate.status.value}")
    updated = candidate.model_copy(
        update={
            "status": CandidateStatus.REJECTED,
            "reviewed_by": reviewer,
            "reviewed_at": now,
            "rejection_reason": rejection_reason,
        }
    )
    return ReviewOutcome(True, updated)
