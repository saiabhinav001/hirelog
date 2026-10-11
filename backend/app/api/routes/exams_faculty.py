"""Faculty exam routes (Prompt 3). Faculty preview (GET) is strictly
read-only — only the command endpoint mutates state, and every mutation
requires an explicit expected_version + idempotency_key.

Every route is institution-scoped: the router-level guard resolves each path
entity (definition/run) to its owning institution and requires the caller to
be faculty THERE (or a platform operator). Records are stamped at creation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import can_act_as_faculty, require_exam_faculty, require_exams_enabled
from app.api.exams_errors import raise_for_domain_error
from app.api.exams_scope import (
    ATTEMPT_PAGE_MIN_REFRESH_SECONDS,
    assert_faculty_scope,
    attempt_pages,
    college_ids_by_uid,
    make_faculty_scope_guard,
    resolve_creation_institution,
    run_access,
)
from app.core import background
from app.core.firebase import db
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.policy import ExamPolicy
from app.domain.exams.repository import AttemptRepository
from app.domain.generation.repository import GenerationRepository
from app.domain.live.publisher import publish_attempt_status_changed, publish_run_state_changed

router = APIRouter(
    prefix="/api/exams/v1/faculty",
    tags=["exams-faculty"],
    dependencies=[Depends(require_exams_enabled), Depends(make_faculty_scope_guard())],
)


def _service() -> ExamCommandService:
    return ExamCommandService(db)


class CreateDefinitionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    institution_id: Optional[str] = Field(default=None, max_length=200)


class PublishRevisionRequest(BaseModel):
    slot_count: int = Field(ge=1, le=500)


class CreateRunRequest(BaseModel):
    definition_id: str
    revision_id: str
    policy: ExamPolicy
    # Pilot (institution-less) exams enrol raw uids. Institution exams may also
    # enrol by college ID, or the whole active student roster; every uid must
    # belong to an ACTIVE student of the exam's institution.
    enrolled_uids: list[str] = Field(default_factory=list, max_length=5000)
    enrolled_identity_keys: list[str] = Field(default_factory=list, max_length=5000)
    enroll_all_institution_students: bool = False


class RunCommandRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    force: bool = False


class TimeExtensionRequest(BaseModel):
    extra_minutes: int = Field(ge=1, le=24 * 60)


def _resolve_enrollment(institution_id: Optional[str], payload: CreateRunRequest) -> list[str]:
    uids = list(payload.enrolled_uids)
    if institution_id is None:
        if payload.enrolled_identity_keys or payload.enroll_all_institution_students:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "institution_required", "message": "Roster enrolment needs an institution exam."},
            )
        return uids

    from app.domain.institution.models import MembershipStatus, RoleScope
    from app.domain.institution.normalization import normalize_identity_key
    from app.domain.institution.repository import MembershipRepository

    students = {
        m.identity_key: m
        for m in MembershipRepository(db).list_by_institution(institution_id)
        if m.status == MembershipStatus.ACTIVE and m.role_scope == RoleScope.STUDENT
    }
    provisioned = {m.uid for m in students.values() if m.uid}
    if payload.enroll_all_institution_students:
        uids += sorted(provisioned)
    missing: list[str] = []
    for raw_key in payload.enrolled_identity_keys:
        key = normalize_identity_key(raw_key)
        member = students.get(key)
        if member is None or not member.uid:
            missing.append(key)
        else:
            uids.append(member.uid)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_identity_keys", "identity_keys": missing[:50], "count": len(missing)},
        )
    uids = list(dict.fromkeys(uids))
    not_students = [uid for uid in uids if uid not in provisioned]
    if not_students:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "uids_not_active_students", "count": len(not_students)},
        )
    return uids


@router.post("/definitions")
def create_definition(payload: CreateDefinitionRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    institution_id = resolve_creation_institution(user, payload.institution_id)
    definition = _service().create_definition(
        title=payload.title, owner_uid=user["uid"], institution_id=institution_id
    )
    return definition.model_dump(mode="json")


@router.post("/definitions/{definition_id}/publish")
def publish_revision(
    definition_id: str, payload: PublishRevisionRequest, user: dict = Depends(require_exam_faculty)
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
    service = _service()
    definition = service.definitions.get(payload.definition_id)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    assert_faculty_scope(user, definition.institution_id)
    revision = service.definitions.get_revision(payload.definition_id, payload.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    enrolled = _resolve_enrollment(definition.institution_id, payload)
    run = service.create_run(
        definition_id=payload.definition_id,
        revision=revision,
        policy=payload.policy,
        enrolled_uids=enrolled,
        institution_id=definition.institution_id,
    )
    return run.model_dump(mode="json")


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
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
    user: dict = Depends(require_exam_faculty),
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
        background.wake("outbox")
    return {
        "applied": outcome.applied,
        "reused_existing_receipt": outcome.reused_existing_receipt,
        "receipt": outcome.receipt.model_dump(mode="json"),
    }


@router.post("/runs/{run_id}/attempts/{uid}/extend")
def extend_time(
    run_id: str, uid: str, payload: TimeExtensionRequest, user: dict = Depends(require_exam_faculty)
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
    """'Assigned courses' screen data — definitions this faculty member owns
    (and may still act on: a lapsed membership hides its institution's records)."""
    definitions = [
        d for d in _service().definitions.list_by_owner(user["uid"]) if can_act_as_faculty(user, d.institution_id)
    ]
    return {"results": [d.model_dump(mode="json") for d in definitions]}


@router.get("/definitions/{definition_id}/runs")
def list_runs_for_definition(definition_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    runs = _service().runs.list_by_definition(definition_id)
    return {"results": [r.model_dump(mode="json") for r in runs]}


@router.get("/definitions/{definition_id}/blueprints")
def list_blueprints_for_definition(definition_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    blueprints = GenerationRepository(db).list_blueprints_by_definition(definition_id)
    return {"results": [b.model_dump(mode="json") for b in blueprints]}


@router.get("/runs/{run_id}/attempts")
def list_attempts_for_run(
    run_id: str,
    user: dict = Depends(require_exam_faculty),
    limit: int = 100,
    cursor_uid: str | None = None,
    fresh: bool = False,
) -> dict:
    """Candidate grid data — paginated (contract: 'pagination/virtualization').
    Served from a short-lived cache: a refetch burst (many live events, several
    tabs) must not re-read every attempt each time. Any committed attempt change
    drops the cache, and `fresh=true` forces a re-read when the cached page is at
    least ATTEMPT_PAGE_MIN_REFRESH_SECONDS old."""
    bounded_limit = max(1, min(limit, 200))
    key = (run_id, bounded_limit, cursor_uid)

    def load() -> dict:
        attempts, next_cursor = AttemptRepository(db).list_page_for_run(run_id, limit=bounded_limit, cursor_uid=cursor_uid)
        college_ids = college_ids_by_uid(run_access(run_id).institution_id)
        return {
            "results": [{**a.model_dump(mode="json"), "college_id": college_ids.get(a.uid)} for a in attempts],
            "next_cursor_uid": next_cursor,
        }

    if fresh:
        age = attempt_pages.age(key)
        if age is not None and age >= ATTEMPT_PAGE_MIN_REFRESH_SECONDS:
            attempt_pages.invalidate(key)
    return attempt_pages.get(key, load)
