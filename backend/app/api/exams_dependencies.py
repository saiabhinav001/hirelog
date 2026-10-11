"""Auth/feature-flag dependencies for exam routes.

Deliberately separate from `app.api.dependencies` (legacy). Exam authorization
uses `get_current_user_uncached` (added in Prompt 1) — never the legacy
5-minute cached role. Authority is computed on the server by the pure policy
in `app.domain.authz` (ADR 0017) from exactly three inputs: the VERIFIED token
e-mail checked against the `EXAM_SUPER_ADMIN_EMAILS` / `EXAM_FACULTY_EMAILS`
allowlists (and bound to the first uid that proves it), the caller's
institution memberships (Prompt 2, ADR 0010), and the resource being touched.
Neither the request body nor any `users/{uid}` field is ever an input — in
particular the legacy `users/{uid}.email` is client-writable at creation and
is NEVER used for authority (the verified `token_email` is).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status

from app.api.dependencies import get_current_user_uncached
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.domain.authz.policy import (
    ExamPrincipal,
    ExamRole,
    StaffGrants,
    can_participate_as_student,
    check_token_membership_freshness,
    resolve_staff_grants,
)
from app.domain.authz.staff_binding import StaffBindingService
from app.domain.exams.repository import ExamRunRepository
from app.domain.institution.errors import MembershipNotActiveError, StaleAuthorizationVersionError
from app.domain.institution.normalization import normalize_email

logger = logging.getLogger("hirelog.exams.authz")

# Per-request memoization lives on the user dict FastAPI already shares between
# every dependency of one request (`get_current_exam_user` is cached per request).
_GRANTS_KEY = "_exam_staff_grants"
_PRINCIPAL_KEY = "_exam_principal"

_unverified_flag_warned = False


def require_exams_enabled() -> None:
    if not settings.EXAMS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Exams module is disabled.",
        )


def _staff_binding_service() -> StaffBindingService:
    return StaffBindingService(db)


def _load_memberships(uid: str) -> tuple:
    from app.domain.institution.repository import MembershipRepository

    return tuple(MembershipRepository(db).find_by_uid(uid))


def _warn_if_unverified_flag_ignored() -> None:
    global _unverified_flag_warned
    if settings.EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL and not settings.unverified_staff_email_allowed() and not _unverified_flag_warned:
        _unverified_flag_warned = True
        logger.error("EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL is ignored when ENV=production")


def _staff_grants(user: dict) -> StaffGrants:
    """Allowlist authority for this request: VERIFIED token e-mail only, and
    bound to the first uid that proved it. A different uid presenting a bound
    address is denied (and audited by the binding service)."""
    cached = user.get(_GRANTS_KEY)
    if isinstance(cached, StaffGrants):
        return cached

    _warn_if_unverified_flag_ignored()
    email = normalize_email(str(user.get("token_email") or ""))
    grants = resolve_staff_grants(
        email=email,
        email_verified=bool(user.get("email_verified")),
        super_admin_emails=settings.exam_super_admin_emails_set(),
        teacher_emails=settings.exam_faculty_emails_set(),
        allow_unverified=settings.unverified_staff_email_allowed(),
    )
    if grants.any:
        roles = [
            role.value
            for role, granted in ((ExamRole.SUPER_ADMIN, grants.super_admin), (ExamRole.TEACHER, grants.teacher))
            if granted
        ]
        try:
            outcome = _staff_binding_service().ensure_bound(
                email=email, uid=user["uid"], roles=roles, sign_in_provider=user.get("sign_in_provider")
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never grant authority on an unverifiable binding
            logger.exception("exam staff binding check failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="authorization_backend_unavailable"
            ) from exc
        if outcome == "conflict":
            grants = StaffGrants()
    user[_GRANTS_KEY] = grants
    return grants


def resolve_principal(user: dict) -> ExamPrincipal:
    """Server-computed identity + roles + memberships for this request
    (one membership query, memoized on the request's user dict)."""
    cached = user.get(_PRINCIPAL_KEY)
    if isinstance(cached, ExamPrincipal):
        return cached
    grants = _staff_grants(user)
    principal = ExamPrincipal(
        uid=user["uid"],
        email=normalize_email(str(user.get("token_email") or "")),
        email_verified=bool(user.get("email_verified")),
        bootstrap_super_admin=grants.super_admin,
        bootstrap_teacher=grants.teacher,
        memberships=_load_memberships(user["uid"]),
    )
    user[_PRINCIPAL_KEY] = principal
    return principal


def _enforce_token_membership_freshness(user: dict, institution_id: str) -> None:
    principal = resolve_principal(user)
    try:
        check_token_membership_freshness(
            principal.memberships, institution_id=institution_id, token_authz_version=user.get("exam_authz_version")
        )
    except (MembershipNotActiveError, StaleAuthorizationVersionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)}
        ) from exc


def get_current_exam_user(
    user: dict = Depends(get_current_user_uncached),
    _flag: None = Depends(require_exams_enabled),
) -> dict:
    """Verified identity + feature flag + CURRENT membership/role version.

    A session minted by the institutional OTP flow carries
    `exam_institution_id` / `exam_authz_version` claims; revoking or changing
    that membership must take effect on the very next request even though the
    Firebase ID token itself stays valid until it expires (adversarial review
    §1: this check previously existed but no route called it)."""
    institution_id = user.get("exam_institution_id")
    if institution_id:
        _enforce_token_membership_freshness(user, str(institution_id))
    return user


def require_exam_faculty(user: dict = Depends(get_current_exam_user)) -> dict:
    """Teacher (or super_admin) authority: a verified, UID-bound bootstrap
    allowlist entry or an ACTIVE institution FACULTY/ADMIN membership. This is
    only the ROLE gate — resource ownership/institution scope is enforced by
    the dependencies in `app.api.exams_scope`."""
    if _staff_grants(user).any:
        return user
    if resolve_principal(user).is_teacher:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Exam faculty authority required.",
    )


def require_exam_super_admin(user: dict = Depends(get_current_exam_user)) -> dict:
    if _staff_grants(user).super_admin:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Exam super-admin authority required.",
    )


def authorize_student_for_run(user: dict, run_id: str):
    """Student-side scope for one run, checked on EVERY request: the run
    exists, the caller is enrolled, and — when the run belongs to an
    institution — the caller currently holds an ACTIVE student membership in
    it. Returns the run so callers can reuse it."""
    run = ExamRunRepository(db).get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"exam run {run_id} not found")
    if user["uid"] not in run.enrolled_uids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this exam run.")
    if run.institution_id and not can_participate_as_student(
        resolve_principal(user), run_institution_id=run.institution_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "membership_not_active", "message": "No active student membership in this exam's institution."},
        )
    return run


def require_student_run_scope(run_id: str, user: dict = Depends(get_current_exam_user)) -> dict:
    authorize_student_for_run(user, run_id)
    return user



def require_active_institution_membership(institution_id: str, user: dict = Depends(get_current_exam_user)) -> dict:
    """Explicit per-institution check (re-reads Firestore fresh). Note that
    `get_current_exam_user` already applies the token-claim membership
    freshness check to every exam route, and `require_student_run_scope`
    applies the run's institution membership — this stays available for
    callers that need the membership object itself."""
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


def require_seb_admission(run_id: str, request: Request, user: dict = Depends(require_student_run_scope)) -> dict:
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
