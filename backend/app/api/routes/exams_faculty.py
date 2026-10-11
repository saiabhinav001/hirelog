"""Faculty exam routes (Prompt 3). Faculty preview (GET) is strictly
read-only — only the command endpoint mutates state, and every mutation
requires an explicit expected_version + idempotency_key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled, require_recent_staff_reauth
from app.api.exams_errors import raise_for_domain_error
from app.api.exams_scope import (
    authorize_definition,
    require_definition_scope,
    require_instructor_admin_scope,
    require_run_scope,
    resolve_new_definition_institution,
    validate_enrollment_scope,
)
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.finalization import ExamFinalizationSweeper
from app.domain.exams.institution_enrollment import InstructorAssignmentError, assign_instructors
from app.domain.exams.models import RunStatus
from app.domain.exams.policy import ExamPolicy
from app.domain.exams.repository import AttemptRepository
from app.domain.exams.short_links import ShortLink, ShortLinkError, ShortLinkService, format_code
from app.domain.generation.repository import GenerationRepository
from app.domain.live.publisher import publish_attempt_status_changed, publish_run_state_changed

router = APIRouter(
    prefix="/api/exams/v1/faculty",
    tags=["exams-faculty"],
    dependencies=[Depends(require_exams_enabled)],
)


def _service() -> ExamCommandService:
    return ExamCommandService(db)


class CreateDefinitionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    # Optional: defaults to the caller's only teaching institution; required
    # when they teach in several. Bootstrap/pilot teachers may omit it.
    institution_id: str | None = Field(default=None, min_length=1, max_length=128)


class PublishRevisionRequest(BaseModel):
    # Legacy path: the client states the slot count. Blueprint path (Prompt 3):
    # the count, total marks and approved candidates are derived server-side
    # and frozen into the revision; blockers refuse the publish.
    slot_count: int | None = Field(default=None, ge=1, le=500)
    blueprint_id: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _needs_a_source_of_slots(self):
        if self.slot_count is None and not self.blueprint_id:
            raise ValueError("slot_count or blueprint_id is required")
        return self


class CreateRunRequest(BaseModel):
    definition_id: str
    revision_id: str
    policy: ExamPolicy
    enrolled_uids: list[str] = Field(default_factory=list, max_length=5000)


class RunCommandRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    force: bool = False
    reason: str | None = Field(default=None, max_length=500)


class TimeExtensionRequest(BaseModel):
    extra_minutes: int = Field(ge=1, le=24 * 60)
    reason: str | None = Field(default=None, max_length=500)
    # Optional for backward compatibility; without it a retried request adds the
    # minutes again. Clients should always send one.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class AttemptCommandRequest(BaseModel):
    """Terminate / recover: destructive-or-recovery actions require a reason and an idempotency key."""

    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=500)


class AccessChangeRequest(BaseModel):
    uid: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class SetInstructorsRequest(BaseModel):
    # College IDs of linked FACULTY/ADMIN members of the definition's institution.
    identity_keys: list[str] = Field(default_factory=list, max_length=20)

    model_config = {"extra": "forbid"}


@router.post("/definitions")
def create_definition(payload: CreateDefinitionRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    institution_id = resolve_new_definition_institution(user, payload.institution_id)
    definition = _service().create_definition(
        title=payload.title, owner_uid=user["uid"], institution_id=institution_id
    )
    return definition.model_dump(mode="json")


@router.get("/definitions/{definition_id}/publish-preview")
def preview_publication(
    definition_id: str,
    blueprint_id: str,
    duration_minutes: int | None = None,
    user: dict = Depends(require_definition_scope),
) -> dict:
    """Read-only: resolved counts, total marks, blockers and warnings for
    publishing `blueprint_id` - shown to the teacher BEFORE publishing."""
    try:
        _, _, preview = _service().preview_publication(
            definition_id=definition_id, blueprint_id=blueprint_id, duration_minutes=duration_minutes
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return preview.to_dict()


@router.post("/definitions/{definition_id}/publish")
def publish_revision(
    definition_id: str, payload: PublishRevisionRequest, user: dict = Depends(require_definition_scope)
) -> dict:
    try:
        revision = _service().publish_revision(
            definition_id=definition_id,
            slot_count=payload.slot_count,
            published_by=user["uid"],
            blueprint_id=payload.blueprint_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return revision.model_dump(mode="json", exclude_none=True)


@router.post("/runs")
def create_run(payload: CreateRunRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    definition = authorize_definition(user, payload.definition_id)
    service = _service()
    revision = service.definitions.get_revision(payload.definition_id, payload.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    validate_enrollment_scope(definition.institution_id, payload.enrolled_uids)
    policy = payload.policy
    if policy.proctoring_profile is not None:
        # Provenance is the server's: a client-supplied value is overwritten.
        policy = policy.model_copy(update={"proctoring_profile": policy.proctoring_profile.model_copy(update={"set_by": user["uid"]})})
    run = service.create_run(
        definition_id=payload.definition_id,
        revision=revision,
        policy=policy,
        enrolled_uids=payload.enrolled_uids,
        institution_id=definition.institution_id,
        actor_uid=user["uid"],
    )
    return run.model_dump(mode="json")


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    """Read-only preview. Never mutates — Preview and Release are different
    actions (contract #4/Prompt 3)."""
    run = _service().runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return {**run.model_dump(mode="json"), "server_time": datetime.now(timezone.utc).isoformat()}


@router.post("/runs/{run_id}/commands/{command}")
def run_command(
    run_id: str,
    command: Literal["publish", "release", "pause", "resume", "close", "end"],
    payload: RunCommandRequest,
    user: dict = Depends(require_run_scope),
) -> dict:
    service = _service()
    try:
        outcome = service.execute(
            run_id=run_id,
            command=command,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            force=payload.force,
            actor_uid=user["uid"],
            reason=payload.reason,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if outcome.applied:
        updated_run = service.runs.get(run_id)
        if updated_run is not None:
            publish_run_state_changed(updated_run, now=datetime.now(timezone.utc))
    return {
        "applied": outcome.applied,
        "reused_existing_receipt": outcome.reused_existing_receipt,
        "receipt": outcome.receipt.model_dump(mode="json"),
    }


@router.post("/runs/{run_id}/attempts/{uid}/extend")
def extend_time(
    run_id: str, uid: str, payload: TimeExtensionRequest, user: dict = Depends(require_run_scope)
) -> dict:
    try:
        result = _service().grant_time_extension(
            run_id=run_id, uid=uid, extra_minutes=payload.extra_minutes, actor_uid=user["uid"],
            reason=payload.reason, idempotency_key=payload.idempotency_key,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not result.reused_existing_receipt:
        _publish_attempt(run_id, uid)
    return {
        "status": "extended", "run_id": run_id, "uid": uid, "extra_minutes": payload.extra_minutes,
        "deadline": result.deadline, "manual_extension_minutes": result.manual_extension_minutes,
        "reused_existing_receipt": result.reused_existing_receipt,
    }


def _publish_attempt(run_id: str, uid: str) -> None:
    updated_attempt = AttemptRepository(db).get(run_id, uid)
    if updated_attempt is not None:
        publish_attempt_status_changed(updated_attempt, now=datetime.now(timezone.utc))


@router.post("/runs/{run_id}/attempts/{uid}/terminate")
def terminate_attempt(
    run_id: str, uid: str, payload: AttemptCommandRequest, user: dict = Depends(require_run_scope)
) -> dict:
    """Seal one student's attempt now (EXPIRED + receipt + grading trigger).
    Requires a reason; never reopens anything."""
    try:
        result = _service().terminate_attempt(
            run_id=run_id, uid=uid, idempotency_key=payload.idempotency_key, actor_uid=user["uid"],
            reason=payload.reason,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not result.reused_existing_receipt:
        _publish_attempt(run_id, uid)
    return {"command": "terminate", "run_id": run_id, "uid": uid, "status": result.status,
            "reused_existing_receipt": result.reused_existing_receipt}


@router.post("/runs/{run_id}/attempts/{uid}/recover")
def recover_attempt(
    run_id: str, uid: str, payload: AttemptCommandRequest, user: dict = Depends(require_run_scope)
) -> dict:
    """Re-drive grading for an already-sealed attempt whose trigger was lost
    or failed. Idempotent downstream; never reopens an attempt."""
    try:
        result = _service().recover_attempt(
            run_id=run_id, uid=uid, idempotency_key=payload.idempotency_key, actor_uid=user["uid"],
            reason=payload.reason,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"command": "recover", "run_id": run_id, "uid": uid, "status": result.status,
            "reused_existing_receipt": result.reused_existing_receipt}


def _change_access(run_id: str, action: str, payload: AccessChangeRequest, user: dict) -> dict:
    service = _service()
    run = service.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    if action == "grant":
        validate_enrollment_scope(run.institution_id, [payload.uid])
    try:
        result = service.change_access(
            run_id=run_id, action=action, uid=payload.uid, idempotency_key=payload.idempotency_key,
            actor_uid=user["uid"], reason=payload.reason,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"command": action, "run_id": run_id, "uid": payload.uid, "changed": result.changed,
            "reused_existing_receipt": result.reused_existing_receipt}


@router.post("/runs/{run_id}/access/grant")
def grant_access(run_id: str, payload: AccessChangeRequest, user: dict = Depends(require_run_scope)) -> dict:
    """Add one student to the run (institution-scoped runs: active students of that institution only)."""
    return _change_access(run_id, "grant", payload, user)


@router.post("/runs/{run_id}/access/revoke")
def revoke_access(run_id: str, payload: AccessChangeRequest, user: dict = Depends(require_run_scope)) -> dict:
    """Remove one student who has no attempt yet; terminate an attempt instead of revoking."""
    return _change_access(run_id, "revoke", payload, user)


@router.post("/runs/{run_id}/finalize-expired")
def finalize_expired(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    """One bounded finalization pass for this run - same code the background
    sweeper runs. Only attempts past their OWN deadline are sealed."""
    sealed = ExamFinalizationSweeper(db).sweep_once(run_id, batch_size=settings.EXAM_FINALIZATION_BATCH_SIZE)
    return {"run_id": run_id, "sealed": sealed, "batch_size": settings.EXAM_FINALIZATION_BATCH_SIZE}


@router.get("/definitions")
def list_my_definitions(user: dict = Depends(require_exam_faculty)) -> dict:
    """'Assigned courses' screen data — definitions this faculty member owns or
    has been assigned to as a co-instructor."""
    repository = _service().definitions
    assigned = {d.id: d for d in repository.list_by_owner(user["uid"])}
    assigned.update({d.id: d for d in repository.list_by_instructor(user["uid"])})
    return {"results": [d.model_dump(mode="json") for d in assigned.values()]}


@router.get("/definitions/{definition_id}/instructors")
def get_instructors(definition_id: str, user: dict = Depends(require_definition_scope)) -> dict:
    definition = _service().definitions.get(definition_id)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Definition not found.")
    return {"definition_id": definition_id, "owner_uid": definition.owner_uid, "instructor_uids": definition.instructor_uids}


@router.put("/definitions/{definition_id}/instructors")
def set_instructors(
    definition_id: str,
    payload: SetInstructorsRequest,
    user: dict = Depends(require_instructor_admin_scope),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    """Course-scoped teacher assignment ("definition = course"): replaces the
    co-instructor list. Super-admin or ADMIN of the definition's institution only."""
    try:
        uids = assign_instructors(
            db, definition_id=definition_id, identity_keys=payload.identity_keys, actor_uid=user["uid"]
        )
    except InstructorAssignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if exc.code == "definition_not_found" else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "problems": exc.problems[:20]},
        ) from exc
    return {"definition_id": definition_id, "instructor_uids": uids}


@router.get("/definitions/{definition_id}/runs")
def list_runs_for_definition(definition_id: str, user: dict = Depends(require_definition_scope)) -> dict:
    runs = _service().runs.list_by_definition(definition_id)
    return {"results": [r.model_dump(mode="json") for r in runs]}


@router.get("/definitions/{definition_id}/blueprints")
def list_blueprints_for_definition(definition_id: str, user: dict = Depends(require_definition_scope)) -> dict:
    blueprints = GenerationRepository(db).list_blueprints_by_definition(definition_id)
    return {"results": [b.model_dump(mode="json") for b in blueprints]}


@router.get("/runs/{run_id}/attempts")
def list_attempts_for_run(run_id: str, user: dict = Depends(require_run_scope), limit: int = 100, cursor_uid: str | None = None) -> dict:
    """Candidate grid data — paginated (contract: 'pagination/virtualization')."""
    bounded_limit = max(1, min(limit, 200))
    attempts, next_cursor = AttemptRepository(db).list_page_for_run(run_id, limit=bounded_limit, cursor_uid=cursor_uid)
    return {
        "results": [a.model_dump(mode="json") for a in attempts],
        "next_cursor_uid": next_cursor,
        "server_time": datetime.now(timezone.utc).isoformat(),  # the freshness stamp the control room shows
    }


# -- short share links (Prompt 7, ADR 0023) ---------------------------------------------------------------

_short_link_issue_limiter = SlidingWindowLimiter(10, 600, namespace="exam_short_link_issue")


class ShortLinkRequest(BaseModel):
    regenerate: bool = False


def _short_link_view(link: ShortLink | None) -> dict | None:
    if link is None:
        return None
    return {"code": link.code, "display_code": format_code(link.code), "created_at": link.created_at.isoformat(), "created_by": link.created_by}


def _require_short_links_enabled() -> None:
    if not settings.EXAM_SHORT_LINKS_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "short_links_not_enabled", "message": "Short links are not enabled on this deployment."})


@router.get("/runs/{run_id}/short-link")
def get_short_link(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    """Current link for the run (a locator only - see the resolver for what it does not grant)."""
    if not settings.EXAM_SHORT_LINKS_ENABLED:
        return {"enabled": False, "link": None}
    return {"enabled": True, "link": _short_link_view(ShortLinkService(db).current(run_id))}


@router.post("/runs/{run_id}/short-link")
def issue_short_link(run_id: str, payload: ShortLinkRequest, user: dict = Depends(require_run_scope)) -> dict:
    """Create the run's short link, or with `regenerate` replace it (the old code stops working at once)."""
    _require_short_links_enabled()
    run = _service().runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    if run.status in (RunStatus.ENDED, RunStatus.CLOSED):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "run_not_admitting", "message": "This run no longer admits students."})
    _short_link_issue_limiter.check(user["uid"], detail="Too many short-link changes. Please wait before trying again.")
    try:
        link, created = ShortLinkService(db).issue(run_id, actor_uid=user["uid"], institution_id=run.institution_id, regenerate=payload.regenerate)
    except ShortLinkError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": exc.code, "message": str(exc)}) from exc
    return {"enabled": True, "link": _short_link_view(link), "created": created}


@router.post("/runs/{run_id}/short-link/revoke")
def revoke_short_link(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    _require_short_links_enabled()
    run = _service().runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    _short_link_issue_limiter.check(user["uid"], detail="Too many short-link changes. Please wait before trying again.")
    return {"enabled": True, "revoked": ShortLinkService(db).revoke(run_id, actor_uid=user["uid"], institution_id=run.institution_id)}
