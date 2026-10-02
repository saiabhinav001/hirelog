"""Independent review job tests — "Backend model prompt B": deterministic
fake review provider, the enforced response schema (cannot self-approve),
fabricated-anchor stripping, and durable/restart-safe job semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.domain.generation.independent_review_provider import (
    DeterministicFakeReviewProvider,
    HttpIndependentReviewProviderAdapter,
    IndependentReviewResult,
    ReviewProviderError,
    build_independent_review_request_payload,
)
from app.domain.generation.jobs import GenerationJobRunner, IndependentReviewJobRunner, new_generation_request
from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.generation.provider import DeterministicFakeProvider

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _FakeRepo:
    def __init__(self) -> None:
        self.requests: dict[str, object] = {}
        self.candidates: dict[str, GenerationCandidate] = {}
        self.malformed: list[tuple] = []

    def get_request(self, request_id: str):
        return self.requests.get(request_id)

    def update_request(self, request_id: str, update: dict) -> None:
        request = self.requests[request_id]
        self.requests[request_id] = request.model_copy(update=update)

    def previously_used_signatures(self, blueprint_id: str) -> set[str]:
        return {c.uniqueness_signature for c in self.candidates.values() if c.blueprint_id == blueprint_id}

    def save_candidate(self, candidate) -> None:
        self.candidates[candidate.id] = candidate

    def get_candidate(self, candidate_id: str) -> Optional[GenerationCandidate]:
        return self.candidates.get(candidate_id)

    def record_malformed_candidate(self, request_id, raw, issues) -> None:
        self.malformed.append((request_id, raw, issues))


def _request(repo: _FakeRepo, **overrides):
    defaults = dict(
        blueprint_id="bp-1", slot_index=0, objective_ids=["obj-1"], item_type=ItemType.SINGLE_CHOICE,
        difficulty="medium", marks=5.0, target_minutes=5, requested_variant_count=1,
        permitted_languages=[],
        approved_source_bundle=[{"source_id": "src-1", "source_version": 1, "anchor_id": "a1", "excerpt": "x"}],
        deterministic_seed=7, created_by="faculty-1", now=NOW,
    )
    defaults.update(overrides)
    request = new_generation_request(**defaults)
    repo.requests[request.id] = request
    return request


def _ready_candidate(repo: _FakeRepo, request) -> GenerationCandidate:
    """A candidate that already cleared structural validation (via the
    generation job runner + deterministic fake), landing on
    READY_FOR_HUMAN_REVIEW — the only status independent review acts on."""
    runner = GenerationJobRunner(repo, DeterministicFakeProvider())
    result = runner.run(request.id)
    assert result.status == "succeeded"
    assert result.ready_count == 1
    candidate = repo.candidates[result.candidate_ids[0]]
    assert candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW
    return candidate


# ── system prompt + enforced schema ─────────────────────────────────────────

def test_system_prompt_b_never_allows_self_approval() -> None:
    from app.domain.generation.independent_review_provider import GENERATION_SYSTEM_PROMPT_B_V1

    assert "never published or verified" in GENERATION_SYSTEM_PROMPT_B_V1
    assert "never approval on behalf of faculty" in GENERATION_SYSTEM_PROMPT_B_V1
    assert "Missing evidence is a blocking uncertainty" in GENERATION_SYSTEM_PROMPT_B_V1


def test_http_adapter_exposes_the_same_system_prompt_constant() -> None:
    from app.domain.generation.independent_review_provider import GENERATION_SYSTEM_PROMPT_B_V1

    assert HttpIndependentReviewProviderAdapter.SYSTEM_PROMPT == GENERATION_SYSTEM_PROMPT_B_V1


def test_response_schema_rejects_a_self_asserted_approved_recommendation() -> None:
    try:
        IndependentReviewResult.parse_payload(
            {"candidate_id": "c1", "agreement": "agree", "recommended_status": "approved", "issues": []}
        )
        raise AssertionError("expected ReviewProviderError")
    except ReviewProviderError:
        pass


def test_response_schema_rejects_missing_candidate_id() -> None:
    try:
        IndependentReviewResult.parse_payload({"agreement": "agree", "recommended_status": "ready_for_human_review"})
        raise AssertionError("expected ReviewProviderError")
    except ReviewProviderError:
        pass


def test_response_schema_accepts_the_deterministic_fake_providers_shape() -> None:
    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)
    result = DeterministicFakeReviewProvider().review(candidate, request)
    reparsed = IndependentReviewResult.parse_payload(result.model_dump())
    assert reparsed == result


def test_build_request_payload_includes_proposed_private_answer_and_source_bundle() -> None:
    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)
    payload = build_independent_review_request_payload(candidate, request)

    assert payload["candidate_id"] == candidate.id
    assert payload["proposed_private_answer"]["private_expected_answer"] == candidate.private_expected_answer
    assert payload["approved_source_bundle"] == [
        {"source_id": "src-1", "source_version": 1, "anchor_id": "a1", "excerpt": "x"}
    ]


# ── job runner: durable, restart-safe, never self-approves ─────────────────

def test_job_runner_downgrades_ready_candidate_on_disagreement() -> None:
    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)

    runner = IndependentReviewJobRunner(repo, DeterministicFakeReviewProvider(force_disagreement=True))
    result = runner.run(candidate.id)

    assert result.status == "succeeded"
    assert result.new_status == CandidateStatus.NEEDS_REVISION.value
    updated = repo.get_candidate(candidate.id)
    assert updated.status == CandidateStatus.NEEDS_REVISION
    assert updated.independent_review_agreement == "disagree"
    assert len(updated.independent_review_issues) == 1


def test_job_runner_never_advances_status_beyond_ready_for_human_review() -> None:
    """Even a clean "agree" review leaves the candidate at
    READY_FOR_HUMAN_REVIEW — it is never auto-approved or published."""
    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)

    runner = IndependentReviewJobRunner(repo, DeterministicFakeReviewProvider())
    result = runner.run(candidate.id)

    assert result.status == "succeeded"
    assert result.agreement == "agree"
    assert result.new_status == CandidateStatus.READY_FOR_HUMAN_REVIEW.value
    assert repo.get_candidate(candidate.id).status == CandidateStatus.READY_FOR_HUMAN_REVIEW


def test_job_runner_is_a_noop_on_a_second_run() -> None:
    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)
    runner = IndependentReviewJobRunner(repo, DeterministicFakeReviewProvider())

    first = runner.run(candidate.id)
    second = runner.run(candidate.id)

    assert first.status == "succeeded"
    assert second.status == "noop_already_reviewed"


def test_job_runner_is_a_noop_for_a_candidate_still_needing_revision() -> None:
    repo = _FakeRepo()
    request = _request(repo, approved_source_bundle=[])  # forces insufficient_source, no ready candidate
    runner = GenerationJobRunner(repo, DeterministicFakeProvider())
    runner.run(request.id)

    # Build a NEEDS_REVISION candidate directly to exercise the not-ready path.
    needs_revision = GenerationCandidate(
        id="cand-needs-revision", request_id=request.id, blueprint_id="bp-1", slot_index=0, family_id="fam-0",
        local_candidate_id="local-1", item_type=ItemType.SINGLE_CHOICE, objective_ids=["obj-1"], difficulty="medium",
        marks=5.0, estimated_minutes=5, uniqueness_signature="sig-1", public_prompt="p", status=CandidateStatus.NEEDS_REVISION,
        created_at=NOW,
    )
    repo.save_candidate(needs_revision)

    review_runner = IndependentReviewJobRunner(repo, DeterministicFakeReviewProvider())
    result = review_runner.run(needs_revision.id)
    assert result.status == "noop_not_ready"


def test_job_runner_survives_unexpected_provider_exception() -> None:
    class _BrokenReviewProvider:
        def review(self, candidate, request):
            raise RuntimeError("boom")

    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)
    runner = IndependentReviewJobRunner(repo, _BrokenReviewProvider())
    result = runner.run(candidate.id)
    assert result.status == "failed"
    assert "boom" in result.error
    # A failed attempt leaves the candidate untouched — safe to retry.
    assert repo.get_candidate(candidate.id).independent_review_completed_at is None


def test_job_runner_strips_a_fabricated_source_anchor_citation() -> None:
    class _FabricatingReviewProvider:
        def review(self, candidate, request):
            return IndependentReviewResult(
                candidate_id=candidate.id,
                agreement="uncertain",
                issues=[
                    {
                        "severity": "major",
                        "field": "public_prompt",
                        "reason": "cites a source not actually supplied",
                        "source_anchor": {"source_id": "src-NOT-SUPPLIED", "source_version": 1, "anchor_id": "a9"},
                        "counterexample": None,
                    }
                ],
                recommended_status="ready_for_human_review",
            )

    repo = _FakeRepo()
    request = _request(repo)
    candidate = _ready_candidate(repo, request)
    runner = IndependentReviewJobRunner(repo, _FabricatingReviewProvider())
    result = runner.run(candidate.id)

    assert result.status == "succeeded"
    assert len(result.fabricated_anchor_warnings) == 1
    updated = repo.get_candidate(candidate.id)
    assert updated.independent_review_issues[0].source_anchor is None
