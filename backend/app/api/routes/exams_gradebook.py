"""Gradebook, similarity review, export, and learning-feedback routes.

Student routes only ever resolve `user["uid"]` from the auth token — never a
URL/body-supplied uid — so a student can only ever see their own row/
feedback/appeals. Every result route requires an existing release
(`ResultsNotReleasedError` -> 403) — there is no path for a student to see
grades before faculty release them, released or not, regrade or not.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled, require_student_run_scope
from app.api.exams_errors import raise_for_domain_error
from app.api.exams_scope import require_export_job_scope, require_grade_appeal_scope, require_run_scope
from app.api.routes.exams_proctoring import _storage
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.domain.exams.repository import ExamRunRepository
from app.domain.gradebook.aggregation_service import GradebookAggregationService
from app.domain.gradebook.export_service import GradebookExportService
from app.domain.gradebook.feedback_provider import HttpFeedbackProviderAdapter
from app.domain.gradebook.feedback_service import FeedbackService, TemplateFeedbackProvider
from app.domain.gradebook.models import ExportFormat
from app.domain.gradebook.release_service import GradebookReleaseService
from app.domain.gradebook.similarity_service import SimilarityService

student_router = APIRouter(
    prefix="/api/exams/v1/student/gradebook", tags=["exams-gradebook-student"],
    dependencies=[Depends(require_exams_enabled)],
)
faculty_router = APIRouter(
    prefix="/api/exams/v1/faculty/gradebook", tags=["exams-gradebook-faculty"],
    dependencies=[Depends(require_exams_enabled)],
)


def _similarity_provider():
    from app.exam_main import _similarity_provider as provider_factory

    return provider_factory()


def _feedback_provider():
    """Real provider only if BOTH base_url and api_key are configured — same
    institutional-decision pattern as every other provider in this codebase."""
    if settings.EXAM_FEEDBACK_PROVIDER_BASE_URL and settings.EXAM_FEEDBACK_PROVIDER_API_KEY:
        return HttpFeedbackProviderAdapter(
            base_url=settings.EXAM_FEEDBACK_PROVIDER_BASE_URL, api_key=settings.EXAM_FEEDBACK_PROVIDER_API_KEY
        )
    return TemplateFeedbackProvider()


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


class ReleaseRequest(BaseModel):
    # Required (non-empty) from the second release on: the reason for the correction.
    note: str = Field(default="", max_length=1000)
    # Needed to release while the run is open, attempts are live, or answers are not yet graded.
    acknowledge_incomplete: bool = False


class DefectiveQuestionRequest(BaseModel):
    resolution: Literal["full_credit_all", "exclude_from_total"]
    reason: str = Field(min_length=1, max_length=1000)


class GradeAppealRequest(BaseModel):
    student_statement: str = Field(min_length=1, max_length=4000)


class ResolveGradeAppealRequest(BaseModel):
    uphold: bool
    resolution_notes: str = Field(default="", max_length=1000)


class SimilarityRunRequest(BaseModel):
    uids: Optional[list[str]] = None  # defaults to the full enrolled cohort


class ExportRequest(BaseModel):
    format: Literal["csv", "json"] = "csv"


# ── Student ──────────────────────────────────────────────────────────────────

@student_router.get("/runs/{run_id}/results")
def get_my_results(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    """The student's frozen row for the latest release (never the live gradebook), without internal grading
    provenance. Nothing at all before a release."""
    _set_no_store(response)
    try:
        row, release = GradebookReleaseService(db).student_result(run_id, user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"row": row.model_dump(mode="json"), "release_version": release.version}


@student_router.get("/runs/{run_id}/feedback")
def get_my_feedback(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    _set_no_store(response)
    try:
        feedback = FeedbackService(db, _feedback_provider()).build_feedback_for_student(run_id, user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return feedback.model_dump(mode="json")


@student_router.post("/runs/{run_id}/questions/{question_id}/appeals")
def submit_grade_appeal(run_id: str, question_id: str, payload: GradeAppealRequest, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    _set_no_store(response)
    try:
        appeal = GradebookReleaseService(db).submit_grade_appeal(
            run_id=run_id, uid=user["uid"], question_id=question_id, student_statement=payload.student_statement
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return appeal.model_dump(mode="json")


@student_router.get("/runs/{run_id}/appeals")
def list_my_grade_appeals(run_id: str, response: Response, user: dict = Depends(require_student_run_scope)) -> dict:
    _set_no_store(response)
    appeals = GradebookReleaseService(db).list_grade_appeals_for_student(run_id, user["uid"])
    return {"results": [a.model_dump(mode="json") for a in appeals]}


# ── Faculty: gradebook rows / counts ────────────────────────────────────────

@faculty_router.get("/runs/{run_id}/rows")
def list_gradebook_rows(run_id: str, user: dict = Depends(require_run_scope), limit: int = 50, cursor_uid: str | None = None) -> dict:
    bounded_limit = max(1, min(limit, 200))
    try:
        rows, next_cursor = GradebookAggregationService(db).list_gradebook_page(run_id, limit=bounded_limit, cursor_uid=cursor_uid)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"results": [r.model_dump(mode="json") for r in rows], "next_cursor_uid": next_cursor}


@faculty_router.get("/runs/{run_id}/counts")
def get_gradebook_counts(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    try:
        counts = GradebookAggregationService(db).compute_all_counts(run_id)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"results": [c.model_dump(mode="json") for c in counts]}


# ── Faculty: release versioning ─────────────────────────────────────────────

@faculty_router.post("/runs/{run_id}/release")
def release_results(run_id: str, payload: ReleaseRequest, user: dict = Depends(require_run_scope)) -> dict:
    try:
        release = GradebookReleaseService(db).release_results(
            run_id=run_id, released_by=user["uid"], note=payload.note, acknowledge_incomplete=payload.acknowledge_incomplete,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return release.model_dump(mode="json")


@faculty_router.get("/runs/{run_id}/releases")
def list_releases(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    releases = GradebookReleaseService(db).releases.list_for_run(run_id)
    return {"results": [r.model_dump(mode="json") for r in sorted(releases, key=lambda r: r.version)]}


# ── Faculty: defective-question resolution ──────────────────────────────────

@faculty_router.post("/runs/{run_id}/questions/{question_id}/defective")
def resolve_defective_question(run_id: str, question_id: str, payload: DefectiveQuestionRequest, user: dict = Depends(require_run_scope)) -> dict:
    try:
        _run, revision = GradebookAggregationService(db).get_run_and_revision(run_id)
        if question_id not in {f"{revision.id}:slot:{index}" for index in range(revision.slot_count)}:
            raise LookupError(f"question {question_id} is not part of run {run_id}")
        record = GradebookReleaseService(db).resolve_defective_question(
            run_id=run_id, question_id=question_id, resolution=payload.resolution, reason=payload.reason, resolved_by=user["uid"],
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return record.model_dump(mode="json")


@faculty_router.get("/runs/{run_id}/defective")
def list_defective_resolutions(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    records = GradebookReleaseService(db).list_defective_resolutions(run_id)
    return {"results": [r.model_dump(mode="json") for r in records]}

# ── Faculty: grade appeals ───────────────────────────────────────────────────

@faculty_router.get("/runs/{run_id}/appeals")
def list_grade_appeals(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    appeals = GradebookReleaseService(db).list_grade_appeals_for_run(run_id)
    return {"results": [a.model_dump(mode="json") for a in appeals]}


@faculty_router.post("/appeals/{appeal_id}/resolve")
def resolve_grade_appeal(appeal_id: str, payload: ResolveGradeAppealRequest, user: dict = Depends(require_grade_appeal_scope)) -> dict:
    try:
        appeal = GradebookReleaseService(db).resolve_grade_appeal(
            appeal_id=appeal_id, uphold=payload.uphold, resolved_by=user["uid"], resolution_notes=payload.resolution_notes,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return appeal.model_dump(mode="json")


# ── Faculty: similarity review ───────────────────────────────────────────────

@faculty_router.post("/runs/{run_id}/questions/{question_id}/similarity")
def run_similarity_check(run_id: str, question_id: str, payload: SimilarityRunRequest, user: dict = Depends(require_run_scope)) -> dict:
    uids = payload.uids
    if uids is None:
        run = ExamRunRepository(db).get(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        uids = run.enrolled_uids
    service = SimilarityService(db, _similarity_provider())
    matches = service.run_for_question(run_id=run_id, question_id=question_id, uids=uids)
    return {
        "results": [m.model_dump(mode="json") for m in matches],
        "disclaimer": "Similarity matches are a review aid for authorized faculty only — never an automatic misconduct finding or score penalty.",
    }


@faculty_router.get("/runs/{run_id}/questions/{question_id}/similarity")
def list_similarity_matches(run_id: str, question_id: str, user: dict = Depends(require_run_scope)) -> dict:
    matches = SimilarityService(db, _similarity_provider()).list_matches(run_id, question_id)
    return {"results": [m.model_dump(mode="json") for m in matches]}


@faculty_router.get("/runs/{run_id}/similarity")
def list_similarity_matches_for_run(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    matches = SimilarityService(db, _similarity_provider()).list_matches_for_run(run_id)
    return {"results": [m.model_dump(mode="json") for m in matches]}


# ── Faculty: export ──────────────────────────────────────────────────────────

@faculty_router.post("/runs/{run_id}/export")
def create_export(run_id: str, payload: ExportRequest, user: dict = Depends(require_run_scope)) -> dict:
    service = GradebookExportService(db, _storage(), retention_hours=settings.EXAM_EXPORT_RETENTION_HOURS)
    export_format = ExportFormat.CSV if payload.format == "csv" else ExportFormat.JSON
    job = service.create_job(run_id=run_id, requested_by=user["uid"], export_format=export_format)
    try:
        job = service.run_job(job.id)
    except Exception:
        # The job record itself already reflects FAILED — surface its
        # current (failed) state rather than a raw 500, so the caller can
        # poll/display it like any other job outcome.
        job = service.jobs.get(job.id)
    return job.model_dump(mode="json")


@faculty_router.get("/export/{job_id}")
def get_export_job(job_id: str, user: dict = Depends(require_export_job_scope)) -> dict:
    job = GradebookExportService(db, _storage()).jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export job not found.")
    return job.model_dump(mode="json")


@faculty_router.post("/export/{job_id}/download-ticket")
def issue_export_download_ticket(job_id: str, user: dict = Depends(require_export_job_scope)) -> dict:
    service = GradebookExportService(db, _storage())
    try:
        ticket = service.issue_download_ticket(
            job_id=job_id, issued_to=user["uid"], ttl_seconds=settings.EXAM_EXPORT_DOWNLOAD_TICKET_TTL_SECONDS,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return {"download_ticket": ticket.id, "expires_at": ticket.expires_at.isoformat()}


@faculty_router.get("/export/download/{ticket_id}")
def download_export(ticket_id: str, response: Response, user: dict = Depends(require_exam_faculty)):
    _set_no_store(response)
    from fastapi import Response as RawResponse

    service = GradebookExportService(db, _storage())
    try:
        content, job = service.download(ticket_id=ticket_id, requested_by=user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    media_type = "text/csv" if job.format == ExportFormat.CSV else "application/json"
    filename = f"gradebook-{job.run_id}-v{job.id[:8]}.{job.format.value}"
    return RawResponse(
        content=content, media_type=media_type,
        headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{filename}"'},
    )
