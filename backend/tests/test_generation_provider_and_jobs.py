"""Provider adapter + job-runner tests: deterministic fake provider, provider
timeout, insufficient_source, and durable/restart-safe job semantics (a crash
between attempts is safe to retry; an already-succeeded request is a no-op).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.generation.jobs import GenerationJobRunner, new_generation_request
from app.domain.generation.models import CandidateStatus, ItemType
from app.domain.generation.provider import DeterministicFakeProvider

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _FakeGenerationRepo:
    """In-memory stand-in for `GenerationRepository` — proves the job
    runner's durability contract without needing Firestore."""

    def __init__(self) -> None:
        self.requests: dict[str, object] = {}
        self.candidates: dict[str, object] = {}
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

    def record_malformed_candidate(self, request_id, raw, issues) -> None:
        self.malformed.append((request_id, raw, issues))

    def claim_request(self, request_id, *, now, stale_after_seconds=600):
        request = self.requests[request_id]
        if request.status == "succeeded":
            return None
        claimed = request.model_copy(update={"status": "running", "attempts": request.attempts + 1, "running_since": now})
        self.requests[request_id] = claimed
        return claimed

    def live_prompts_for_slot(self, blueprint_id, slot_index):
        return {}


def _request(repo: _FakeGenerationRepo, **overrides):
    defaults = dict(
        blueprint_id="bp-1", slot_index=0, objective_ids=["obj-1"], item_type=ItemType.SINGLE_CHOICE,
        difficulty="medium", marks=5.0, target_minutes=5, requested_variant_count=2,
        permitted_languages=[],
        approved_source_bundle=[{"source_id": "src-1", "source_version": 1, "anchor_id": "a1", "excerpt": "x"}],
        deterministic_seed=7, created_by="faculty-1", now=NOW,
    )
    defaults.update(overrides)
    request = new_generation_request(**defaults)
    repo.requests[request.id] = request
    return request


def test_deterministic_fake_provider_is_reproducible() -> None:
    repo = _FakeGenerationRepo()
    request = _request(repo, deterministic_seed=123)
    provider = DeterministicFakeProvider()
    response1 = provider.generate(request)
    response2 = provider.generate(request)
    assert [c["uniqueness_signature"] for c in response1.candidates] == [
        c["uniqueness_signature"] for c in response2.candidates
    ]


def test_provider_insufficient_source_when_no_bundle() -> None:
    repo = _FakeGenerationRepo()
    request = _request(repo, approved_source_bundle=[])
    response = DeterministicFakeProvider().generate(request)
    assert response.status == "insufficient_source"


def test_job_runner_succeeds_and_persists_ready_candidates() -> None:
    repo = _FakeGenerationRepo()
    request = _request(repo, requested_variant_count=2)
    runner = GenerationJobRunner(repo, DeterministicFakeProvider())
    result = runner.run(request.id)
    assert result.status == "succeeded"
    assert result.ready_count == 2
    assert len(repo.candidates) == 2
    assert all(c.status == CandidateStatus.READY_FOR_HUMAN_REVIEW for c in repo.candidates.values())


def test_job_runner_handles_provider_timeout_as_retryable_failure() -> None:
    repo = _FakeGenerationRepo()
    request = _request(repo)
    runner = GenerationJobRunner(repo, DeterministicFakeProvider(force_timeout=True))
    result = runner.run(request.id)
    assert result.status == "failed"
    assert repo.requests[request.id].status == "failed"
    assert "timeout" in repo.requests[request.id].last_error


def test_job_runner_handles_insufficient_capacity() -> None:
    repo = _FakeGenerationRepo()
    request = _request(repo)
    runner = GenerationJobRunner(repo, DeterministicFakeProvider(force_insufficient=True))
    result = runner.run(request.id)
    assert result.status == "insufficient_source"
    assert result.shortfall == request.requested_variant_count


def test_job_runner_restart_after_crash_is_idempotent_noop() -> None:
    """Simulates a worker restart: calling run() again on an
    already-succeeded request must not duplicate candidates."""
    repo = _FakeGenerationRepo()
    request = _request(repo)
    runner = GenerationJobRunner(repo, DeterministicFakeProvider())
    first = runner.run(request.id)
    candidate_count_after_first = len(repo.candidates)

    second = runner.run(request.id)

    assert first.status == "succeeded"
    assert second.status == "noop_already_done"
    assert len(repo.candidates) == candidate_count_after_first  # nothing duplicated


def test_job_runner_survives_unexpected_provider_exception() -> None:
    class _BrokenProvider:
        def generate(self, request):
            raise RuntimeError("boom")

    repo = _FakeGenerationRepo()
    request = _request(repo)
    runner = GenerationJobRunner(repo, _BrokenProvider())
    result = runner.run(request.id)
    assert result.status == "failed"
    assert "boom" in result.error


def test_job_runner_records_malformed_candidate_without_failing_whole_batch() -> None:
    class _PartlyBrokenProvider:
        def generate(self, request):
            from app.domain.generation.provider import ProviderResponse

            good = DeterministicFakeProvider().generate(request).candidates[0]
            bad = {"local_candidate_id": "bad-1"}  # missing required keys
            return ProviderResponse(request_id=request.id, status="draft", candidates=[good, bad])

    repo = _FakeGenerationRepo()
    request = _request(repo, requested_variant_count=2)
    runner = GenerationJobRunner(repo, _PartlyBrokenProvider())
    result = runner.run(request.id)
    assert result.status == "succeeded"
    assert result.ready_count == 1
    assert len(repo.malformed) == 1


# ── "Backend model prompt A" wiring: system prompt + enforced envelope ──────

def test_system_prompt_forbids_self_approval_and_fabrication() -> None:
    from app.domain.generation.provider import GENERATION_SYSTEM_PROMPT_A_V1

    assert "Never mark an item approved or verified" in GENERATION_SYSTEM_PROMPT_A_V1
    assert "do not invent URLs, citations, facts or source IDs" in GENERATION_SYSTEM_PROMPT_A_V1
    assert "You have no student identities" in GENERATION_SYSTEM_PROMPT_A_V1


def test_http_adapter_exposes_the_same_system_prompt_constant() -> None:
    from app.domain.generation.provider import GENERATION_SYSTEM_PROMPT_A_V1, HttpGenerationProviderAdapter

    assert HttpGenerationProviderAdapter.SYSTEM_PROMPT == GENERATION_SYSTEM_PROMPT_A_V1


def test_provider_response_envelope_rejects_a_self_asserted_approved_status() -> None:
    from app.domain.generation.provider import ProviderError, ProviderResponse

    try:
        ProviderResponse.parse_payload({"request_id": "r1", "status": "approved", "candidates": []})
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass


def test_provider_response_envelope_rejects_missing_request_id() -> None:
    from app.domain.generation.provider import ProviderError, ProviderResponse

    try:
        ProviderResponse.parse_payload({"status": "draft", "candidates": []})
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass


def test_provider_response_envelope_accepts_the_deterministic_fake_providers_shape() -> None:
    """Proves the schema is realistic, not just decorative — even the fake
    provider's own output round-trips through it unchanged."""
    from app.domain.generation.provider import ProviderResponse

    repo = _FakeGenerationRepo()
    request = _request(repo)
    response = DeterministicFakeProvider().generate(request)
    reparsed = ProviderResponse.parse_payload(response.model_dump())
    assert reparsed == response


def test_build_request_payload_contains_only_the_allowlisted_fields() -> None:
    from app.domain.generation.provider import build_provider_request_payload

    repo = _FakeGenerationRepo()
    request = _request(repo)
    payload = build_provider_request_payload(request, previously_used_signatures={"sig-old-1"})

    assert set(payload.keys()) == {
        "request_id", "blueprint_revision", "question_slot", "objective_ids", "item_type",
        "difficulty", "marks", "target_minutes", "requested_variant_count", "permitted_languages",
        "approved_source_bundle", "previously_used_signatures", "deterministic_seed",
        "options_will_be_shuffled", "source_text_is_untrusted_data",
    }
    assert payload["request_id"] == request.id
    assert payload["previously_used_signatures"] == ["sig-old-1"]
    assert payload["approved_source_bundle"] == [
        {"source_id": "src-1", "source_version": 1, "anchor_id": "a1", "page": None, "section": None, "excerpt": "x"}
    ]


def test_http_adapter_build_request_payload_matches_the_module_function() -> None:
    from app.domain.generation.provider import HttpGenerationProviderAdapter, build_provider_request_payload

    repo = _FakeGenerationRepo()
    request = _request(repo)
    adapter = HttpGenerationProviderAdapter(base_url="https://example.test", api_key="k")
    assert adapter.build_request_payload(request, previously_used_signatures=set()) == (
        build_provider_request_payload(request, previously_used_signatures=set())
    )
