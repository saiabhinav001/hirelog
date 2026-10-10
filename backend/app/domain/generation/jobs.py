"""Generation job orchestration — durable and restart-safe (contract #11):
all state lives in Firestore on the `GenerationRequest`/candidates, so ANY
worker process can pick up `run_generation_job` after a crash; there is no
in-process-only state a restart would lose. Calling this function again on
an already-succeeded request is a safe no-op.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from app.domain.exams.clock import Clock, system_clock
from app.domain.generation.independent_review import apply_independent_review
from app.domain.generation.independent_review_provider import (
    IndependentReviewProvider,
    ReviewProviderError,
    ReviewProviderTimeoutError,
)
from app.domain.generation.models import CandidateStatus, GenerationRequest
from app.domain.generation.provider import GenerationProvider, ProviderError, ProviderTimeoutError
from app.domain.generation.repository import GenerationRepository
from app.domain.generation.validation import validate_candidate_dict


@dataclass
class JobResult:
    status: str  # "succeeded" | "failed" | "insufficient_source" | "noop_already_done"
    ready_count: int = 0
    needs_revision_count: int = 0
    shortfall: int = 0
    error: str | None = None
    candidate_ids: list[str] = field(default_factory=list)


MAX_ATTEMPTS = 3


class GenerationJobRunner:
    def __init__(self, repo: GenerationRepository, provider: GenerationProvider, clock: Clock = system_clock) -> None:
        self._repo = repo
        self._provider = provider
        self._clock = clock

    def run(self, request_id: str, *, max_attempts: int = MAX_ATTEMPTS) -> JobResult:
        request = self._repo.get_request(request_id)
        if request is None:
            raise LookupError(f"generation request {request_id} not found")

        if request.status == "succeeded":
            return JobResult(status="noop_already_done")
        if request.status == "failed" and request.attempts >= max_attempts:
            return JobResult(status="failed", error=f"retry limit reached after {request.attempts} attempts: {request.last_error}")

        now = self._clock.now()
        # Compare-and-set in a transaction: two workers (or a double click) cannot both run one request.
        claimed = self._repo.claim_request(request_id, now=now)
        if claimed is None:
            return JobResult(status="noop_in_progress")
        request = claimed
        retries = max(0, request.attempts - 1)

        previously_used = self._repo.previously_used_signatures(request.blueprint_id)
        previous_prompts = self._repo.live_prompts_for_slot(request.blueprint_id, request.slot_index)
        started = time.monotonic()
        try:
            response = self._provider.generate(request.model_copy(update={"previous_signatures": sorted(previously_used)}))
        except ProviderTimeoutError as exc:
            return self._fail(request_id, f"timeout: {exc}", str(exc), retries, started)
        except ProviderError as exc:
            return self._fail(request_id, str(exc), str(exc), retries, started)
        except Exception as exc:  # noqa: BLE001 - never let a malformed provider crash the worker
            return self._fail(request_id, f"unexpected error: {type(exc).__name__}", str(exc), retries, started)

        metadata = {
            "provider_name": type(self._provider).__name__, "model_name": response.model_name, "usage": response.usage,
            "cost_estimate": _cost_from(response.usage), "retries": retries,
            "latency_ms": response.latency_ms if response.latency_ms is not None else int((time.monotonic() - started) * 1000),
            "completed_at": self._clock.now().isoformat(), "running_since": None,
        }

        if response.status == "insufficient_source":
            reason = "insufficient_source: " + "; ".join(response.issues) if response.issues else "insufficient_source"
            self._repo.update_request(request_id, {**metadata, "status": "failed", "last_error": reason, "accepted_shortfall": response.shortfall})
            return JobResult(status="insufficient_source", shortfall=response.shortfall)

        signatures_seen: set[str] = set()
        batch_prompts: dict[str, str] = {}
        ready_count = 0
        needs_revision_count = 0
        candidate_ids: list[str] = []

        # A provider cannot flood a slot: at most the requested number of candidates is considered.
        for raw in response.candidates[: request.requested_variant_count]:
            candidate, issues = validate_candidate_dict(
                raw,
                request=request,
                now=now,
                previously_used_signatures=previously_used,
                signatures_seen_this_batch=signatures_seen,
                previous_prompts=previous_prompts,
                batch_prompts=batch_prompts,
            )
            if candidate is None:
                # Malformed provider JSON for this one candidate - record and
                # continue; one bad candidate must not sink the whole batch.
                self._repo.record_malformed_candidate(request_id, raw if isinstance(raw, dict) else {"raw": str(raw)[:2000]}, issues)
                continue

            signatures_seen.add(candidate.uniqueness_signature)
            batch_prompts[candidate.id] = candidate.public_prompt
            self._repo.save_candidate(candidate)
            candidate_ids.append(candidate.id)
            if candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW:
                ready_count += 1
            else:
                needs_revision_count += 1

        shortfall = max(0, request.requested_variant_count - ready_count)
        self._repo.update_request(request_id, {**metadata, "status": "succeeded", "last_error": None, "accepted_shortfall": shortfall})
        return JobResult(
            status="succeeded",
            ready_count=ready_count,
            needs_revision_count=needs_revision_count,
            shortfall=shortfall,
            candidate_ids=candidate_ids,
        )

    def _fail(self, request_id: str, last_error: str, error: str, retries: int, started: float) -> JobResult:
        self._repo.update_request(request_id, {
            "status": "failed", "last_error": last_error[:500], "retries": retries,
            "provider_name": type(self._provider).__name__, "latency_ms": int((time.monotonic() - started) * 1000),
            "completed_at": self._clock.now().isoformat(), "running_since": None,
        })
        return JobResult(status="failed", error=error)


def _cost_from(usage: dict) -> float | None:
    value = usage.get("cost_usd") if isinstance(usage, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

@dataclass
class IndependentReviewJobResult:
    status: str  # "succeeded" | "failed" | "noop_already_reviewed" | "noop_not_ready"
    agreement: str | None = None
    new_status: str | None = None
    fabricated_anchor_warnings: list[str] = field(default_factory=list)
    error: str | None = None


class IndependentReviewJobRunner:
    """"Backend model prompt B" job runner — a SEPARATE job from
    `GenerationJobRunner` (contract: "Use a separate review job"). Durable/
    restart-safe the same way: all state lives on the candidate document, so
    re-running after a crash is a safe no-op once `independent_review_completed_at`
    is set, and re-running before that point simply redoes the (deterministic-
    for-tests) review call.
    """

    def __init__(self, repo: GenerationRepository, provider: IndependentReviewProvider, clock: Clock = system_clock) -> None:
        self._repo = repo
        self._provider = provider
        self._clock = clock

    def run(self, candidate_id: str) -> IndependentReviewJobResult:
        candidate = self._repo.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError(f"generation candidate {candidate_id} not found")

        if candidate.independent_review_completed_at is not None:
            return IndependentReviewJobResult(status="noop_already_reviewed")
        if candidate.status != CandidateStatus.READY_FOR_HUMAN_REVIEW:
            return IndependentReviewJobResult(status="noop_not_ready")

        request = self._repo.get_request(candidate.request_id)
        if request is None:
            raise LookupError(f"generation request {candidate.request_id} not found")

        now = self._clock.now()
        try:
            result = self._provider.review(candidate, request)
        except ReviewProviderTimeoutError as exc:
            return IndependentReviewJobResult(status="failed", error=f"timeout: {exc}")
        except ReviewProviderError as exc:
            return IndependentReviewJobResult(status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never let a malformed provider crash the worker
            return IndependentReviewJobResult(status="failed", error=f"unexpected error: {exc}")

        known_anchors = {
            (anchor.source_id, anchor.source_version, anchor.anchor_id) for anchor in request.approved_source_bundle
        }
        application = apply_independent_review(candidate, result, known_anchors=known_anchors, now=now)
        self._repo.save_candidate(application.candidate)
        return IndependentReviewJobResult(
            status="succeeded",
            agreement=application.candidate.independent_review_agreement,
            new_status=application.candidate.status.value,
            fabricated_anchor_warnings=application.fabricated_anchor_warnings,
        )


def new_generation_request(
    *,
    blueprint_id: str,
    slot_index: int,
    objective_ids: list[str],
    item_type,
    difficulty: str,
    marks: float,
    target_minutes: int,
    requested_variant_count: int,
    permitted_languages: list[str],
    approved_source_bundle: list[dict],
    deterministic_seed: int,
    created_by: str,
    now: datetime,
) -> GenerationRequest:
    return GenerationRequest(
        id=uuid4().hex,
        blueprint_id=blueprint_id,
        slot_index=slot_index,
        objective_ids=objective_ids,
        item_type=item_type,
        difficulty=difficulty,
        marks=marks,
        target_minutes=target_minutes,
        requested_variant_count=requested_variant_count,
        permitted_languages=permitted_languages,
        approved_source_bundle=approved_source_bundle,
        deterministic_seed=deterministic_seed,
        created_by=created_by,
        created_at=now,
    )
