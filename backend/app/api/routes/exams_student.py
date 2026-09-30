"""Student exam routes (Prompt 4). Every mutating endpoint enforces ownership
(uid from the verified token, never a path/body-supplied uid), enrollment,
session epoch, window state and server cutoff — all inside the domain service.
Responses are no-store; the exam API client is expected to pin to one origin
(frontend concern, tracked for Prompt 7).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import get_current_exam_user
from app.api.exams_errors import raise_for_conflict_reason, raise_for_domain_error
from app.core.firebase import db
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.student_service import ExamStudentService
from app.domain.generation.public_content import build_public_content
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.live.publisher import publish_attempt_status_changed

router = APIRouter(prefix="/api/exams/v1/student", tags=["exams-student"])


def _service() -> ExamStudentService:
    return ExamStudentService(db)


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


class SaveDraftRequest(BaseModel):
    expected_version: int = Field(ge=0)
    answer: dict = Field(default_factory=dict)


class SubmitRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    client_epoch: int = Field(ge=0)
    expected_draft_versions: dict[str, int] = Field(default_factory=dict)


class FinishRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    client_epoch: int = Field(ge=0)


@router.get("/runs")
def list_my_runs(response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """'Assigned exams' screen data — every run this student is enrolled in,
    plus their own attempt status/deadline if one exists."""
    _set_no_store(response)
    service = _service()
    runs = service.runs.list_for_student(user["uid"])
    results = []
    for run in runs:
        attempt = service.attempts.get(run.id, user["uid"])
        results.append({
            "run_id": run.id,
            "definition_id": run.definition_id,
            "status": run.status.value,
            "version": run.version,
            "released_at": run.released_at.isoformat() if run.released_at else None,
            "attempt_status": attempt.status.value if attempt else None,
            "deadline": attempt.deadline.isoformat() if attempt and attempt.deadline else None,
        })
    return {"results": results}


@router.get("/runs/{run_id}/state")
def get_state(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    try:
        attempt = _service().get_state(run_id, user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return attempt.model_dump(mode="json")


@router.post("/runs/{run_id}/admit")
def admit(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    try:
        attempt = _service().admit(run_id, user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    publish_attempt_status_changed(attempt, now=datetime.now(timezone.utc))
    return attempt.model_dump(mode="json")


@router.get("/runs/{run_id}/question")
def get_current_question(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Only the CURRENT slot is ever constructed — never a guessed/future
    question id, never locked content (contract #3)."""
    _set_no_store(response)
    service = _service()
    run = service.get_run_or_404(run_id)
    revision = ExamCommandService(db).definitions.get_revision(run.definition_id, run.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    try:
        question = service.get_current_question(run_id, user["uid"], revision)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise

    candidate_id = VariantAssignmentRepository(db).get_assignment(
        run_id=run_id, uid=user["uid"], slot_index=question.slot_index
    )
    candidate = GenerationRepository(db).get_candidate(candidate_id) if candidate_id else None
    if candidate is not None:
        question = question.model_copy(update={"public_content": build_public_content(candidate)})
    return question.model_dump(mode="json")


@router.post("/runs/{run_id}/next-question")
def advance_to_next_question(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Forward-only — there is no corresponding 'previous question' route
    (contract: 'Keyboard Back/Next cannot access locked or rewrite submitted
    content')."""
    _set_no_store(response)
    service = _service()
    run = service.get_run_or_404(run_id)
    revision = ExamCommandService(db).definitions.get_revision(run.definition_id, run.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    try:
        outcome = service.advance_to_next_question(run_id, user["uid"], revision)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not outcome.accepted:
        raise_for_conflict_reason(outcome.conflict_reason or "no_further_questions")
    return {"accepted": True, "slot_index": outcome.new_slot_index}


@router.patch("/runs/{run_id}/drafts/{question_id}")
def save_draft(
    run_id: str,
    question_id: str,
    payload: SaveDraftRequest,
    response: Response,
    user: dict = Depends(get_current_exam_user),
) -> dict:
    _set_no_store(response)
    try:
        outcome = _service().save_draft(
            run_id, user["uid"], question_id, expected_version=payload.expected_version, answer=payload.answer
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not outcome.accepted:
        raise_for_conflict_reason(outcome.conflict_reason or "draft_version_conflict")
    return {"accepted": True, "version": outcome.new_version}


@router.get("/runs/{run_id}/drafts/{question_id}")
def get_my_draft(run_id: str, question_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Re-hydrates the editor/answer UI after a refresh — never another
    student's draft (ownership is the verified token's own uid, not a
    path/body-supplied one)."""
    _set_no_store(response)
    draft = _service().drafts.get(run_id, user["uid"], question_id)
    if draft is None:
        return {"version": 0, "answer": None}
    return {"version": draft.version, "answer": draft.answer}


@router.get("/runs/{run_id}/drafts")
def list_my_draft_versions(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Recovery endpoint for a refreshed/reopened tab — rebuilds the local
    `expected_draft_versions` map final-submit needs, without requiring the
    client to have remembered every slot's version across the whole session."""
    _set_no_store(response)
    versions = _service().drafts.all_versions(run_id, user["uid"])
    return {"versions": versions}


@router.post("/runs/{run_id}/submit")
def submit(run_id: str, payload: SubmitRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    service = _service()
    try:
        outcome = service.submit(
            run_id,
            user["uid"],
            idempotency_key=payload.idempotency_key,
            client_epoch=payload.client_epoch,
            expected_draft_versions=payload.expected_draft_versions,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not outcome.accepted:
        raise_for_conflict_reason(outcome.conflict_reason or "submit_conflict")
    updated_attempt = service.attempts.get(run_id, user["uid"])
    if updated_attempt is not None:
        publish_attempt_status_changed(updated_attempt, now=datetime.now(timezone.utc))
    return {"accepted": True, "receipt": outcome.receipt.model_dump(mode="json")}


@router.post("/runs/{run_id}/finish")
def finish(run_id: str, payload: FinishRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    service = _service()
    try:
        outcome = service.finish(
            run_id, user["uid"], idempotency_key=payload.idempotency_key, client_epoch=payload.client_epoch
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not outcome.accepted:
        raise_for_conflict_reason(outcome.conflict_reason or "submit_conflict")
    updated_attempt = service.attempts.get(run_id, user["uid"])
    if updated_attempt is not None:
        publish_attempt_status_changed(updated_attempt, now=datetime.now(timezone.utc))
    return {"accepted": True, "receipt": outcome.receipt.model_dump(mode="json")}


@router.get("/runs/{run_id}/receipt")
def get_receipt(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Works even after the deadline — a committed receipt is always
    reconcilable; it never authorizes new/late work (contract #4)."""
    _set_no_store(response)
    try:
        receipt = _service().get_receipt(run_id, user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if receipt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No receipt yet — attempt not finalized.")
    return receipt.model_dump(mode="json")
