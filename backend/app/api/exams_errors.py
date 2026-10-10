"""Maps exam domain exceptions to HTTP responses. Kept in one place so every
exam route returns a consistent shape for the same underlying condition."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.domain.exams.errors import (
    CommandRejectedError,
    ExamDomainError,
    IllegalTransitionError,
    NotEnrolledError,
    PublicationBlockedError,
    UnresolvedAttemptsError,
    VersionConflictError,
)
from app.domain.gradebook.errors import ExportJobNotReadyError, ExportTicketInvalidError, GradebookDomainError, ResultsNotReleasedError
from app.domain.judge.errors import JudgeDomainError, RuntimeNotAllowedError, SubmissionNotFoundError
from app.domain.proctoring.errors import ProctoringDomainError, UploadTicketInvalidError, ViewTicketInvalidError


def raise_for_domain_error(exc: Exception) -> None:
    if isinstance(exc, LookupError) or isinstance(exc, SubmissionNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, NotEnrolledError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this exam run.") from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, ResultsNotReleasedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)}) from exc
    if isinstance(exc, UnresolvedAttemptsError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "unresolved_count": exc.count},
        ) from exc
    if isinstance(exc, CommandRejectedError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "command": exc.command, "reason": exc.reason},
        ) from exc
    if isinstance(exc, PublicationBlockedError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "blockers": exc.blockers[:50]},
        ) from exc
    if isinstance(exc, VersionConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "expected": exc.expected, "actual": exc.actual},
        ) from exc
    if isinstance(exc, IllegalTransitionError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "current": exc.current, "target": exc.target},
        ) from exc
    if isinstance(exc, RuntimeNotAllowedError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": str(exc)}) from exc
    if isinstance(exc, (UploadTicketInvalidError, ViewTicketInvalidError, ExportTicketInvalidError)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": exc.code, "message": str(exc)}) from exc
    if isinstance(exc, ExportJobNotReadyError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc
    if (
        isinstance(exc, ExamDomainError)
        or isinstance(exc, JudgeDomainError)
        or isinstance(exc, ProctoringDomainError)
        or isinstance(exc, GradebookDomainError)
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc
    raise exc


def raise_for_conflict_reason(reason: str) -> None:
    """Maps a `decisions.py` conflict_reason string (submit/draft-save/content
    gating) to an HTTP response."""
    forbidden_like = {"attempt_blocked", "run_not_released", "slot_not_released"}
    status_code = status.HTTP_409_CONFLICT
    if reason in forbidden_like:
        status_code = status.HTTP_403_FORBIDDEN
    elif reason == "question_not_in_run":
        status_code = status.HTTP_404_NOT_FOUND
    elif reason == "answer_too_large":
        status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    raise HTTPException(status_code=status_code, detail={"code": reason})
