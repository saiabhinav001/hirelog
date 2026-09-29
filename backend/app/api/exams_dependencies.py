"""Auth/feature-flag dependencies for exam routes.

Deliberately separate from `app.api.dependencies` (legacy). Exam authorization
uses `get_current_user_uncached` (added in Prompt 1) — never the legacy
5-minute cached role — and exam faculty authority is resolved from
`EXAM_FACULTY_EMAILS` server config, NOT from the legacy `users/{uid}.role`
field, so it never lives in a client-editable user record (contract #2).
This is an explicit, bounded stand-in for Prompt 2's institution/faculty
membership repository (see docs/adr/0008).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.api.dependencies import get_current_user_uncached
from app.core.config import settings


def require_exams_enabled() -> None:
    if not settings.EXAMS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Exams module is disabled.",
        )


def get_current_exam_user(
    user: dict = Depends(get_current_user_uncached),
    _flag: None = Depends(require_exams_enabled),
) -> dict:
    return user


def require_exam_faculty(user: dict = Depends(get_current_exam_user)) -> dict:
    email = str(user.get("email") or "").strip().lower()
    if not email or email not in settings.exam_faculty_emails_set():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Exam faculty authority required.",
        )
    return user
