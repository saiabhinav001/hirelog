"""Student-facing public test run (quota'd, public tests only — never
prefetches or returns hidden-test data) and faculty grading/review routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

import re

from app.api.exams_dependencies import (
    get_current_exam_user,
    require_exam_faculty,
    require_exams_enabled,
)
from app.api.exams_errors import raise_for_domain_error
from app.api.exams_scope import assert_student_run_access, make_faculty_scope_guard, run_institution, visible_institution_ids
from app.core.firebase import db
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.judge.converters import to_test_groups
from app.domain.judge.grading import public_test_result_dto, sealed_grade_summary_dto
from app.domain.judge.repository import GradingResultRepository, ShortAnswerReviewRepository
from app.domain.judge.runtime_allowlist import default_allowlist
from app.domain.judge.service import JudgeService


def _judge_provider_for_request():
    from app.exam_main import _judge_provider

    return _judge_provider()


student_router = APIRouter(
    prefix="/api/exams/v1/student/judge", tags=["exams-judge-student"], dependencies=[Depends(require_exams_enabled)]
)
faculty_router = APIRouter(
    prefix="/api/exams/v1/faculty/judge", tags=["exams-judge-faculty"],
    dependencies=[Depends(require_exams_enabled), Depends(make_faculty_scope_guard())],
)

_QUESTION_ID = re.compile(r"^[0-9a-f]{32}:slot:[0-9]{1,3}$")


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


def _candidate_for_question(run_id: str, uid: str, question_id: str):
    if not _QUESTION_ID.match(question_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid question id.")
    slot_index = int(question_id.rsplit(":slot:", 1)[1])
    candidate_id = VariantAssignmentRepository(db).get_assignment_cached(run_id=run_id, uid=uid, slot_index=slot_index)
    if not candidate_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No variant assigned for this question.")
    candidate = GenerationRepository(db).get_candidate_cached(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    return candidate


@student_router.post("/public-run")
def public_run(payload: PublicRunRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    assert_student_run_access(user, payload.run_id)
    candidate = _candidate_for_question(payload.run_id, user["uid"], payload.question_id)
    service = JudgeService(db, _judge_provider_for_request())
    # Quota (HTTP 429) is enforced inside `run_public_test` by `SlidingWindowLimiter.check`,
    # which raises `HTTPException` directly — no translation needed here.
    try:
        submission = service.run_public_test(
            candidate_id=candidate.id, run_id=payload.run_id, uid=user["uid"], question_id=payload.question_id,
            language_key=payload.language_key, source_code=payload.source_code,
            public_tests=to_test_groups(candidate.public_tests), allowlist=default_allowlist(),
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"submission_id": submission.id, "status": submission.status.value}



@student_router.get("/public-run/{submission_id}")
def get_public_run(submission_id: str, run_id: str, question_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    assert_student_run_access(user, run_id)
    candidate = _candidate_for_question(run_id, user["uid"], question_id)
    service = JudgeService(db, _judge_provider_for_request())
    submission = service.poll_public_run(submission_id, public_tests=to_test_groups(candidate.public_tests))
    if submission is None or submission.uid != user["uid"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")
    return {
        "status": submission.status.value,
        "results": [public_test_result_dto(r) for r in submission.public_run_results] if submission.public_run_results else [],
    }


@faculty_router.get("/grading-results/{submission_id}")
def get_grading_result(submission_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    result = GradingResultRepository(db).latest_for_submission(submission_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No grading result yet.")
    return sealed_grade_summary_dto(result)


@faculty_router.post("/grading-results/{submission_id}/regrade")
def regrade(submission_id: str, payload: RegradeRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    result = GradingResultRepository(db).latest_for_submission(submission_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No grading result yet.")
    candidate = GenerationRepository(db).get_candidate(result.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    all_groups = to_test_groups(candidate.public_tests) + to_test_groups(candidate.hidden_test_groups)
    service = JudgeService(db, _judge_provider_for_request())
    new_version = service.regrade(
        submission_id, all_test_groups=all_groups, allowlist=default_allowlist(),
        reviewer=user["uid"], reason=payload.reason,
    )
    if new_version is None:
        return {"status": "processing"}
    return sealed_grade_summary_dto(new_version)


@faculty_router.post("/mcq-results/{run_id}/{uid}/{question_id}/regrade")
def regrade_mcq(run_id: str, uid: str, question_id: str, payload: McqRegradeRequest, user: dict = Depends(require_exam_faculty)) -> dict:
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
    visible = visible_institution_ids(user)
    reviews = ShortAnswerReviewRepository(db).list_pending()
    if visible is not None:
        in_scope = []
        for review in reviews:
            try:
                if run_institution(review.run_id) in visible:
                    in_scope.append(review)
            except LookupError:
                continue
        reviews = in_scope
    return {"results": [r.model_dump(mode="json") for r in reviews]}


@faculty_router.post("/short-answer-reviews/{review_id}")
def review_short_answer(review_id: str, payload: ReviewShortAnswerRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    service = JudgeService(db, _judge_provider_for_request())
    try:
        review = service.submit_short_answer_review(
            review_id, score=payload.score, rubric_notes=payload.rubric_notes, reviewer=user["uid"]
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return review.model_dump(mode="json")
