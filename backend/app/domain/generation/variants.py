"""Variant reservation — one approved candidate per (slot, student), with
uniqueness. Insufficient verified pool blocks reservation unless the caller
explicitly opts into balanced reuse (never silently reused).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VariantReservationResult:
    assignments: dict[str, str] = field(default_factory=dict)  # uid -> candidate_id
    blocked: bool = False
    reason: str | None = None


def reserve_variants(
    *, uids: list[str], approved_candidate_ids: list[str], allow_balanced_reuse: bool
) -> VariantReservationResult:
    if not uids:
        return VariantReservationResult(assignments={}, blocked=False)

    if not approved_candidate_ids:
        return VariantReservationResult(
            assignments={}, blocked=True, reason="no approved candidates available for this slot"
        )

    if len(approved_candidate_ids) >= len(uids):
        # Enough for a fully unique 1:1 assignment.
        assignments = {uid: approved_candidate_ids[i] for i, uid in enumerate(uids)}
        return VariantReservationResult(assignments=assignments, blocked=False)

    if not allow_balanced_reuse:
        return VariantReservationResult(
            assignments={},
            blocked=True,
            reason=(
                f"insufficient verified pool: need {len(uids)} unique variant(s), "
                f"have {len(approved_candidate_ids)} approved"
            ),
        )

    # Explicit balanced reuse — round-robin so reuse count is as even as
    # possible across the approved pool, never all students on one variant.
    assignments = {uid: approved_candidate_ids[i % len(approved_candidate_ids)] for i, uid in enumerate(uids)}
    return VariantReservationResult(assignments=assignments, blocked=False)
