"""`GET /api/exams/v1/me` — server-confirmed capabilities for role routing.
The frontend routes the signed-in user based on THIS response, not a
client-side guess (contract: "Role routing is based on server capabilities")."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.exams_dependencies import get_current_exam_user, is_bootstrap_faculty, memberships_for, require_exams_enabled

router = APIRouter(prefix="/api/exams/v1", tags=["exams-me"], dependencies=[Depends(require_exams_enabled)])


@router.get("/me")
def get_me(user: dict = Depends(get_current_exam_user)) -> dict:
    memberships = memberships_for(user["uid"])

    return {
        "uid": user["uid"],
        # Only a verified email is ever surfaced or trusted.
        "email": user.get("email") or None,
        "email_verified": bool(user.get("email_verified")),
        "is_bootstrap_faculty": is_bootstrap_faculty(user),
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
