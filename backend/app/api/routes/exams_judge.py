"""Student-facing public test run (quota'd, public tests only — never
prefetches or returns hidden-test data) and faculty grading/review routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import (
    authorize_student_for_run,
    get_current_exam_user,
    require_exam_faculty,
    require_exams_enabled,
    require_student_run_scope,
)
from app.api.exams_errors import raise_for_domain_error
from app.api.exams_scope import (
    filter_reviews_for_user,
    require_run_scope,
    require_short_answer_review_scope,
    require_submission_scope,
)
from app.core.firebase import exam_db as db
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.student_service import ExamStudentService
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.judge.converters import to_test_groups
from app.domain.judge.errors import JudgeUnavailableError
from app.domain.judge.grading import public_test_result_dto, sealed_grade_summary_dto
from app.domain.judge.models import SubmissionPurpose, SubmissionStatus
from app.domain.judge.repository import GradingResultRepository, ShortAnswerReviewRepository
from app.domain.judge.runtime_allowlist import configured_allowlist
from app.domain.judge.service import JudgeService


def _judge_provider_for_request():
    from app.exam_main import _judge_provider

    return _judge_provider()


student_router = APIRouter(
    prefix="/api/exams/v1/student/judge", tags=["exams-judge-student"], dependencies=[Depends(require_exams_enabled)]
)
faculty_router = APIRouter(
    prefix="/api/exams/v1/faculty/judge", tags=["exams-judge-faculty"], dependencies=[Depends(require_exams_enabled)]
)


class PublicRunRequest(BaseModel):
    run_id: str
    question_id: str
    language_key: str
    source_code: str = Field(max_length=200_000)


class ReviewShortAnswerRequest(BaseModel):
    score: float = Field(ge=0)
    rubric_notes: str = Field(max_length=2000)


class RegradeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class McqRegradeRequest(BaseModel):
    correct: bool
    reason: str = Field(min_length=1, max_length=500)


def _released_slot(run_id: str, uid: str, question_id: str, *, require_active: bool) -> int:
    """A guessed, malformed or not-yet-released question id is refused before any lookup."""
    try:
        return ExamStudentService(db).authorize_released_question(run_id, uid, question_id, require_active=require_active)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise


def _candidate_for_question(run_id: str, uid: str, question_id: str, *, require_active: bool):
    slot_index = _released_slot(run_id, uid, question_id, require_active=require_active)
    candidate_id = VariantAssignmentRepository(db).get_assignment(run_id=run_id, uid=uid, slot_index=slot_index)
    if not candidate_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No variant assigned for this question.")
    candidate = GenerationRepository(db).get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    return candidate


@student_router.post("/public-run")
def public_run(payload: PublicRunRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    authorize_student_for_run(user, payload.run_id)
    candidate = _candidate_for_question(payload.run_id, user["uid"], payload.question_id, require_active=True)
    service = JudgeService(db, _judge_provider_for_request())
    # Quota (HTTP 429) is enforced inside `run_public_test` by `SlidingWindowLimiter.check`,
    # which raises `HTTPException` directly — no translation needed here.
    try:
        submission = service.run_public_test(
            candidate_id=candidate.id, run_id=payload.run_id, uid=user["uid"], question_id=payload.question_id,
            language_key=payload.language_key, source_code=payload.source_code,
            public_tests=to_test_groups(candidate.public_tests), allowlist=configured_allowlist(),
            test_suite_version=candidate.version,
        )
        if submission.status == SubmissionStatus.INFRA_ERROR:
            # Say so plainly: nothing ran, and no result (pass or fail) is implied.
            raise JudgeUnavailableError("The code runner is not available right now. Your code was not run.")
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"submission_id": submission.id, "status": submission.status.value}



@student_router.get("/public-run/{submission_id}")
def get_public_run(submission_id: str, run_id: str, question_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    candidate = _candidate_for_question(run_id, user["uid"], question_id, require_active=False)
    service = JudgeService(db, _judge_provider_for_request())
    # Ownership is checked before anything is polled, and the submission must be this student's own test run
    # for this very question.
    existing = service.get_submission(submission_id)
    if (
        existing is None or existing.uid != user["uid"] or existing.run_id != run_id
        or existing.question_id != question_id or existing.purpose != SubmissionPurpose.PUBLIC_RUN
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")
    submission = service.poll_public_run(submission_id, public_tests=to_test_groups(candidate.public_tests))
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")
    return {
        "status": submission.status.value,
        "results": [public_test_result_dto(r) for r in submission.public_run_results] if submission.public_run_results else [],
    }


@faculty_router.get("/grading-results/{submission_id}")
def get_grading_result(submission_id: str, user: dict = Depends(require_submission_scope)) -> dict:
    result = GradingResultRepository(db).latest_for_submission(submission_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No grading result yet.")
    return sealed_grade_summary_dto(result)


@faculty_router.post("/grading-results/{submission_id}/regrade")
def regrade(submission_id: str, payload: RegradeRequest, user: dict = Depends(require_submission_scope)) -> dict:
    result = GradingResultRepository(db).latest_for_submission(submission_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No grading result yet.")
    candidate = GenerationRepository(db).get_candidate(result.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    all_groups = to_test_groups(candidate.public_tests) + to_test_groups(candidate.hidden_test_groups)
    service = JudgeService(db, _judge_provider_for_request())
    new_version = service.regrade(
        submission_id, all_test_groups=all_groups, allowlist=configured_allowlist(),
        reviewer=user["uid"], reason=payload.reason,
    )
    if new_version is None:
        return {"status": "processing"}
    return sealed_grade_summary_dto(new_version)


@faculty_router.get("/runs/{run_id}/stalled-submissions")
def list_stalled_submissions(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    """Sealed-grade submissions the judge could not finish. They are listed here and stay PENDING in the gradebook;
    none has been scored."""
    stalled = JudgeService(db, _judge_provider_for_request()).list_stalled(run_id)
    return {
        "results": [
            {
                "submission_id": s.id, "uid": s.uid, "question_id": s.question_id, "language_key": s.language_key,
                "attempts": s.attempts, "reason": s.last_error, "updated_at": s.updated_at.isoformat(),
            }
            for s in stalled
        ]
    }


@faculty_router.post("/runs/{run_id}/requeue-stalled")
def requeue_stalled_submissions(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    """Sends a run's stalled submissions back to the judge with a fresh attempt budget; audited with the actor."""
    requeued = JudgeService(db, _judge_provider_for_request()).requeue_stalled(run_id)
    if requeued:
        AuthzAuditRepository(db).append(build_audit_event(
            "exam_judge_requeue", actor_uid=user["uid"], detail={"run_id": run_id, "count": len(requeued), "submission_ids": requeued[:50]},
        ))
    return {"requeued": len(requeued)}


@faculty_router.post("/mcq-results/{run_id}/{uid}/{question_id}/regrade")
def regrade_mcq(run_id: str, uid: str, question_id: str, payload: McqRegradeRequest, user: dict = Depends(require_run_scope)) -> dict:
    """Explicit override — e.g. a defective-question resolution or an
    upheld grade appeal. Always a new version; the original automatic
    exact-match result is never edited (Prompt 9)."""
    service = JudgeService(db, _judge_provider_for_request())
    try:
        new_version = service.regrade_mcq(
            run_id=run_id, uid=uid, question_id=question_id, reviewer=user["uid"],
            correct=payload.correct, reason=payload.reason,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return new_version.model_dump(mode="json")


@faculty_router.get("/short-answer-reviews/pending")
def list_pending_short_answer_reviews(user: dict = Depends(require_exam_faculty)) -> dict:
    reviews = filter_reviews_for_user(user, ShortAnswerReviewRepository(db).list_pending())
    return {"results": [r.model_dump(mode="json") for r in reviews]}


@faculty_router.post("/short-answer-reviews/{review_id}")
def review_short_answer(review_id: str, payload: ReviewShortAnswerRequest, user: dict = Depends(require_short_answer_review_scope)) -> dict:
    service = JudgeService(db, _judge_provider_for_request())
    try:
        review = service.submit_short_answer_review(
            review_id, score=payload.score, rubric_notes=payload.rubric_notes, reviewer=user["uid"]
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return review.model_dump(mode="json")
