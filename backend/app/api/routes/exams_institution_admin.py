"""Institution/roster faculty-admin routes. Revoking a membership requires a
RECENT reauthentication (not just a valid token) — see
`require_recent_staff_reauth` in exams_dependencies.py.

Every route is institution-scoped by the router-level guard (the caller must be
faculty IN that institution, or a platform operator). On top of that:

* only platform operators create institutions;
* only an institution ADMIN (or platform operator) grants elevated roles,
  overwrites an existing member, or revokes a membership — a FACULTY member may
  only add NEW students;
* a roster job can only be applied to the institution it was created for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import (
    can_administer,
    require_bootstrap_faculty,
    require_exam_faculty,
    require_exams_enabled,
    require_recent_staff_reauth,
)
from app.api.exams_scope import make_faculty_scope_guard, roster_job_institution
from app.core.firebase import db
from app.domain.institution.auth_service import InstitutionAuthService
from app.domain.institution.models import Institution, RoleScope, RosterRowInput
from app.domain.institution.normalization import normalize_identity_key
from app.domain.institution.repository import InstitutionRepository, MembershipRepository, RosterImportRepository
from app.domain.institution.roster_service import RosterService

router = APIRouter(
    prefix="/api/exams/v1/faculty/institutions",
    tags=["exams-institution-admin"],
    dependencies=[
        Depends(require_exams_enabled),
        Depends(make_faculty_scope_guard({"job_id": roster_job_institution})),
    ],
)


def _requester_max_role_scope(user: dict, institution_id: str) -> RoleScope:
    """Platform operators and institution ADMINs may grant up to ADMIN; a
    FACULTY member may only add students (cannot mint new faculty/admin peers)."""
    return RoleScope.ADMIN if can_administer(user, institution_id) else RoleScope.STUDENT


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
def create_institution(payload: CreateInstitutionRequest, user: dict = Depends(require_bootstrap_faculty)) -> dict:
    now = datetime.now(timezone.utc)
    institution = Institution(id=uuid4().hex, name=payload.name, created_at=now, updated_at=now)
    InstitutionRepository(db).create(institution)
    return institution.model_dump(mode="json")


@router.get("/{institution_id}/members")
def list_members(institution_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    """Roster view. Contact emails are PII, so they are returned to institution
    admins only; faculty see identity keys, roles and status."""
    members = MembershipRepository(db).list_by_institution(institution_id)
    include_contact = can_administer(user, institution_id)
    return {
        "results": [
            {
                "identity_key": m.identity_key,
                "role_scope": m.role_scope.value,
                "status": m.status.value,
                "linked": m.uid is not None,
                **({"contact_email": m.contact_email} if include_contact else {}),
            }
            for m in sorted(members, key=lambda item: item.identity_key)
        ]
    }


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
    job = RosterImportRepository(db).get(job_id)
    if job is None or job.institution_id != institution_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    max_scope = _requester_max_role_scope(user, institution_id)
    try:
        applied = RosterService(db).apply_batch(
            job_id, rows=payload.rows, requester_max_role_scope=max_scope, batch_size=payload.batch_size
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return applied.model_dump(mode="json")


@router.post("/{institution_id}/memberships/revoke")
def revoke_membership(
    institution_id: str,
    payload: RevokeMembershipRequest,
    user: dict = Depends(require_exam_faculty),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    if not can_administer(user, institution_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Institution admin authority required.")
    target = MembershipRepository(db).get(institution_id, normalize_identity_key(payload.identity_key))
    if target is not None and target.uid == user["uid"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "cannot_revoke_self"})
    try:
        InstitutionAuthService(db).revoke(institution_id=institution_id, identity_key=payload.identity_key)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"status": "revoked", "institution_id": institution_id, "identity_key": payload.identity_key}
