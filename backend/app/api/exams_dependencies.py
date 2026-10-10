"""Auth/feature-flag dependencies for exam routes.

Deliberately separate from `app.api.dependencies` (legacy) and independent of
the legacy `users/{uid}` document: an exam caller is identified purely from
the verified Firebase ID token (no Firestore read, no write to a collection the
legacy app owns). Authority is never taken from client-writable data:

* Faculty by email (`EXAM_FACULTY_EMAILS`) requires the token's own `email`
  claim AND `email_verified` — Email/Password sign-up in the shared Firebase
  project can mint unverified tokens for any address.
* Institutional authority is the caller's CURRENT membership (looked up by
  uid, cached for a few seconds and invalidated on any membership write made by
  this process), never a claim baked into an old token. A token minted by the
  OTP flow carries `exam_institution_id` + `exam_authz_version`; both are
  re-checked on EVERY request, so a revoked/stale session stops working at its
  next call instead of at token expiry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import _verify_firebase_token, security
from app.core.config import settings
from app.core.firebase import db


def require_exams_enabled() -> None:
    if not settings.EXAMS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Exams module is disabled.",
        )


def get_exam_identity(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Verified-token identity only. `email` is populated only when the token
    says it is verified, so every downstream `user.get("email")` is safe to
    use for authority decisions."""
    decoded = _verify_firebase_token(credentials.credentials)
    uid = decoded.get("uid") or decoded.get("user_id") or decoded.get("sub")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload.")
    email_verified = decoded.get("email_verified") is True
    raw_email = str(decoded.get("email") or "").strip().lower()
    firebase_claim = decoded.get("firebase") or {}
    return {
        "uid": uid,
        "email": raw_email if email_verified else "",
        "email_verified": email_verified,
        "name": decoded.get("name") or "",
        "auth_time": decoded.get("auth_time"),
        "exam_authz_version": decoded.get("exam_authz_version"),
        "exam_institution_id": decoded.get("exam_institution_id"),
        "sign_in_provider": firebase_claim.get("sign_in_provider") if isinstance(firebase_claim, dict) else None,
    }


# ── institutional standing ────────────────────────────────────────────────────

def memberships_for(uid: str) -> list:
    from app.domain.institution.repository import MembershipRepository

    return MembershipRepository(db).find_by_uid(uid)


def active_membership(uid: str, institution_id: str):
    """The caller's ACTIVE membership in `institution_id`, or None."""
    from app.domain.institution.models import MembershipStatus

    for membership in memberships_for(uid):
        if membership.institution_id == institution_id and membership.status == MembershipStatus.ACTIVE:
            return membership
    return None


def assert_session_current(identity: dict) -> None:
    """Rejects a revoked or stale institution-backed session (adversarial
    review §1). A session minted by the OTP flow always names its institution
    and the authorization version it was issued under."""
    institution_id = identity.get("exam_institution_id")
    if not institution_id:
        return
    membership = active_membership(identity["uid"], institution_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "membership_not_active", "message": "Your institutional access is no longer active."},
        )
    try:
        token_version = int(identity.get("exam_authz_version"))
    except (TypeError, ValueError):
        token_version = None
    if token_version != membership.authorization_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "stale_authorization", "message": "Your session is out of date. Please sign in again."},
        )


def get_current_exam_user(
    user: dict = Depends(get_exam_identity),
    _flag: None = Depends(require_exams_enabled),
) -> dict:
    assert_session_current(user)
    return user


# ── faculty authority ─────────────────────────────────────────────────────────

def is_bootstrap_faculty(user: dict) -> bool:
    """Operator-configured platform faculty. Needs a VERIFIED email claim."""
    email = str(user.get("email") or "").strip().lower()
    return bool(email) and user.get("email_verified") is True and email in settings.exam_faculty_emails_set()


def faculty_institution_ids(user: dict) -> set[str]:
    from app.domain.institution.models import MembershipStatus, RoleScope

    return {
        m.institution_id
        for m in memberships_for(user["uid"])
        if m.status == MembershipStatus.ACTIVE and m.role_scope in (RoleScope.FACULTY, RoleScope.ADMIN)
    }


def admin_institution_ids(user: dict) -> set[str]:
    from app.domain.institution.models import MembershipStatus, RoleScope

    return {
        m.institution_id
        for m in memberships_for(user["uid"])
        if m.status == MembershipStatus.ACTIVE and m.role_scope == RoleScope.ADMIN
    }


def _has_institution_faculty_membership(uid: str) -> bool:
    return bool(faculty_institution_ids({"uid": uid}))


def require_exam_faculty(user: dict = Depends(get_current_exam_user)) -> dict:
    if is_bootstrap_faculty(user) or _has_institution_faculty_membership(user["uid"]):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Exam faculty authority required.",
    )


def require_bootstrap_faculty(user: dict = Depends(require_exam_faculty)) -> dict:
    """Platform-level actions (e.g. creating an institution)."""
    if not is_bootstrap_faculty(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform administrator authority required.")
    return user


def can_act_as_faculty(user: dict, institution_id: Optional[str]) -> bool:
    """Bootstrap faculty are platform operators and may act anywhere; everyone
    else only inside an institution where they hold ACTIVE faculty/admin
    membership. Records with no institution (pilot data) are bootstrap-only."""
    if is_bootstrap_faculty(user):
        return True
    return institution_id is not None and institution_id in faculty_institution_ids(user)


def can_administer(user: dict, institution_id: str) -> bool:
    return is_bootstrap_faculty(user) or institution_id in admin_institution_ids(user)


def require_active_institution_membership(institution_id: str, user: dict = Depends(get_current_exam_user)) -> dict:
    """Re-checks membership state on every call — a membership revoked or
    role-changed mid-session is observed immediately, even though the
    Firebase token itself remains valid until it expires."""
    from app.domain.institution.auth_service import InstitutionAuthService
    from app.domain.institution.errors import InstitutionDomainError

    claim_institution = user.get("exam_institution_id")
    token_version = user.get("exam_authz_version") if claim_institution in (None, institution_id) else None
    service = InstitutionAuthService(db)
    try:
        membership = service.check_active_membership(
            uid=user["uid"], institution_id=institution_id, token_authz_version=token_version
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


# ── student run access ────────────────────────────────────────────────────────

def require_student_run_access(run_id: str, user: dict = Depends(get_current_exam_user)) -> dict:
    """Enrolled in THIS run and, for institution-bound runs, still an ACTIVE
    member of that institution right now."""
    from app.api.exams_scope import assert_student_run_access

    assert_student_run_access(user, run_id)
    return user


def require_seb_admission(run_id: str, request: Request, user: dict = Depends(require_student_run_access)) -> dict:
    """Managed-lockdown admission gate (Prompt 10). A no-op (fast path) for
    the overwhelming majority of runs that never enable strict mode.
    Never uses User-Agent as proof — only the two SEB-documented
    cryptographic headers are ever consulted (see
    `app.domain.lockdown.decisions`). Composed into individual student
    routes via `Depends(require_seb_admission)` — see ADR 0015 for exactly
    which routes re-check this (admit/question/draft/submit/finish), not
    literally every request."""
    from app.api.exams_scope import lockdown_profile
    from app.domain.lockdown.decisions import CONFIG_KEY_HASH_HEADER, REQUEST_HASH_HEADER
    from app.domain.lockdown.errors import LockdownAdmissionDeniedError
    from app.domain.lockdown.service import LockdownService

    profile = lockdown_profile(run_id)
    if not profile.enabled:
        return user

    service = LockdownService(db)
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
