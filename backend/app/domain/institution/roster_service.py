"""Thin wiring for roster import — dry-run and bounded resumable apply. All
legality/duplicate/authorization logic lives in `roster_import.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.domain.institution.models import (
    Membership,
    MembershipStatus,
    RoleScope,
    RosterImportJob,
    RosterImportStatus,
    RosterRowInput,
)
from app.domain.institution.normalization import normalize_email, normalize_identity_key
from app.domain.institution.repository import MembershipRepository, RosterImportRepository
from app.domain.institution.roster_import import plan_apply_batch, plan_dry_run


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RosterService:
    def __init__(self, db) -> None:
        self._memberships = MembershipRepository(db)
        self._jobs = RosterImportRepository(db)

    def dry_run(
        self, *, institution_id: str, rows: list[RosterRowInput], requester_max_role_scope: RoleScope, uploaded_by: str
    ) -> RosterImportJob:
        existing = self._memberships.existing_identity_keys(institution_id)
        results = plan_dry_run(rows, requester_max_role_scope=requester_max_role_scope, existing_identity_keys=existing)
        now = _utc_now()
        job = RosterImportJob(
            id=uuid4().hex, institution_id=institution_id, uploaded_by=uploaded_by,
            requester_max_role_scope=requester_max_role_scope.value, status=RosterImportStatus.DRY_RUN,
            total_rows=len(rows), applied_cursor=0, row_results=results, created_at=now, updated_at=now,
        )
        self._jobs.create(job)
        return job

    def apply_batch(
        self, job_id: str, *, rows: list[RosterRowInput], requester_max_role_scope: RoleScope, batch_size: int = 200
    ) -> RosterImportJob:
        """Bounded and resumable: processes at most `batch_size` rows starting
        at the job's persisted cursor. Safe to call again after a crash — the
        cursor only advances after a batch's writes are applied, and every
        write is an upsert keyed by identity_key (idempotent), never a
        blind append."""
        job = self._jobs.get(job_id)
        if job is None:
            raise LookupError(f"roster import job {job_id} not found")

        existing_by_key = {m.identity_key: m for m in self._memberships.list_by_institution(job.institution_id)}
        results, next_cursor = plan_apply_batch(
            rows, requester_max_role_scope=requester_max_role_scope, existing_identity_keys=set(existing_by_key),
            cursor=job.applied_cursor, batch_size=batch_size,
        )

        now = _utc_now()
        for result in results:
            if not result.ok:
                continue
            row = rows[result.row_index]
            previous = existing_by_key.get(result.identity_key)
            role = RoleScope(row.role_scope)
            email = normalize_email(row.contact_email)
            if previous and previous.status == MembershipStatus.ACTIVE and previous.role_scope == role and previous.contact_email == email:
                continue  # unchanged — no write
            # Students get their uid now, so they can be enrolled in an exam before
            # their first sign-in. Faculty/admins keep uid=None until first login or
            # an explicit link of an existing account.
            uid = previous.uid if previous else (f"exam-{uuid4().hex}" if role == RoleScope.STUDENT else None)
            version = previous.authorization_version if previous else 1
            if previous and (previous.role_scope != role or previous.contact_email != email):
                version += 1  # sessions issued under the old role/contact stop working
            self._memberships.upsert(
                Membership(
                    institution_id=job.institution_id,
                    identity_key=normalize_identity_key(row.identity_key),
                    contact_email=email,
                    role_scope=role,
                    status=MembershipStatus.ACTIVE,
                    uid=uid,
                    authorization_version=version,
                    created_at=previous.created_at if previous else now,
                    updated_at=now,
                )
            )

        merged_results = list(job.row_results) + results
        new_status = RosterImportStatus.COMPLETED if next_cursor >= job.total_rows else RosterImportStatus.APPLYING
        self._jobs.update(
            job_id,
            {"applied_cursor": next_cursor, "row_results": [r.model_dump(mode="json") for r in merged_results],
             "status": new_status.value, "updated_at": now.isoformat()},
        )
        return self._jobs.get(job_id)
