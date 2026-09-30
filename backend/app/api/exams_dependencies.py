"""Auth/feature-flag dependencies for exam routes.

Deliberately separate from `app.api.dependencies` (legacy). Exam authorization
uses `get_current_user_uncached` (added in Prompt 1) — never the legacy
5-minute cached role. Faculty authority has TWO grant paths, both server-side
and neither living in the legacy `users/{uid}.role` field (contract #2):
`EXAM_FACULTY_EMAILS` config (the original Prompt 3 stand-in, kept for small
pilots with no roster) and real institution-scoped faculty/admin membership
(Prompt 2, docs/adr/0010) — whichever grants access first.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status

from app.api.dependencies import get_current_user_uncached
from app.core.config import settings
from app.core.firebase import db


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


def _has_institution_faculty_membership(uid: str) -> bool:
    from app.domain.institution.models import MembershipStatus, RoleScope
    from app.domain.institution.repository import MembershipRepository

    memberships = MembershipRepository(db).find_by_uid(uid)
    return any(
        m.status == MembershipStatus.ACTIVE and m.role_scope in (RoleScope.FACULTY, RoleScope.ADMIN)
        for m in memberships
    )


def require_exam_faculty(user: dict = Depends(get_current_exam_user)) -> dict:
    email = str(user.get("email") or "").strip().lower()
    if email and email in settings.exam_faculty_emails_set():
        return user
    if _has_institution_faculty_membership(user["uid"]):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Exam faculty authority required.",
    )


def require_active_institution_membership(institution_id: str, user: dict = Depends(get_current_exam_user)) -> dict:
    """Re-checks Firestore fresh on every call — a membership revoked or
    role-changed mid-session is observed immediately, even though the
    Firebase token itself remains valid until it expires."""
    from app.domain.institution.auth_service import InstitutionAuthService
    from app.domain.institution.errors import InstitutionDomainError

    service = InstitutionAuthService(db)
    try:
        membership = service.check_active_membership(
            uid=user["uid"], institution_id=institution_id, token_authz_version=user.get("exam_authz_version")
        )
    except InstitutionDomainError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)}) from exc
    return {**user, "membership": membership}


def require_recent_staff_reauth(user: dict = Depends(get_current_exam_user)) -> dict:
    """Staff/faculty-scoped actions require a RECENT sign-in — this checks
    Firebase's own `auth_time` claim, it does not claim to be MFA (an email
    OTP alone is never labeled MFA per the master prompt)."""
    auth_time = user.get("auth_time")
    if not auth_time:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth_time claim.")
    age_seconds = datetime.now(timezone.utc).timestamp() - float(auth_time)
    if age_seconds > settings.EXAM_STAFF_REAUTH_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "reauthentication_required", "max_age_seconds": settings.EXAM_STAFF_REAUTH_MAX_AGE_SECONDS},
        )
    return user


def require_seb_admission(run_id: str, request: Request, user: dict = Depends(get_current_exam_user)) -> dict:
    """Managed-lockdown admission gate (Prompt 10). A no-op (fast path) for
    the overwhelming majority of runs that never enable strict mode.
    Never uses User-Agent as proof — only the two SEB-documented
    cryptographic headers are ever consulted (see
    `app.domain.lockdown.decisions`). Composed into individual student
    routes via `Depends(require_seb_admission)` — see ADR 0015 for exactly
    which routes re-check this (admit/question/draft/submit/finish), not
    literally every request."""
    from app.domain.lockdown.decisions import CONFIG_KEY_HASH_HEADER, REQUEST_HASH_HEADER
    from app.domain.lockdown.errors import LockdownAdmissionDeniedError
    from app.domain.lockdown.service import LockdownService

    service = LockdownService(db)
    profile = service.get_or_default_profile(run_id)
    if not profile.enabled:
        return user

    try:
        service.check_admission(
            run_id=run_id,
            uid=user["uid"],
            url=str(request.url),
            config_key_hash_header=request.headers.get(CONFIG_KEY_HASH_HEADER),
            request_hash_header=request.headers.get(REQUEST_HASH_HEADER),
        )
    except LockdownAdmissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": exc.code,
                "message": "This exam requires a verified managed-lockdown browser (Safe Exam Browser). "
                "An ordinary browser — even with the exam PWA installed — cannot be admitted.",
                "reason": str(exc),
            },
        ) from exc
    return user
