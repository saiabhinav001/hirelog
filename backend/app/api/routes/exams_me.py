"""`GET /api/exams/v1/me` — server-confirmed capabilities for role routing.
The frontend routes the signed-in user based on THIS response, not a
client-side guess (contract: "Role routing is based on server capabilities")."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.exams_dependencies import get_current_exam_user, require_exams_enabled, resolve_principal

router = APIRouter(prefix="/api/exams/v1", tags=["exams-me"], dependencies=[Depends(require_exams_enabled)])


@router.get("/me")
def get_me(user: dict = Depends(get_current_exam_user)) -> dict:
    # Roles are computed by the server policy (ADR 0017) from the VERIFIED
    # token e-mail + memberships — never from stored profile fields. The
    # frontend uses this for routing only; every route re-checks on the server.
    principal = resolve_principal(user)
    memberships = principal.memberships

    return {
        "uid": user["uid"],
        "email": user.get("email"),
        "email_verified": principal.email_verified,
        # Verified bootstrap staff authority (teacher or super_admin allowlist,
        # UID-bound). Name kept for frontend compatibility (`examAuth.tsx`).
        "is_bootstrap_faculty": principal.bootstrap_teacher or principal.bootstrap_super_admin,
        "is_super_admin": principal.is_super_admin,
        "exam_roles": [role.value for role in principal.roles()],
        "memberships": [
            {
                "institution_id": m.institution_id,
                "role_scope": m.role_scope.value,
                "status": m.status.value,
                "authorization_version": m.authorization_version,
            }
            for m in memberships
        ],
        # A legacy user (search/practice-lists) with no exam membership at
        # all gets an empty list here, not an error — legacy compatibility.
        "has_any_exam_membership": len(memberships) > 0,
    }
