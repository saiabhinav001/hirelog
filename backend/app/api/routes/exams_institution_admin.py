"""Institution/roster faculty-admin routes. Revoking a membership requires a
RECENT reauthentication (not just a valid token) — see
`require_recent_staff_reauth` in exams_dependencies.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled, require_recent_staff_reauth
from app.core.config import settings
from app.core.firebase import db
from app.domain.institution.auth_service import InstitutionAuthService
from app.domain.institution.models import Institution, MembershipStatus, RoleScope, RosterRowInput
from app.domain.institution.repository import InstitutionRepository, MembershipRepository
from app.domain.institution.roster_service import RosterService

router = APIRouter(
    prefix="/api/exams/v1/faculty/institutions",
    tags=["exams-institution-admin"],
    dependencies=[Depends(require_exams_enabled)],
)


def _requester_max_role_scope(user: dict, institution_id: str) -> RoleScope:
    """Bootstrap users (EXAM_FACULTY_EMAILS) get full ADMIN authority — same
    tier they already have across every other exam faculty endpoint. A real
    institution ADMIN member may grant up to ADMIN; a FACULTY member may
    only grant STUDENT (cannot mint new faculty/admin peers)."""
    email = str(user.get("email") or "").strip().lower()
    if email and email in settings.exam_faculty_emails_set():
        return RoleScope.ADMIN

    memberships = MembershipRepository(db).find_by_uid(user["uid"])
    for m in memberships:
        if m.institution_id == institution_id and m.status == MembershipStatus.ACTIVE and m.role_scope == RoleScope.ADMIN:
            return RoleScope.ADMIN
    return RoleScope.STUDENT


class CreateInstitutionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class RosterDryRunRequest(BaseModel):
    rows: list[RosterRowInput] = Field(min_length=1, max_length=5000)


class RosterApplyRequest(BaseModel):
    rows: list[RosterRowInput] = Field(min_length=1, max_length=5000)
    batch_size: int = Field(default=200, ge=1, le=1000)


class RevokeMembershipRequest(BaseModel):
    identity_key: str = Field(min_length=1, max_length=64)


@router.post("")
def create_institution(payload: CreateInstitutionRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    now = datetime.now(timezone.utc)
    institution = Institution(id=uuid4().hex, name=payload.name, created_at=now, updated_at=now)
    InstitutionRepository(db).create(institution)
    return institution.model_dump(mode="json")


@router.post("/{institution_id}/roster/dry-run")
def roster_dry_run(institution_id: str, payload: RosterDryRunRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    max_scope = _requester_max_role_scope(user, institution_id)
    job = RosterService(db).dry_run(
        institution_id=institution_id, rows=payload.rows, requester_max_role_scope=max_scope, uploaded_by=user["uid"]
    )
    return job.model_dump(mode="json")


@router.post("/{institution_id}/roster/{job_id}/apply")
def roster_apply(
    institution_id: str, job_id: str, payload: RosterApplyRequest, user: dict = Depends(require_exam_faculty)
) -> dict:
    max_scope = _requester_max_role_scope(user, institution_id)
    try:
        job = RosterService(db).apply_batch(
            job_id, rows=payload.rows, requester_max_role_scope=max_scope, batch_size=payload.batch_size
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return job.model_dump(mode="json")


@router.post("/{institution_id}/memberships/revoke")
def revoke_membership(
    institution_id: str,
    payload: RevokeMembershipRequest,
    user: dict = Depends(require_exam_faculty),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    try:
        InstitutionAuthService(db).revoke(institution_id=institution_id, identity_key=payload.identity_key)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"status": "revoked", "institution_id": institution_id, "identity_key": payload.identity_key}
