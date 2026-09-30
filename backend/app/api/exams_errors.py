"""Maps exam domain exceptions to HTTP responses. Kept in one place so every
exam route returns a consistent shape for the same underlying condition."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.domain.exams.errors import (
    ExamDomainError,
    IllegalTransitionError,
    NotEnrolledError,
    UnresolvedAttemptsError,
    VersionConflictError,
)
from app.domain.judge.errors import JudgeDomainError, RuntimeNotAllowedError, SubmissionNotFoundError


def raise_for_domain_error(exc: Exception) -> None:
    if isinstance(exc, LookupError) or isinstance(exc, SubmissionNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, NotEnrolledError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this exam run.") from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, UnresolvedAttemptsError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "unresolved_count": exc.count},
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
    if isinstance(exc, ExamDomainError) or isinstance(exc, JudgeDomainError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}) from exc
    raise exc


def raise_for_conflict_reason(reason: str) -> None:
    """Maps a `decisions.py` conflict_reason string (submit/draft-save/content
    gating) to an HTTP response."""
    not_found_like = {"attempt_not_in_progress", "attempt_not_active"}
    forbidden_like = {"attempt_blocked", "run_not_released"}
    status_code = status.HTTP_409_CONFLICT
    if reason in forbidden_like:
        status_code = status.HTTP_403_FORBIDDEN
    elif reason in not_found_like:
        status_code = status.HTTP_409_CONFLICT
    raise HTTPException(status_code=status_code, detail={"code": reason})
