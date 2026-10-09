"""Faculty exam routes (Prompt 3). Faculty preview (GET) is strictly
read-only — only the command endpoint mutates state, and every mutation
requires an explicit expected_version + idempotency_key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

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
from app.core.firebase import db
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.institution_enrollment import InstructorAssignmentError, assign_instructors
from app.domain.exams.policy import ExamPolicy
from app.domain.exams.repository import AttemptRepository
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
    slot_count: int = Field(ge=1, le=500)


class CreateRunRequest(BaseModel):
    definition_id: str
    revision_id: str
    policy: ExamPolicy
    enrolled_uids: list[str] = Field(default_factory=list, max_length=5000)


class RunCommandRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    force: bool = False


class TimeExtensionRequest(BaseModel):
    extra_minutes: int = Field(ge=1, le=24 * 60)


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


@router.post("/definitions/{definition_id}/publish")
def publish_revision(
    definition_id: str, payload: PublishRevisionRequest, user: dict = Depends(require_definition_scope)
) -> dict:
    try:
        revision = _service().publish_revision(
            definition_id=definition_id, slot_count=payload.slot_count, published_by=user["uid"]
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return revision.model_dump(mode="json")


@router.post("/runs")
def create_run(payload: CreateRunRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    definition = authorize_definition(user, payload.definition_id)
    service = _service()
    revision = service.definitions.get_revision(payload.definition_id, payload.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    validate_enrollment_scope(definition.institution_id, payload.enrolled_uids)
    run = service.create_run(
        definition_id=payload.definition_id,
        revision=revision,
        policy=payload.policy,
        enrolled_uids=payload.enrolled_uids,
        institution_id=definition.institution_id,
    )
    return run.model_dump(mode="json")


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    """Read-only preview. Never mutates — Preview and Release are different
    actions (contract #4/Prompt 3)."""
    run = _service().runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return run.model_dump(mode="json")


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
        _service().grant_time_extension(run_id=run_id, uid=uid, extra_minutes=payload.extra_minutes)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    updated_attempt = AttemptRepository(db).get(run_id, uid)
    if updated_attempt is not None:
        publish_attempt_status_changed(updated_attempt, now=datetime.now(timezone.utc))
    return {"status": "extended", "run_id": run_id, "uid": uid, "extra_minutes": payload.extra_minutes}


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
    return {"results": [a.model_dump(mode="json") for a in attempts], "next_cursor_uid": next_cursor}
