"""Faculty blueprint/generation routes (Prompt 5, ADR 0021).

Generation is grounded: only APPROVED sources, resolved to their pinned extracted version, with
the real page/section anchors and excerpts (never placeholders), and never any student data -
`GenerationRequest` has no field for it. Source text is data, not instructions. The model cannot
approve or publish; those are teacher actions (`review`), and any substantive edit makes a new
version and resets approval. Nothing here is called while a student is sitting an exam.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled
from app.api.exams_scope import (
    authorize_blueprint,
    authorize_definition,
    authorize_run,
    authorize_source,
    require_blueprint_scope,
    require_candidate_scope,
    require_definition_scope,
    require_generation_request_scope,
    require_run_scope,
    validate_variant_targets,
)
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.domain.exams.repository import ExamDefinitionRepository
from app.domain.generation.blueprint_spec import BlueprintSpec, expand_spec
from app.domain.generation.capacity import compute_capacity
from app.domain.generation.commands import GenerationCommandService
from app.domain.generation.edit import CandidateEditError
from app.domain.generation.grounding import GroundedBundle, GroundingError, build_bundle, terms_from
from app.domain.generation.independent_review_provider import (
    DeterministicFakeReviewProvider,
    HttpIndependentReviewProviderAdapter,
)
from app.domain.generation.jobs import GenerationJobRunner, IndependentReviewJobRunner, new_generation_request
from app.domain.generation.models import BlueprintSlot, CandidateStatus, ExamBlueprint, ItemType
from app.domain.generation.provider import DeterministicFakeProvider, HttpGenerationProviderAdapter
from app.domain.generation.repository import GenerationRepository
from app.domain.generation.topics import suggest_topics
from app.domain.judge.runtime_allowlist import configured_allowlist
from app.domain.kt.repository import ApprovedSourceRepository, UploadedSourceRepository

router = APIRouter(
    prefix="/api/exams/v1/faculty",
    tags=["exams-generation"],
    dependencies=[Depends(require_exams_enabled)],
)

MAX_BLUEPRINT_SLOTS = 500
MAX_SLOTS_PER_GENERATE_CALL = 5


def _generation_provider():
    """Real provider only if BOTH base_url and api_key are configured - an institutional decision,
    never a code change (contract: surface missing provider inputs as bounded setup requirements)."""
    if settings.EXAM_GENERATION_PROVIDER_BASE_URL and settings.EXAM_GENERATION_PROVIDER_API_KEY:
        return HttpGenerationProviderAdapter(
            base_url=settings.EXAM_GENERATION_PROVIDER_BASE_URL, api_key=settings.EXAM_GENERATION_PROVIDER_API_KEY
        )
    return DeterministicFakeProvider()


def _independent_review_provider():
    """A separate, independently configurable provider/endpoint (Backend model prompt B)."""
    if settings.EXAM_REVIEW_PROVIDER_BASE_URL and settings.EXAM_REVIEW_PROVIDER_API_KEY:
        return HttpIndependentReviewProviderAdapter(
            base_url=settings.EXAM_REVIEW_PROVIDER_BASE_URL, api_key=settings.EXAM_REVIEW_PROVIDER_API_KEY
        )
    return DeterministicFakeReviewProvider()


def _unprocessable(code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": code, "message": message, **extra})


# --- request bodies ---------------------------------------------------------------------------


class CreateBlueprintRequest(BaseModel):
    exam_definition_id: str
    title: str = Field(min_length=1, max_length=200)
    slots: Optional[list[BlueprintSlot]] = Field(default=None, min_length=1, max_length=MAX_BLUEPRINT_SLOTS)
    spec: Optional[BlueprintSpec] = None  # alternative to `slots`: expanded server-side
    topics: list[str] = Field(default_factory=list, max_length=50)
    topics_confirmed: bool = False
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    language_allowlist: list[str] = Field(default_factory=list, max_length=20)
    shuffle_options: bool = False
    variant_policy: Literal["unique", "balanced_reuse"] = "unique"


class SourceAnchorInput(BaseModel):
    source_id: str
    anchor_id: Optional[str] = None  # omitted/"auto" -> the server picks the most relevant approved passages


class CreateGenerationRequestPayload(BaseModel):
    blueprint_id: str
    slot_index: int = Field(ge=0)
    source_anchors: list[SourceAnchorInput] = Field(default_factory=list, max_length=200)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    deterministic_seed: int = 0


class GenerateBlueprintPayload(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=20)
    slot_indexes: Optional[list[int]] = Field(default=None, max_length=MAX_BLUEPRINT_SLOTS)
    deterministic_seed: int = 0


class RegeneratePayload(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=20)


class ReviewCandidateRequest(BaseModel):
    decision: Literal["approve", "reject"]
    rejection_reason: Optional[str] = None


class EditCandidateRequest(BaseModel):
    changes: dict[str, Any] = Field(min_length=1)


class ShortfallRequest(BaseModel):
    accepted: bool
    reason: Optional[str] = Field(default=None, max_length=500)


class ReserveVariantsRequest(BaseModel):
    run_id: str
    uids: list[str] = Field(min_length=1, max_length=5000)
    allow_balanced_reuse: bool = False


class AssignRunVariantsRequest(BaseModel):
    allow_balanced_reuse: bool = False


class TopicSuggestionsRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=20)


# --- blueprints ---------------------------------------------------------------------------------


@router.post("/blueprints")
def create_blueprint(payload: CreateBlueprintRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_definition(user, payload.exam_definition_id)
    if (payload.slots is None) == (payload.spec is None):
        raise _unprocessable("slots_or_spec", "Provide exactly one of `slots` or `spec`.")
    try:
        slots = list(payload.slots) if payload.slots is not None else expand_spec(payload.spec)
    except ValueError as exc:
        raise _unprocessable("invalid_spec", str(exc)) from exc

    if sorted(s.slot_index for s in slots) != list(range(len(slots))):
        raise _unprocessable("slot_indexes", "Slot indexes must be unique and contiguous from 0.")
    if not settings.EXAM_NEGATIVE_MARKING_ENABLED and any(slot.negative_marks > 0 for slot in slots):
        raise _unprocessable(
            "negative_marking_not_enabled", "Negative marking is not enabled on this deployment (grading does not apply it yet)."
        )
    if any(slot.negative_marks > slot.marks for slot in slots):
        raise _unprocessable("negative_marks_exceed_marks", "A slot's negative marks cannot exceed its marks.")

    known_languages = {entry.language_key for entry in configured_allowlist()}
    allowlist = list(dict.fromkeys(payload.language_allowlist))
    bad = sorted(set(allowlist) - known_languages)
    if bad:
        raise _unprocessable("unknown_languages", f"Languages not supported by the judge: {bad}", languages=bad)
    used = {lang for slot in slots for lang in slot.permitted_languages}
    bad = sorted(used - known_languages)
    if bad:
        raise _unprocessable("unknown_languages", f"Languages not supported by the judge: {bad}", languages=bad)
    if allowlist and sorted(used - set(allowlist)):
        raise _unprocessable("languages_outside_allowlist", "A coding slot uses a language outside the exam allowlist.")
    if any(s.item_type == ItemType.CODING and not s.permitted_languages for s in slots):
        raise _unprocessable("coding_needs_language", "Coding slots need at least one permitted language.")

    topics = [t.strip() for t in payload.topics if t.strip()]
    if topics and not payload.topics_confirmed:
        raise _unprocessable("topics_not_confirmed", "Topics are only stored once the teacher has confirmed them.")

    now = datetime.now(timezone.utc)
    blueprint = ExamBlueprint(
        id=uuid4().hex, exam_definition_id=payload.exam_definition_id, title=payload.title, slots=slots,
        topics=topics, topics_confirmed_by=user["uid"] if topics else None, duration_minutes=payload.duration_minutes,
        language_allowlist=allowlist, shuffle_options=payload.shuffle_options, variant_policy=payload.variant_policy,
        created_by=user["uid"], created_at=now, updated_at=now,
    )
    GenerationRepository(db).create_blueprint(blueprint)
    return blueprint.model_dump(mode="json")


@router.get("/blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, user: dict = Depends(require_blueprint_scope)) -> dict:
    blueprint = GenerationRepository(db).get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="blueprint not found")
    return blueprint.model_dump(mode="json")


@router.get("/blueprints/{blueprint_id}/candidates")
def list_candidates_for_blueprint(blueprint_id: str, user: dict = Depends(require_blueprint_scope)) -> dict:
    """Question/test review screen data - every candidate for this blueprint, any status."""
    candidates = GenerationRepository(db).list_candidates_by_blueprint(blueprint_id)
    return {"results": [c.model_dump(mode="json") for c in candidates]}


@router.get("/blueprints/{blueprint_id}/capacity")
def get_blueprint_capacity(blueprint_id: str, user: dict = Depends(require_blueprint_scope)) -> dict:
    """requested / generated / valid / approved per slot, computed from stored candidates."""
    repo = GenerationRepository(db)
    blueprint = repo.get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="blueprint not found")
    return compute_capacity(blueprint, repo.list_candidates_by_blueprint(blueprint_id), repo.list_requests_by_blueprint(blueprint_id)).to_dict()


@router.post("/blueprints/{blueprint_id}/shortfall")
def set_shortfall(blueprint_id: str, payload: ShortfallRequest, user: dict = Depends(require_blueprint_scope)) -> dict:
    """Explicitly accept (or withdraw) publishing with fewer approved variants than requested."""
    if payload.accepted and not (payload.reason or "").strip():
        raise _unprocessable("reason_required", "Accepting a shortfall needs a reason.")
    GenerationCommandService(db).set_shortfall_acceptance(
        blueprint_id, accepted=payload.accepted, actor=user["uid"], reason=(payload.reason or "").strip() or None
    )
    blueprint = GenerationRepository(db).get_blueprint(blueprint_id)
    return {"blueprint_id": blueprint_id, "shortfall_accepted": bool(blueprint and blueprint.shortfall_accepted)}


# --- grounded generation ------------------------------------------------------------------------


def _selections(source_anchors: list[SourceAnchorInput], source_ids: list[str]) -> dict[str, Optional[list[str]]]:
    selections: dict[str, Optional[list[str]]] = {sid: None for sid in source_ids}
    for item in source_anchors:
        if item.anchor_id and item.anchor_id != "auto":
            existing = selections.get(item.source_id)
            selections[item.source_id] = [*(existing or []), item.anchor_id]
        else:
            selections.setdefault(item.source_id, None)
    return selections


def _build_grounded_bundle(user: dict, selections: dict[str, Optional[list[str]]], blueprint: ExamBlueprint, slot: BlueprintSlot) -> GroundedBundle:
    """Approved sources only, each at its pinned extracted version. Re-checked here, never trusted from the request."""
    sources_repo = ApprovedSourceRepository(db)
    uploads_repo = UploadedSourceRepository(db)
    resolved = []
    for source_id, anchors in selections.items():
        authorize_source(user, source_id)
        source = sources_repo.get(source_id)
        if source is None or source.review_status.value != "approved":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"source {source_id} is not an approved source")
        version = (
            uploads_repo.get_extracted_version(source.upload_id, source.extracted_version_id)
            if source.upload_id and source.extracted_version_id else None
        )
        if version is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"source {source_id} has no extracted content to ground on"
            )
        resolved.append((source, version, anchors))
    try:
        return build_bundle(resolved, terms=terms_from(slot.objective_ids, blueprint.topics))
    except GroundingError as exc:
        raise _unprocessable(exc.code, exc.message, **exc.detail) from exc


def _run_generation(
    user: dict, blueprint: ExamBlueprint, slot: BlueprintSlot, selections: dict[str, Optional[list[str]]], *,
    seed: int, regenerates_candidate_id: Optional[str] = None,
) -> dict:
    repo = GenerationRepository(db)
    bundle = _build_grounded_bundle(user, selections, blueprint, slot)
    now = datetime.now(timezone.utc)
    request = new_generation_request(
        blueprint_id=blueprint.id, slot_index=slot.slot_index, objective_ids=slot.objective_ids,
        item_type=slot.item_type, difficulty=slot.difficulty, marks=slot.marks, target_minutes=slot.target_minutes,
        requested_variant_count=slot.requested_variant_count, permitted_languages=slot.permitted_languages,
        approved_source_bundle=[a.model_dump(mode="json") for a in bundle.anchors], deterministic_seed=seed,
        created_by=user["uid"], now=now, family_constraints=slot.family_constraints,
    ).model_copy(update={
        "source_versions": bundle.source_versions, "excluded_anchors": bundle.excluded, "bundle_sha256": bundle.sha256,
        "shuffle_options": blueprint.shuffle_options, "regenerates_candidate_id": regenerates_candidate_id,
    })
    repo.create_request(request)
    return _execute_request(repo, request.id, slot.slot_index)


def _execute_request(repo: GenerationRepository, request_id: str, slot_index: int) -> dict:
    result = GenerationJobRunner(repo, _generation_provider()).run(request_id)

    # Independent review runs for every candidate that cleared validation. It can only pull one back to
    # NEEDS_REVISION - it never advances anything. A review failure never blocks this response: the
    # teacher still sees the candidate awaiting human review.
    review_runner = IndependentReviewJobRunner(repo, _independent_review_provider())
    independent_review_results = []
    for candidate_id in result.candidate_ids:
        candidate = repo.get_candidate(candidate_id)
        if candidate is None or candidate.status != CandidateStatus.READY_FOR_HUMAN_REVIEW:
            continue
        review_result = review_runner.run(candidate_id)
        independent_review_results.append({
            "candidate_id": candidate_id, "status": review_result.status, "agreement": review_result.agreement,
            "new_status": review_result.new_status, "error": review_result.error,
        })
    stored = repo.get_request(request_id)
    return {
        "request_id": request_id, "slot_index": slot_index, "status": result.status, "ready_count": result.ready_count,
        "needs_revision_count": result.needs_revision_count, "shortfall": result.shortfall,
        "candidate_ids": result.candidate_ids, "error": result.error,
        "independent_review_results": independent_review_results,
        "excluded_anchors": stored.excluded_anchors if stored else [],
        "source_versions": stored.source_versions if stored else {},
    }


@router.post("/generation-requests")
def create_and_run_generation_request(
    payload: CreateGenerationRequestPayload, user: dict = Depends(require_exam_faculty)
) -> dict:
    blueprint = authorize_blueprint(user, payload.blueprint_id)
    slot = next((s for s in blueprint.slots if s.slot_index == payload.slot_index), None)
    if slot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="slot not found on blueprint")
    selections = _selections(payload.source_anchors, payload.source_ids)
    if not selections:
        raise _unprocessable("no_sources", "Choose at least one approved source.")
    for source_id in selections:
        authorize_source(user, source_id)  # explicit here (and re-checked when the bundle is built)
    return _run_generation(user, blueprint, slot, selections, seed=payload.deterministic_seed)


@router.post("/blueprints/{blueprint_id}/generate")
def generate_for_blueprint(blueprint_id: str, payload: GenerateBlueprintPayload, user: dict = Depends(require_blueprint_scope)) -> dict:
    """Runs generation for the slots that still lack requested capacity, a few per call (bounded latency).
    Call again to continue; `remaining_slots` lists what is left."""
    repo = GenerationRepository(db)
    blueprint = repo.get_blueprint(blueprint_id)
    if blueprint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="blueprint not found")
    capacity = compute_capacity(blueprint, repo.list_candidates_by_blueprint(blueprint_id))
    needed = {c.slot_index for c in capacity.slots if c.valid < c.requested}
    wanted = [s for s in sorted(blueprint.slots, key=lambda s: s.slot_index)
              if s.slot_index in needed and (payload.slot_indexes is None or s.slot_index in payload.slot_indexes)]
    selections = _selections([], payload.source_ids)
    results = [
        _run_generation(user, blueprint, slot, selections, seed=payload.deterministic_seed + slot.slot_index)
        for slot in wanted[:MAX_SLOTS_PER_GENERATE_CALL]
    ]
    return {"results": results, "remaining_slots": [s.slot_index for s in wanted[MAX_SLOTS_PER_GENERATE_CALL:]]}


@router.get("/generation-requests/{request_id}")
def get_generation_request(request_id: str, user: dict = Depends(require_generation_request_scope)) -> dict:
    request = GenerationRepository(db).get_request(request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return request.model_dump(mode="json")


@router.post("/generation-requests/{request_id}/retry")
def retry_generation_request(request_id: str, user: dict = Depends(require_generation_request_scope)) -> dict:
    """Retry a failed request (bounded by EXAM_GENERATION_MAX_ATTEMPTS). A succeeded one is a no-op."""
    repo = GenerationRepository(db)
    request = repo.get_request(request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return _execute_request(repo, request_id, request.slot_index)


# --- candidates ------------------------------------------------------------------------------------


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, user: dict = Depends(require_candidate_scope)) -> dict:
    candidate = GenerationRepository(db).get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    return candidate.model_dump(mode="json")


@router.post("/candidates/{candidate_id}/review")
def review_candidate(candidate_id: str, payload: ReviewCandidateRequest, user: dict = Depends(require_candidate_scope)) -> dict:
    try:
        outcome = GenerationCommandService(db).review_candidate(
            candidate_id, decision=payload.decision, reviewer=user["uid"], rejection_reason=payload.rejection_reason
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not outcome.accepted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=outcome.reason)
    return outcome.candidate.model_dump(mode="json")


@router.post("/candidates/{candidate_id}/edit")
def edit_candidate(candidate_id: str, payload: EditCandidateRequest, user: dict = Depends(require_candidate_scope)) -> dict:
    """A substantive edit creates a NEW version (old one superseded) and resets approval; a minor edit
    (wording of explanation/tags) keeps the status. Re-validated either way."""
    try:
        outcome = GenerationCommandService(db).edit_candidate(candidate_id, payload.changes, editor=user["uid"])
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CandidateEditError as exc:
        code = status.HTTP_409_CONFLICT if exc.code in {"candidate_in_published_revision", "candidate_not_editable"} else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail={"code": exc.code, "message": str(exc)}) from exc
    return {"kind": outcome.kind, "candidate": outcome.candidate.model_dump(mode="json")}


@router.post("/candidates/{candidate_id}/regenerate")
def regenerate_candidate(candidate_id: str, payload: RegeneratePayload, user: dict = Depends(require_candidate_scope)) -> dict:
    """Ask for fresh candidates for the same slot, re-grounded on the same sources. The original is kept
    until the teacher rejects it; its wording is fed back so the new ones differ."""
    repo = GenerationRepository(db)
    candidate = repo.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    blueprint = authorize_blueprint(user, candidate.blueprint_id)
    slot = next((s for s in blueprint.slots if s.slot_index == candidate.slot_index), None)
    if slot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="slot not found on blueprint")
    selections = _selections([], payload.source_ids)
    if not selections:
        original = repo.get_request(candidate.request_id)
        for anchor in (original.approved_source_bundle if original else []):
            selections.setdefault(anchor.source_id, None)
    if not selections:
        raise _unprocessable("no_sources", "Choose at least one approved source.")
    return _run_generation(
        user, blueprint, slot, selections, seed=random.SystemRandom().randrange(2**31), regenerates_candidate_id=candidate_id
    )


# --- variants -------------------------------------------------------------------------------------


@router.post("/blueprints/{blueprint_id}/slots/{slot_index}/reserve-variants")
def reserve_variants_for_slot(
    blueprint_id: str, slot_index: int, payload: ReserveVariantsRequest, user: dict = Depends(require_blueprint_scope)
) -> dict:
    run = authorize_run(user, payload.run_id)
    validate_variant_targets(run, payload.uids)
    result = GenerationCommandService(db).reserve_variants_for_slot(
        run_id=payload.run_id, blueprint_id=blueprint_id, slot_index=slot_index, uids=payload.uids,
        allow_balanced_reuse=payload.allow_balanced_reuse or run.policy.variant_policy == "balanced_reuse",
    )
    if result.blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.reason)
    return {"assignments": result.assignments}


@router.post("/runs/{run_id}/variants/assign")
def assign_run_variants(run_id: str, payload: AssignRunVariantsRequest, user: dict = Depends(require_run_scope)) -> dict:
    """Assign every enrolled student a variant (and a seeded option order) for every slot, BEFORE they
    start, from the candidates pinned in the run's revision. Idempotent: re-running only fills gaps."""
    from app.domain.exams.models import RunStatus

    run = authorize_run(user, run_id)
    if run.status in (RunStatus.ENDED, RunStatus.CLOSED):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_already_ended")
    revision = ExamDefinitionRepository(db).get_revision(run.definition_id, run.revision_id)
    if revision is None or not revision.slots:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run revision has no blueprint-derived slots")
    report = GenerationCommandService(db).assign_run_variants(
        run=run, revision=revision, uids=list(run.enrolled_uids),
        allow_balanced_reuse=payload.allow_balanced_reuse or run.policy.variant_policy == "balanced_reuse"
        or revision.variant_policy == "balanced_reuse",
        shuffle_options=bool(revision.shuffle_options), actor=user["uid"],
    )
    return report.to_dict()


# --- topics ---------------------------------------------------------------------------------------


@router.post("/definitions/{definition_id}/topic-suggestions")
def topic_suggestions(definition_id: str, payload: TopicSuggestionsRequest, user: dict = Depends(require_definition_scope)) -> dict:
    """Extractive suggestions from approved source text (headings + frequent terms). Suggestions only:
    nothing is stored until the teacher confirms topics on the blueprint."""
    sources_repo = ApprovedSourceRepository(db)
    uploads_repo = UploadedSourceRepository(db)
    gathered = []
    for source_id in dict.fromkeys(payload.source_ids):
        authorize_source(user, source_id)
        source = sources_repo.get(source_id)
        if source is None or source.review_status.value != "approved" or not source.upload_id or not source.extracted_version_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"source {source_id} is not an approved source")
        version = uploads_repo.get_extracted_version(source.upload_id, source.extracted_version_id)
        if version is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"source {source_id} has no extracted content")
        gathered.append((source_id, version))
    return {"suggestions": [s.to_dict() for s in suggest_topics(gathered)]}
