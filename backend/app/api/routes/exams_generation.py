"""Faculty blueprint/generation routes. Generation always runs the
deterministic fake provider in this phase — no real provider is configured
(see docs/adr/0009 "setup requirements"). No student PII ever appears in a
`GenerationRequest` — the schema has no field for it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled
from app.core.config import settings
from app.core.firebase import db
from app.domain.generation.commands import GenerationCommandService
from app.domain.generation.jobs import GenerationJobRunner, new_generation_request
from app.domain.generation.models import BlueprintSlot, ExamBlueprint
from app.domain.generation.provider import DeterministicFakeProvider, HttpGenerationProviderAdapter
from app.domain.generation.repository import GenerationRepository
from app.domain.kt.models import ApprovedSource
from app.domain.kt.repository import ApprovedSourceRepository

router = APIRouter(
    prefix="/api/exams/v1/faculty",
    tags=["exams-generation"],
    dependencies=[Depends(require_exams_enabled)],
)


def _generation_provider():
    """Real provider only if BOTH base_url and api_key are configured — an
    institutional decision, never a code change (contract: "surface missing
    institutional/provider inputs as bounded setup requirements")."""
    if settings.EXAM_GENERATION_PROVIDER_BASE_URL and settings.EXAM_GENERATION_PROVIDER_API_KEY:
        return HttpGenerationProviderAdapter(
            base_url=settings.EXAM_GENERATION_PROVIDER_BASE_URL, api_key=settings.EXAM_GENERATION_PROVIDER_API_KEY
        )
    return DeterministicFakeProvider()


class CreateBlueprintRequest(BaseModel):
    exam_definition_id: str
    title: str = Field(min_length=1, max_length=200)
    slots: list[BlueprintSlot] = Field(min_length=1)


class SourceAnchorInput(BaseModel):
    source_id: str
    anchor_id: str


class CreateGenerationRequestPayload(BaseModel):
    blueprint_id: str
    slot_index: int = Field(ge=0)
    source_anchors: list[SourceAnchorInput] = Field(min_length=0)
    deterministic_seed: int = 0


class ReviewCandidateRequest(BaseModel):
    decision: Literal["approve", "reject"]
    rejection_reason: str | None = None


class ReserveVariantsRequest(BaseModel):
    run_id: str
    uids: list[str] = Field(min_length=1, max_length=5000)
    allow_balanced_reuse: bool = False


@router.post("/blueprints")
def create_blueprint(payload: CreateBlueprintRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    now = datetime.now(timezone.utc)
    blueprint = ExamBlueprint(
        id=uuid4().hex, exam_definition_id=payload.exam_definition_id, title=payload.title,
        slots=payload.slots, created_by=user["uid"], created_at=now, updated_at=now,
    )
    GenerationRepository(db).create_blueprint(blueprint)
    return blueprint.model_dump(mode="json")


@router.get("/blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    blueprint = GenerationRepository(db).get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="blueprint not found")
    return blueprint.model_dump(mode="json")


@router.get("/blueprints/{blueprint_id}/candidates")
def list_candidates_for_blueprint(blueprint_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    """Question/test review screen data — every candidate for this
    blueprint, any status, regardless of which generation request made it."""
    candidates = GenerationRepository(db).list_candidates_by_blueprint(blueprint_id)
    return {"results": [c.model_dump(mode="json") for c in candidates]}


def _resolve_source_bundle(anchors: list[SourceAnchorInput]) -> list[dict]:
    """Only APPROVED sources may ever be cited — this is re-checked here,
    not trusted from the request, and again inside `validation.py`."""
    sources_repo = ApprovedSourceRepository(db)
    bundle: list[dict] = []
    for item in anchors:
        source: ApprovedSource | None = sources_repo.get(item.source_id)
        if source is None or source.review_status.value != "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"source {item.source_id} is not an approved source",
            )
        extracted_version_number = 1  # this phase always parses to version 1 (see kt/repository.py)
        bundle.append(
            {
                "source_id": source.id,
                "source_version": extracted_version_number,
                "anchor_id": item.anchor_id,
                "excerpt": f"[excerpt for anchor {item.anchor_id}]",
            }
        )
    return bundle


@router.post("/generation-requests")
def create_and_run_generation_request(
    payload: CreateGenerationRequestPayload, user: dict = Depends(require_exam_faculty)
) -> dict:
    repo = GenerationRepository(db)
    blueprint = repo.get_blueprint(payload.blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="blueprint not found")
    slot = next((s for s in blueprint.slots if s.slot_index == payload.slot_index), None)
    if slot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="slot not found on blueprint")

    now = datetime.now(timezone.utc)
    bundle = _resolve_source_bundle(payload.source_anchors)
    request = new_generation_request(
        blueprint_id=blueprint.id, slot_index=slot.slot_index, objective_ids=slot.objective_ids,
        item_type=slot.item_type, difficulty=slot.difficulty, marks=slot.marks,
        target_minutes=slot.target_minutes, requested_variant_count=slot.requested_variant_count,
        permitted_languages=slot.permitted_languages, approved_source_bundle=bundle,
        deterministic_seed=payload.deterministic_seed, created_by=user["uid"], now=now,
    )
    repo.create_request(request)

    runner = GenerationJobRunner(repo, _generation_provider())
    result = runner.run(request.id)
    return {
        "request_id": request.id,
        "status": result.status,
        "ready_count": result.ready_count,
        "needs_revision_count": result.needs_revision_count,
        "shortfall": result.shortfall,
        "candidate_ids": result.candidate_ids,
        "error": result.error,
    }


@router.get("/generation-requests/{request_id}")
def get_generation_request(request_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    request = GenerationRepository(db).get_request(request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return request.model_dump(mode="json")


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    candidate = GenerationRepository(db).get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    return candidate.model_dump(mode="json")


@router.post("/candidates/{candidate_id}/review")
def review_candidate(candidate_id: str, payload: ReviewCandidateRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    service = GenerationCommandService(db)
    try:
        outcome = service.review_candidate(
            candidate_id, decision=payload.decision, reviewer=user["uid"], rejection_reason=payload.rejection_reason
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not outcome.accepted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=outcome.reason)
    return outcome.candidate.model_dump(mode="json")


@router.post("/blueprints/{blueprint_id}/slots/{slot_index}/reserve-variants")
def reserve_variants_for_slot(
    blueprint_id: str, slot_index: int, payload: ReserveVariantsRequest, user: dict = Depends(require_exam_faculty)
) -> dict:
    service = GenerationCommandService(db)
    result = service.reserve_variants_for_slot(
        run_id=payload.run_id, blueprint_id=blueprint_id, slot_index=slot_index,
        uids=payload.uids, allow_balanced_reuse=payload.allow_balanced_reuse,
    )
    if result.blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.reason)
    return {"assignments": result.assignments}
