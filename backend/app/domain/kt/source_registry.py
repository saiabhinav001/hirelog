"""Pure decision functions for the approved-source review lifecycle. Faculty
review is the ONLY way a source becomes approved — nothing in the ingestion
pipeline (scan, parse) can set review_status itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.domain.kt.models import ApprovedSource, RightsEvidence, SourceReviewStatus


def decide_approve_source(
    source: ApprovedSource, *, reviewer: str, rights_evidence: RightsEvidence, now: datetime
) -> ApprovedSource:
    if source.review_status != SourceReviewStatus.PENDING:
        raise ValueError(f"source {source.id} is not pending review (status={source.review_status.value})")
    return source.model_copy(
        update={
            "review_status": SourceReviewStatus.APPROVED,
            "rights_evidence": rights_evidence,
            "reviewed_by": reviewer,
            "reviewed_at": now,
            "updated_at": now,
        }
    )


def decide_reject_source(
    source: ApprovedSource, *, reviewer: str, reason: str, now: datetime
) -> ApprovedSource:
    if source.review_status != SourceReviewStatus.PENDING:
        raise ValueError(f"source {source.id} is not pending review (status={source.review_status.value})")
    return source.model_copy(
        update={
            "review_status": SourceReviewStatus.REJECTED,
            "rejection_reason": reason,
            "reviewed_by": reviewer,
            "reviewed_at": now,
            "updated_at": now,
        }
    )


def is_citable(source: Optional[ApprovedSource]) -> bool:
    """A generation request may only cite a source that is APPROVED — this
    is checked again at validation time (`generation/validation.py`), not
    trusted from whatever the caller claims."""
    return source is not None and source.review_status == SourceReviewStatus.APPROVED
