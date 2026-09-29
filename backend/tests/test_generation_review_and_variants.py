"""Faculty review transitions and variant reservation — insufficient pool
blocks publication unless balanced reuse is explicitly selected; duplicate
variant assignment (the same candidate reused without opting in) is rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.generation.models import CandidateStatus, GenerationCandidate
from app.domain.generation.review import decide_review_candidate
from app.domain.generation.variants import reserve_variants

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _candidate(status: CandidateStatus = CandidateStatus.READY_FOR_HUMAN_REVIEW, **overrides) -> GenerationCandidate:
    defaults = dict(
        id="cand-1", request_id="req-1", blueprint_id="bp-1", slot_index=0, family_id="fam-1",
        local_candidate_id="local-1", item_type="single_choice", objective_ids=["obj-1"], difficulty="medium",
        marks=5.0, estimated_minutes=5, uniqueness_signature="sig-1", public_prompt="Q?", status=status,
        created_at=NOW,
    )
    defaults.update(overrides)
    return GenerationCandidate(**defaults)


# ── Review transitions ───────────────────────────────────────────────────────

def test_approve_ready_candidate_succeeds() -> None:
    outcome = decide_review_candidate(_candidate(), decision="approve", reviewer="fac-1", now=NOW)
    assert outcome.accepted
    assert outcome.candidate.status == CandidateStatus.APPROVED
    assert outcome.candidate.reviewed_by == "fac-1"


def test_cannot_approve_candidate_that_needs_revision() -> None:
    outcome = decide_review_candidate(
        _candidate(status=CandidateStatus.NEEDS_REVISION), decision="approve", reviewer="fac-1", now=NOW
    )
    assert not outcome.accepted
    assert "must clear validation" in outcome.reason


def test_reject_needs_revision_candidate_succeeds() -> None:
    outcome = decide_review_candidate(
        _candidate(status=CandidateStatus.NEEDS_REVISION), decision="reject", reviewer="fac-1",
        now=NOW, rejection_reason="wrong objective",
    )
    assert outcome.accepted
    assert outcome.candidate.status == CandidateStatus.REJECTED
    assert outcome.candidate.rejection_reason == "wrong objective"


def test_cannot_review_a_published_candidate() -> None:
    outcome = decide_review_candidate(
        _candidate(status=CandidateStatus.PUBLISHED), decision="approve", reviewer="fac-1", now=NOW
    )
    assert not outcome.accepted
    assert "published" in outcome.reason


def test_cannot_approve_an_already_approved_candidate_twice() -> None:
    outcome = decide_review_candidate(
        _candidate(status=CandidateStatus.APPROVED), decision="approve", reviewer="fac-1", now=NOW
    )
    assert not outcome.accepted


# ── Variant reservation ───────────────────────────────────────────────────────

def test_reserve_variants_assigns_unique_candidate_per_student() -> None:
    result = reserve_variants(
        uids=["s1", "s2", "s3"], approved_candidate_ids=["c1", "c2", "c3"], allow_balanced_reuse=False
    )
    assert not result.blocked
    assert len(set(result.assignments.values())) == 3  # every student gets a distinct variant


def test_reserve_variants_blocks_when_pool_insufficient_without_opt_in() -> None:
    result = reserve_variants(
        uids=["s1", "s2", "s3"], approved_candidate_ids=["c1"], allow_balanced_reuse=False
    )
    assert result.blocked
    assert "insufficient verified pool" in result.reason
    assert result.assignments == {}


def test_reserve_variants_allows_balanced_reuse_when_explicitly_opted_in() -> None:
    result = reserve_variants(
        uids=["s1", "s2", "s3", "s4"], approved_candidate_ids=["c1", "c2"], allow_balanced_reuse=True
    )
    assert not result.blocked
    # Round-robin: c1,c2,c1,c2 — reuse is even, never all on one variant.
    assert result.assignments == {"s1": "c1", "s2": "c2", "s3": "c1", "s4": "c2"}


def test_reserve_variants_blocks_when_pool_empty() -> None:
    result = reserve_variants(uids=["s1"], approved_candidate_ids=[], allow_balanced_reuse=True)
    assert result.blocked
    assert "no approved candidates" in result.reason


def test_reserve_variants_with_no_students_is_a_trivial_noop() -> None:
    result = reserve_variants(uids=[], approved_candidate_ids=["c1"], allow_balanced_reuse=False)
    assert not result.blocked
    assert result.assignments == {}
