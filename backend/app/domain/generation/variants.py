"""Variant reservation — one approved candidate per (slot, student), with
uniqueness. Insufficient verified pool blocks reservation unless the caller
explicitly opts into balanced reuse (never silently reused).
"""

from __future__ import annotations

import hashlib
import random
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


# -- Prompt 5 (ADR 0021): seeded, stored option order ------------------------------------------

def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def seeded_option_order(run_id: str, uid: str, slot_index: int, keys: list[str]) -> list[str]:
    """Per-student option order, a pure function of (run, student, slot) so it can be re-derived
    to audit it. It is STORED with the assignment, so a later code change never reshuffles a student."""
    order = list(keys)
    _rng("options", run_id, uid, slot_index).shuffle(order)
    return order


def seeded_candidate_order(run_id: str, slot_index: int, candidate_ids: list[str]) -> list[str]:
    """Spreads a slot's variants across students without handing neighbours the same one."""
    order = sorted(candidate_ids)
    _rng("variants", run_id, slot_index).shuffle(order)
    return order


def is_valid_option_order(keys: list[str], order: list[str]) -> bool:
    return sorted(order) == sorted(keys) and len(set(order)) == len(order)


def apply_option_order(options: list, order: list[str]) -> list:
    """Reorders {key, text} options; the key stays attached to its text, so grading is unaffected.
    An order that is not an exact permutation of the keys is ignored (canonical order)."""
    by_key = {str(o.get("key")): o for o in options if isinstance(o, dict)}
    if len(by_key) != len(options) or not is_valid_option_order(list(by_key), order):
        return list(options)
    return [by_key[k] for k in order]
