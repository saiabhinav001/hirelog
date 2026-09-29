"""Generation job orchestration — durable and restart-safe (contract #11):
all state lives in Firestore on the `GenerationRequest`/candidates, so ANY
worker process can pick up `run_generation_job` after a crash; there is no
in-process-only state a restart would lose. Calling this function again on
an already-succeeded request is a safe no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from app.domain.exams.clock import Clock, system_clock
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


class GenerationJobRunner:
    def __init__(self, repo: GenerationRepository, provider: GenerationProvider, clock: Clock = system_clock) -> None:
        self._repo = repo
        self._provider = provider
        self._clock = clock

    def run(self, request_id: str) -> JobResult:
        request = self._repo.get_request(request_id)
        if request is None:
            raise LookupError(f"generation request {request_id} not found")

        if request.status == "succeeded":
            return JobResult(status="noop_already_done")

        now = self._clock.now()
        self._repo.update_request(request_id, {"status": "running", "attempts": request.attempts + 1})

        try:
            response = self._provider.generate(request)
        except ProviderTimeoutError as exc:
            self._repo.update_request(request_id, {"status": "failed", "last_error": f"timeout: {exc}"})
            return JobResult(status="failed", error=str(exc))
        except ProviderError as exc:
            self._repo.update_request(request_id, {"status": "failed", "last_error": str(exc)})
            return JobResult(status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never let a malformed provider crash the worker
            self._repo.update_request(request_id, {"status": "failed", "last_error": f"unexpected error: {exc}"})
            return JobResult(status="failed", error=str(exc))

        if response.status == "insufficient_source":
            self._repo.update_request(
                request_id,
                {
                    "status": "failed",
                    "last_error": "insufficient_source: " + "; ".join(response.issues) if response.issues else "insufficient_source",
                },
            )
            return JobResult(status="insufficient_source", shortfall=response.shortfall)

        previously_used = self._repo.previously_used_signatures(request.blueprint_id)
        signatures_seen: set[str] = set()
        ready_count = 0
        needs_revision_count = 0
        candidate_ids: list[str] = []

        for raw in response.candidates:
            candidate, issues = validate_candidate_dict(
                raw,
                request=request,
                now=now,
                previously_used_signatures=previously_used,
                signatures_seen_this_batch=signatures_seen,
            )
            if candidate is None:
                # Malformed provider JSON for this one candidate — record and
                # continue; one bad candidate must not sink the whole batch.
                self._repo.record_malformed_candidate(request_id, raw, issues)
                continue

            signatures_seen.add(candidate.uniqueness_signature)
            self._repo.save_candidate(candidate)
            candidate_ids.append(candidate.id)
            if candidate.status == CandidateStatus.READY_FOR_HUMAN_REVIEW:
                ready_count += 1
            else:
                needs_revision_count += 1

        shortfall = max(0, request.requested_variant_count - ready_count)
        self._repo.update_request(request_id, {"status": "succeeded"})
        return JobResult(
            status="succeeded",
            ready_count=ready_count,
            needs_revision_count=needs_revision_count,
            shortfall=shortfall,
            candidate_ids=candidate_ids,
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
