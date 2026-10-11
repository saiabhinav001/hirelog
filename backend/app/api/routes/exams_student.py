"""Student exam routes (Prompt 4). Every mutating endpoint enforces ownership
(uid from the verified token, never a path/body-supplied uid), enrollment,
session epoch, window state and server cutoff — all inside the domain service.
Responses are no-store; the exam API client is expected to pin to one origin
(frontend concern, tracked for Prompt 7).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import (
    get_current_exam_user,
    require_exams_enabled,
    require_seb_admission,
    require_student_run_scope,
    resolve_principal,
)
from app.api.exams_errors import raise_for_conflict_reason, raise_for_domain_error
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.authz.policy import can_participate_as_student
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.short_links import ShortLinkService
from app.domain.exams.student_service import ExamStudentService
from app.domain.generation.public_content import build_public_content
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.live.publisher import publish_attempt_status_changed

router = APIRouter(prefix="/api/exams/v1/student", tags=["exams-student"], dependencies=[Depends(require_exams_enabled)])


def _service() -> ExamStudentService:
    return ExamStudentService(db)


_short_link_user_limiter = SlidingWindowLimiter(30, 60, namespace="exam_short_link_resolve")
_short_link_ip_limiter = SlidingWindowLimiter(120, 60, namespace="exam_short_link_resolve_ip")


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


class SaveDraftRequest(BaseModel):
    expected_version: int = Field(ge=0)
    answer: dict = Field(default_factory=dict)
    marked_for_review: bool | None = None  # None keeps the stored flag
    client_epoch: int | None = Field(default=None, ge=0)
    client_save_id: str | None = Field(default=None, min_length=1, max_length=100)


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
    if any(run.institution_id for run in runs):
        principal = resolve_principal(user)
        runs = [
            run for run in runs
            if can_participate_as_student(principal, run_institution_id=run.institution_id)
        ]
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


@router.get("/runs/{run_id}/accommodations")
def get_my_accommodations(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    """Own accommodation window only — never another student's (contract:
    'explicit, faculty-authored, never auto-derived'). `camera_waived`
    lets the client skip requesting camera permission entirely for a
    student with an institution-approved alternative, rather than
    prompting and then recording a permission-denied outcome."""
    _set_no_store(response)
    service = _service()
    try:
        service.get_state(run_id, user["uid"])  # enforces enrollment; result unused here
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    run = service.get_run_or_404(run_id)
    return {
        "extra_minutes": run.policy.extra_minutes_for(user["uid"]),
        "camera_waived": run.policy.camera_waived_for(user["uid"]),
    }


@router.get("/runs/{run_id}/state")
def get_state(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    """The attempt plus the server's clock reading and the run's time-warning thresholds: the client
    anchors its timer to `server_time` (see ADR 0023) and never to its own wall clock."""
    _set_no_store(response)
    service = _service()
    try:
        attempt = service.get_state(run_id, user["uid"])
        run = service.get_run_or_404(run_id)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {
        **attempt.model_dump(mode="json"),
        "server_time": service.now().isoformat(),
        "time_warning_minutes": run.policy.time_warning_minutes,
    }


@router.get("/short-links/{code}")
def resolve_short_link(code: str, request: Request, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Turns a short code into the run it names - for a signed-in student who is enrolled in that run, and
    nobody else. Unknown, revoked, malformed and not-yours all answer the same 404, so the code cannot be used
    to probe for runs. It authorizes nothing: admission and content keep their own checks."""
    _set_no_store(response)
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "short_link_not_found"})
    _short_link_user_limiter.check(user["uid"], detail="Too many attempts. Please wait a moment.")
    _short_link_ip_limiter.check(request.client.host if request.client else "unknown", detail="Too many attempts. Please wait a moment.")
    if not settings.EXAM_SHORT_LINKS_ENABLED:
        raise not_found
    link = ShortLinkService(db).resolve(code)
    run = _service().runs.get(link.run_id) if link else None
    if run is None or user["uid"] not in run.enrolled_uids:
        raise not_found
    if run.institution_id and not can_participate_as_student(resolve_principal(user), run_institution_id=run.institution_id):
        raise not_found
    return {"run_id": run.id}


@router.post("/runs/{run_id}/admit")
def admit(run_id: str, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
    _set_no_store(response)
    try:
        attempt = _service().admit(run_id, user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    publish_attempt_status_changed(attempt, now=datetime.now(timezone.utc))
    return attempt.model_dump(mode="json")


@router.get("/runs/{run_id}/question")
def get_current_question(run_id: str, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
    """Only the CURRENT slot is ever constructed — never a guessed/future
    question id, never locked content (contract #3)."""
    _set_no_store(response)
    return _question_response(run_id, user["uid"], None)


@router.get("/runs/{run_id}/questions/{slot_index}")
def get_released_question(run_id: str, slot_index: int, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
    """A slot the student was already shown. The current slot always works; an earlier one only when
    `EXAM_STUDENT_REVISIT_RELEASED_SLOTS` is on; a later one never (403 slot_not_released)."""
    _set_no_store(response)
    return _question_response(run_id, user["uid"], slot_index)


@router.get("/runs/{run_id}/slots")
def list_released_slots(run_id: str, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
    """Navigator data: released slots with answered / marked-for-review state and the count still locked.
    No question content."""
    _set_no_store(response)
    service = _service()
    run = service.get_run_or_404(run_id)
    revision = _revision_or_404(run)
    try:
        return service.list_released_slots(run_id, user["uid"], revision)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise


def _revision_or_404(run):
    revision = ExamCommandService(db).definitions.get_revision(run.definition_id, run.revision_id)
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found.")
    return revision


def _question_response(run_id: str, uid: str, slot_index: int | None) -> dict:
    service = _service()
    run = service.get_run_or_404(run_id)
    revision = _revision_or_404(run)
    try:
        question = service.get_question_for_slot(run_id, uid, revision, slot_index)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise

    assignments = VariantAssignmentRepository(db)
    detail = assignments.get_assignment_detail(run_id=run_id, uid=uid, slot_index=question.slot_index)
    candidate_id = (detail or {}).get("candidate_id")
    candidate = GenerationRepository(db).get_candidate(candidate_id) if candidate_id else None
    if candidate is not None:
        question = question.model_copy(
            update={"public_content": build_public_content(candidate, (detail or {}).get("option_order"))}
        )
    return question.model_dump(mode="json")


@router.post("/runs/{run_id}/next-question")
def advance_to_next_question(run_id: str, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
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
    user: dict = Depends(require_seb_admission),
) -> dict:
    _set_no_store(response)
    try:
        outcome = _service().save_draft(
            run_id, user["uid"], question_id, expected_version=payload.expected_version, answer=payload.answer,
            marked_for_review=payload.marked_for_review, client_epoch=payload.client_epoch,
            client_save_id=payload.client_save_id,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if not outcome.accepted:
        raise_for_conflict_reason(outcome.conflict_reason or "draft_version_conflict")
    return {"accepted": True, "version": outcome.new_version, "replayed": outcome.replayed}


@router.get("/runs/{run_id}/drafts/{question_id}")
def get_my_draft(run_id: str, question_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    """Re-hydrates the editor/answer UI after a refresh — never another
    student's draft (ownership is the verified token's own uid, not a
    path/body-supplied one) and only for a slot the student has been shown."""
    _set_no_store(response)
    try:
        draft = _service().get_draft(run_id, user["uid"], question_id)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    if draft is None:
        return {"version": 0, "answer": None, "marked_for_review": False}
    return {"version": draft.version, "answer": draft.answer, "marked_for_review": draft.marked_for_review}


@router.get("/runs/{run_id}/drafts")
def list_my_draft_versions(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    """Recovery endpoint for a refreshed/reopened tab — rebuilds the local
    `expected_draft_versions` map final-submit needs, without requiring the
    client to have remembered every slot's version across the whole session."""
    _set_no_store(response)
    versions = _service().drafts.all_versions(run_id, user["uid"])
    return {"versions": versions}


@router.post("/runs/{run_id}/submit")
def submit(run_id: str, payload: SubmitRequest, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
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
def finish(run_id: str, payload: FinishRequest, response: Response, user: dict = Depends(require_seb_admission)) -> dict:
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
def get_receipt(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
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
