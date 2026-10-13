"""Roster import wiring — dry-run, bounded resumable apply and explicit teacher
assignment. All legality/duplicate/authorization logic lives in
`roster_import.py`; this layer loads stored state, writes, and audits.

Writes never rewrite a whole membership: creating a student is an atomic
create, and anything that changes who an identity IS (role or contact e-mail)
re-checks the stored membership inside a transaction and commits its audit
event atomically with the change. A roster can therefore never clobber a
concurrent login (`uid`) or revocation (`status`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import uuid4

from firebase_admin import firestore

from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.institution.errors import RosterRowRejectedError, RosterRowsChangedError
from app.domain.institution.models import (
    IdentityKeyPolicy,
    Membership,
    MembershipStatus,
    RoleScope,
    RosterImportJob,
    RosterImportStatus,
    RosterRowInput,
    RosterRowResult,
)
from app.domain.institution.normalization import normalize_email, normalize_identity_key
from app.domain.institution.repository import (
    InstitutionRepository,
    MembershipRepository,
    RosterImportRepository,
    invalidate_membership_cache,
)
from app.domain.institution.roster_csv import RosterRowCandidate
from app.domain.institution.roster_import import (
    batch_identity_keys,
    classify_row,
    plan_apply_batch,
    plan_dry_run,
    rows_digest,
)

logger = logging.getLogger("hirelog.institution.roster")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RosterService:
    def __init__(self, db) -> None:
        self._db = db
        self._memberships = MembershipRepository(db)
        self._institutions = InstitutionRepository(db)
        self._jobs = RosterImportRepository(db)
        self._audit = AuthzAuditRepository(db)

    def _policy(self, institution_id: str) -> Optional[IdentityKeyPolicy]:
        institution = self._institutions.get(institution_id)
        return institution.identity_policy if institution else None

    def dry_run(
        self,
        *,
        institution_id: str,
        rows: Sequence[RosterRowCandidate],
        requester_max_role_scope: RoleScope,
        uploaded_by: str,
        can_change_contact_email: bool = False,
    ) -> RosterImportJob:
        policy = self._policy(institution_id)
        existing = self._memberships.get_many(institution_id, batch_identity_keys(rows, policy=policy))
        results = plan_dry_run(
            rows, requester_max_role_scope=requester_max_role_scope, existing_memberships=existing,
            can_change_contact_email=can_change_contact_email, policy=policy,
        )
        now = _utc_now()
        job = RosterImportJob(
            id=uuid4().hex, institution_id=institution_id, uploaded_by=uploaded_by,
            requester_max_role_scope=requester_max_role_scope.value, status=RosterImportStatus.DRY_RUN,
            total_rows=len(rows), applied_cursor=0, row_results=results,
            rows_digest=rows_digest(rows, policy=policy), created_at=now, updated_at=now,
        )
        self._jobs.create(job)
        return job

    def apply_batch(
        self,
        job_id: str,
        *,
        rows: Sequence[RosterRowCandidate],
        requester_max_role_scope: RoleScope,
        batch_size: int = 200,
        can_change_contact_email: bool = False,
        actor_uid: str = "",
    ) -> RosterImportJob:
        """Bounded and resumable: processes at most `batch_size` rows starting at
        the job's persisted cursor, and only the rows the dry-run validated.
        Safe to call again after a crash — the cursor advances only after the
        batch is written, and a row that was already written re-plans as
        `skip_unchanged`. Calling it on a completed job writes nothing."""
        job = self._jobs.get(job_id)
        if job is None:
            raise LookupError(f"roster import job {job_id} not found")

        policy = self._policy(job.institution_id)
        if len(rows) != job.total_rows or (job.rows_digest and rows_digest(rows, policy=policy) != job.rows_digest):
            raise RosterRowsChangedError("the submitted rows differ from the rows validated by the dry-run")

        keys = batch_identity_keys(rows, policy=policy, cursor=job.applied_cursor, batch_size=batch_size)
        existing = self._memberships.get_many(job.institution_id, keys)
        results, next_cursor = plan_apply_batch(
            rows, requester_max_role_scope=requester_max_role_scope, existing_memberships=existing,
            can_change_contact_email=can_change_contact_email, policy=policy,
            cursor=job.applied_cursor, batch_size=batch_size,
        )

        now = _utc_now()
        final = [
            self._write_row(
                job.institution_id, rows[result.row_index], result, now=now, actor_uid=actor_uid, via="roster",
                requester_max_role_scope=requester_max_role_scope, can_change_contact_email=can_change_contact_email,
            )
            if result.ok and result.action != "skip_unchanged"
            else result
            for result in results
        ]

        if final:
            counts = {
                action: sum(1 for r in final if r.ok and r.action == action)
                for action in ("create", "update", "skip_unchanged")
            }
            counts["failed"] = sum(1 for r in final if not r.ok)
            self._audit.append(
                build_audit_event(
                    "roster_batch_applied", actor_uid=actor_uid or None, institution_id=job.institution_id,
                    detail={"job_id": job_id, "from_row": job.applied_cursor, "to_row": next_cursor, **counts}, now=now,
                )
            )

        merged = {r.row_index: r for r in job.row_results}
        merged.update({r.row_index: r for r in final})  # a replayed slice replaces its earlier entries
        ordered = [merged[index] for index in sorted(merged)]
        new_status = RosterImportStatus.COMPLETED if next_cursor >= job.total_rows else RosterImportStatus.APPLYING
        self._jobs.update(
            job_id,
            {
                "applied_cursor": next_cursor,
                "row_results": [r.model_dump(mode="json", exclude_none=True) for r in ordered],
                "status": new_status.value,
                "updated_at": now.isoformat(),
            },
        )
        return self._jobs.get(job_id)

    def assign_teacher(
        self,
        *,
        institution_id: str,
        identity_key: str,
        contact_email: str,
        role_scope: RoleScope,
        actor_uid: str,
        reason: Optional[str] = None,
    ) -> RosterRowResult:
        """Explicit, audited FACULTY/ADMIN grant (super-admin only — the caller
        enforces that). It is a one-row roster operation, so it follows exactly the
        same create/promote/revoked rules as an import instead of a second code
        path."""
        policy = self._policy(institution_id)
        row = RosterRowInput(identity_key=identity_key, contact_email=contact_email, role_scope=role_scope.value)
        key = normalize_identity_key(identity_key, policy)
        existing = self._memberships.get_many(institution_id, [key])
        [planned] = plan_dry_run(
            [row], requester_max_role_scope=RoleScope.ADMIN, existing_memberships=existing,
            can_change_contact_email=True, policy=policy,
        )
        if not planned.ok:
            raise RosterRowRejectedError(planned.error or "rejected")
        if planned.action == "skip_unchanged":
            return planned
        final = self._write_row(
            institution_id, row, planned, now=_utc_now(), actor_uid=actor_uid, via="assignment", reason=reason,
            requester_max_role_scope=RoleScope.ADMIN, can_change_contact_email=True,
        )
        if not final.ok:
            raise RosterRowRejectedError(final.error or "rejected")
        return final

    def _write_row(
        self,
        institution_id: str,
        row: RosterRowInput,
        result: RosterRowResult,
        *,
        now: datetime,
        actor_uid: str,
        via: str,
        requester_max_role_scope: RoleScope,
        can_change_contact_email: bool,
        reason: Optional[str] = None,
    ) -> RosterRowResult:
        key = result.identity_key
        email = normalize_email(row.contact_email)
        role = RoleScope(row.role_scope)

        if result.action == "create" and role == RoleScope.STUDENT and via == "roster":
            created = self._memberships.create_if_absent(
                Membership(
                    institution_id=institution_id, identity_key=key, contact_email=email, role_scope=role,
                    status=MembershipStatus.ACTIVE, uid=None, authorization_version=1, created_at=now, updated_at=now,
                )
            )
            if created:
                return result
            return result.model_copy(update={"ok": False, "action": "", "error": "membership_created_concurrently"})

        # Anything that changes who an identity is: re-check against the stored
        # membership and commit the change together with its audit event.
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> RosterRowResult:
            current = self._memberships.get_in_transaction(transaction, institution_id, key)
            decision = classify_row(
                result.model_copy(update={"action": ""}), row,
                existing_memberships={key: current} if current else {},
                requester_max_role_scope=requester_max_role_scope, can_change_contact_email=can_change_contact_email,
            )
            if not decision.ok or decision.action == "skip_unchanged":
                return decision

            detail = {"identity_key": key, "via": via, "role_scope": role.value, "reason": reason}
            if current is None:
                self._memberships.create_in_transaction(
                    transaction,
                    Membership(
                        institution_id=institution_id, identity_key=key, contact_email=email, role_scope=role,
                        status=MembershipStatus.ACTIVE, uid=None, authorization_version=1, created_at=now, updated_at=now,
                    ),
                )
                kind = "teacher_assigned" if via == "assignment" else "membership_role_granted"
            else:
                role_changed = role != current.role_scope
                self._memberships.update_in_transaction(
                    transaction, institution_id, key,
                    {
                        "contact_email": email, "role_scope": role.value, "updated_at": now.isoformat(),
                        "authorization_version": current.authorization_version + 1,
                    },
                )
                detail.update(
                    previous_role_scope=current.role_scope.value,
                    contact_changed=email != current.contact_email,
                    was_linked=current.uid is not None,
                )
                kind = "teacher_assigned" if via == "assignment" else (
                    "membership_role_changed" if role_changed else "membership_contact_changed"
                )
            self._audit.append_in_transaction(
                transaction,
                build_audit_event(
                    kind, uid=(current.uid or "") if current else "", actor_uid=actor_uid or None,
                    institution_id=institution_id, detail=detail, now=now,
                ),
            )
            return decision

        decided = _txn(transaction)
        invalidate_membership_cache(self._db)
        return decided
