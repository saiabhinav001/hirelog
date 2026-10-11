"""Institution / roster / membership management routes (super-admin and institution
staff). Sensitive actions — creating an institution, applying a roster, assigning a
teacher, revoking / reinstating / unlinking a membership — require a RECENT
reauthentication (not just a valid token): see `require_recent_staff_reauth` in
exams_dependencies.py. That is a recency check on the Firebase session, not MFA.

The roster dry-run and apply routes accept either `application/json`
(`{"rows": [...]}`, as before) or `multipart/form-data` with a CSV `file`; both
feed the same service. Authorization (the institution scope dependency) runs
before the body is read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile

from app.api.exams_dependencies import (
    require_exam_faculty,
    require_exam_super_admin,
    require_exams_enabled,
    require_recent_staff_reauth,
    resolve_principal,
)
from app.api.exams_scope import require_institution_admin_scope, require_institution_roster_scope
from app.core.firebase import exam_db as db
from app.domain.authz.policy import can_administer_institution, max_grantable_role_scope
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.institution.auth_service import InstitutionAuthService
from app.domain.institution.errors import RosterRowRejectedError, RosterRowsChangedError, UnauthorizedRoleGrantError
from app.domain.institution.models import (
    IdentityKeyPolicy,
    Institution,
    MembershipStatus,
    RoleScope,
    RosterRowInput,
)
from app.domain.institution.repository import InstitutionRepository, MembershipRepository, RosterImportRepository
from app.domain.institution.roster_csv import MAX_CSV_BYTES, RosterCsvError, RosterRowCandidate, parse_roster_csv
from app.domain.institution.roster_service import RosterService

router = APIRouter(
    prefix="/api/exams/v1/faculty/institutions",
    tags=["exams-institution-admin"],
    dependencies=[Depends(require_exams_enabled)],
)

_MAX_UPLOAD_BYTES = MAX_CSV_BYTES + 65_536  # the CSV limit plus multipart framing
_DEFAULT_BATCH_SIZE = 200


def _requester_max_role_scope(user: dict, institution_id: str) -> RoleScope:
    """Highest role a caller may put on roster rows (and the highest role whose
    membership they may revoke / reinstate / unlink): a super-admin up to ADMIN,
    everyone else STUDENT only — granting FACULTY/ADMIN is teacher assignment,
    a super-admin function. Callers without any standing never reach this
    (`require_institution_roster_scope` rejects them first)."""
    return max_grantable_role_scope(resolve_principal(user), institution_id) or RoleScope.STUDENT


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


class CreateInstitutionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Optional college-ID rules; fixed at creation (see IdentityKeyPolicy).
    identity_policy: Optional[IdentityKeyPolicy] = None


class RosterBody(BaseModel):
    rows: list[RosterRowInput] = Field(min_length=1, max_length=5000)
    batch_size: int = Field(default=_DEFAULT_BATCH_SIZE, ge=1, le=1000)


@dataclass(frozen=True)
class RosterPayload:
    rows: list[RosterRowCandidate]
    batch_size: int


class RevokeMembershipRequest(BaseModel):
    identity_key: str = Field(min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=500)


class MembershipActionRequest(BaseModel):
    identity_key: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=500)

    model_config = {"extra": "forbid"}


class AssignTeacherRequest(BaseModel):
    identity_key: str = Field(min_length=1, max_length=64)
    contact_email: str = Field(min_length=3, max_length=254)
    role_scope: Literal["faculty", "admin"] = "faculty"
    reason: Optional[str] = Field(default=None, max_length=500)

    model_config = {"extra": "forbid"}


async def _read_roster_payload(
    request: Request, user: dict = Depends(require_institution_roster_scope)
) -> RosterPayload:
    """JSON rows or a multipart CSV `file` (plus an optional `batch_size` field)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()

    if content_type == "application/json":
        try:
            body = RosterBody.model_validate(await request.json())
        except ValueError as exc:  # invalid JSON, or a ValidationError (a ValueError subclass)
            detail = json.loads(exc.json(include_url=False))[:50] if isinstance(exc, ValidationError) else "invalid_json"
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail) from exc
        return RosterPayload(rows=list(body.rows), batch_size=body.batch_size)

    if content_type == "multipart/form-data":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail={"code": "file_too_large"}
            )
        form = await request.form(max_files=1, max_fields=4)
        try:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "csv_file_required", "message": "Send the CSV as a multipart field named 'file'."},
                )
            data = await upload.read(MAX_CSV_BYTES + 1)
            raw_batch = form.get("batch_size")
            if raw_batch is None:
                batch_size = _DEFAULT_BATCH_SIZE
            elif isinstance(raw_batch, str) and raw_batch.isdigit() and 1 <= int(raw_batch) <= 1000:
                batch_size = int(raw_batch)
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_batch_size"}
                )
        finally:
            await form.close()
        try:
            return RosterPayload(rows=parse_roster_csv(data), batch_size=batch_size)
        except RosterCsvError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": exc.code, "message": str(exc)}
            ) from exc

    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail="Send application/json ({\"rows\": [...]}) or multipart/form-data with a CSV 'file'.",
    )


@router.post("")
def create_institution(
    payload: CreateInstitutionRequest,
    user: dict = Depends(require_exam_super_admin),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    now = datetime.now(timezone.utc)
    institution = Institution(
        id=uuid4().hex, name=payload.name, identity_policy=payload.identity_policy, created_at=now, updated_at=now
    )
    InstitutionRepository(db).create(institution)
    AuthzAuditRepository(db).append(
        build_audit_event(
            "institution_created", actor_uid=user["uid"], institution_id=institution.id, now=now,
            detail={"identity_policy": payload.identity_policy.model_dump(mode="json") if payload.identity_policy else None},
        )
    )
    return institution.model_dump(mode="json")


@router.get("")
def list_institutions(user: dict = Depends(require_exam_faculty)) -> dict:
    """Institutions the caller can see: all of them for a super-admin, otherwise
    those where they hold an active FACULTY/ADMIN membership."""
    principal = resolve_principal(user)
    repository = InstitutionRepository(db)
    if principal.is_super_admin:
        institutions = repository.list_all()
    else:
        institutions = [i for i in (repository.get(iid) for iid in sorted(principal.teacher_institution_ids)) if i]
    institutions.sort(key=lambda i: i.name.lower())
    return {"results": [i.model_dump(mode="json") for i in institutions]}


@router.get("/{institution_id}/memberships")
def list_memberships(
    institution_id: str,
    role_scope: Optional[RoleScope] = None,
    status_filter: Optional[MembershipStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    after: Optional[str] = None,
    user: dict = Depends(require_institution_roster_scope),
) -> dict:
    """Members of one institution, ordered by college ID. Contact e-mails are
    masked and no uid is returned."""
    members = sorted(MembershipRepository(db).list_by_institution(institution_id), key=lambda m: m.identity_key)
    members = [
        m for m in members
        if (role_scope is None or m.role_scope == role_scope)
        and (status_filter is None or m.status == status_filter)
        and (after is None or m.identity_key > after)
    ]
    page = members[:limit]
    return {
        "results": [
            {
                "identity_key": m.identity_key, "role_scope": m.role_scope.value, "status": m.status.value,
                "linked": m.uid is not None, "authorization_version": m.authorization_version,
                "contact_email_masked": _mask_email(m.contact_email), "updated_at": m.updated_at.isoformat(),
            }
            for m in page
        ],
        "next_after": page[-1].identity_key if len(members) > limit else None,
    }


@router.post("/{institution_id}/roster/dry-run")
def roster_dry_run(
    institution_id: str,
    payload: RosterPayload = Depends(_read_roster_payload),
    user: dict = Depends(require_institution_roster_scope),
) -> dict:
    job = RosterService(db).dry_run(
        institution_id=institution_id, rows=payload.rows,
        requester_max_role_scope=_requester_max_role_scope(user, institution_id), uploaded_by=user["uid"],
        can_change_contact_email=can_administer_institution(resolve_principal(user), institution_id),
    )
    return job.model_dump(mode="json")


@router.get("/{institution_id}/roster/{job_id}")
def get_roster_job(
    institution_id: str,
    job_id: str,
    include_rows: bool = False,
    user: dict = Depends(require_institution_roster_scope),
) -> dict:
    """Progress of a dry-run / apply: cursor, status, counts and the first 200 row
    errors (every row with `include_rows=true`)."""
    job = RosterImportRepository(db).get(job_id)
    if job is None or job.institution_id != institution_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="roster import job not found")
    results = job.row_results
    body = job.model_dump(mode="json", exclude={"row_results"})
    body["summary"] = {
        "ok": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "create": sum(1 for r in results if r.ok and r.action == "create"),
        "update": sum(1 for r in results if r.ok and r.action == "update"),
        "skip_unchanged": sum(1 for r in results if r.ok and r.action == "skip_unchanged"),
    }
    body["errors"] = [r.model_dump(mode="json", exclude_none=True) for r in results if not r.ok][:200]
    if include_rows:
        body["row_results"] = [r.model_dump(mode="json", exclude_none=True) for r in results]
    return body


@router.post("/{institution_id}/roster/{job_id}/apply")
def roster_apply(
    institution_id: str,
    job_id: str,
    payload: RosterPayload = Depends(_read_roster_payload),
    user: dict = Depends(require_institution_roster_scope),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    # A job is bound to ONE institution; the path institution (which the scope
    # check above authorized) must be that institution, or a caller scoped to A
    # could apply a job that writes memberships into B.
    job = RosterImportRepository(db).get(job_id)
    if job is None or job.institution_id != institution_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="roster import job not found")
    try:
        job = RosterService(db).apply_batch(
            job_id, rows=payload.rows, requester_max_role_scope=_requester_max_role_scope(user, institution_id),
            batch_size=payload.batch_size, actor_uid=user["uid"],
            can_change_contact_email=can_administer_institution(resolve_principal(user), institution_id),
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RosterRowsChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return job.model_dump(mode="json")


def _membership_action(institution_id: str, user: dict, identity_key: str, reason: Optional[str], action: str) -> dict:
    service = InstitutionAuthService(db)
    try:
        membership = getattr(service, action)(
            institution_id=institution_id, identity_key=identity_key, actor_uid=user["uid"], reason=reason,
            requester_max_role_scope=_requester_max_role_scope(user, institution_id),
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnauthorizedRoleGrantError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return {
        "status": {"revoke": "revoked", "reinstate": "reinstated", "unlink": "unlinked"}[action],
        "institution_id": institution_id, "identity_key": membership.identity_key,
    }


@router.post("/{institution_id}/memberships/revoke")
def revoke_membership(
    institution_id: str,
    payload: RevokeMembershipRequest,
    user: dict = Depends(require_institution_admin_scope),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    result = _membership_action(institution_id, user, payload.identity_key, payload.reason, "revoke")
    # The caller's own spelling, as before (the response used to echo the request).
    return {**result, "identity_key": payload.identity_key}


@router.post("/{institution_id}/memberships/reinstate")
def reinstate_membership(
    institution_id: str,
    payload: MembershipActionRequest,
    user: dict = Depends(require_institution_admin_scope),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    return _membership_action(institution_id, user, payload.identity_key, payload.reason, "reinstate")


@router.post("/{institution_id}/memberships/unlink")
def unlink_membership(
    institution_id: str,
    payload: MembershipActionRequest,
    user: dict = Depends(require_institution_admin_scope),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    return _membership_action(institution_id, user, payload.identity_key, payload.reason, "unlink")


@router.post("/{institution_id}/teachers")
def assign_teacher(
    institution_id: str,
    payload: AssignTeacherRequest,
    user: dict = Depends(require_exam_super_admin),
    _scope: dict = Depends(require_institution_roster_scope),
    _reauth: dict = Depends(require_recent_staff_reauth),
) -> dict:
    """Teacher assignment: grants the FACULTY (or ADMIN) role in an institution.
    Super-admin only; the teacher then proves control of the contact e-mail (OTP)
    and links the account they teach with."""
    try:
        result = RosterService(db).assign_teacher(
            institution_id=institution_id, identity_key=payload.identity_key, contact_email=payload.contact_email,
            role_scope=RoleScope(payload.role_scope), actor_uid=user["uid"], reason=payload.reason,
        )
    except RosterRowRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if exc.reason == "membership_revoked_reinstate_required"
            else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "reason": exc.reason},
        ) from exc
    return {
        "status": "assigned", "institution_id": institution_id, "identity_key": result.identity_key,
        "role_scope": payload.role_scope, "action": result.action,
    }
