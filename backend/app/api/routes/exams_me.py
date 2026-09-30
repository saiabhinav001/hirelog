"""`GET /api/exams/v1/me` — server-confirmed capabilities for role routing.
The frontend routes the signed-in user based on THIS response, not a
client-side guess (contract: "Role routing is based on server capabilities")."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.exams_dependencies import get_current_exam_user, require_exams_enabled
from app.core.config import settings
from app.core.firebase import db
from app.domain.institution.repository import MembershipRepository

router = APIRouter(prefix="/api/exams/v1", tags=["exams-me"], dependencies=[Depends(require_exams_enabled)])


@router.get("/me")
def get_me(user: dict = Depends(get_current_exam_user)) -> dict:
    email = str(user.get("email") or "").strip().lower()
    is_bootstrap_faculty = bool(email) and email in settings.exam_faculty_emails_set()
    memberships = MembershipRepository(db).find_by_uid(user["uid"])

    return {
        "uid": user["uid"],
        "email": user.get("email"),
        "is_bootstrap_faculty": is_bootstrap_faculty,
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
