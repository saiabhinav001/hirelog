"""Faculty exam routes (Prompt 3). Faculty preview (GET) is strictly
read-only — only the command endpoint mutates state, and every mutation
requires an explicit expected_version + idempotency_key.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled
from app.api.exams_errors import raise_for_domain_error
from app.core.firebase import db
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.policy import ExamPolicy

router = APIRouter(
    prefix="/api/exams/v1/faculty",
    tags=["exams-faculty"],
    dependencies=[Depends(require_exams_enabled)],
)


def _service() -> ExamCommandService:
    return ExamCommandService(db)


class CreateDefinitionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


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


@router.post("/definitions")
def create_definition(payload: CreateDefinitionRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    definition = _service().create_definition(title=payload.title, owner_uid=user["uid"])
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
    revision = service.definitions.get_revision(payload.definition_id, payload.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    run = service.create_run(
        definition_id=payload.definition_id,
        revision=revision,
        policy=payload.policy,
        enrolled_uids=payload.enrolled_uids,
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
    try:
        outcome = _service().execute(
            run_id=run_id,
            command=command,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            force=payload.force,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
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
    return {"status": "extended", "run_id": run_id, "uid": uid, "extra_minutes": payload.extra_minutes}
