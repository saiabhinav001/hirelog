"""Bridges Prompt 2 institution membership into Prompt 3's enrollment
snapshot — the exact seam ADR 0008 reserved ("swap the source of this
snapshot... without changing anything below this point"). Does not modify
`exams/commands.py`/`models.py` at all; it only resolves a UID list that the
existing `ExamCommandService.create_run(..., enrolled_uids=...)` already
accepts.

It also hosts co-instructor assignment ("definition = course", ADR 0018), the
other place where an exam resource and institution memberships meet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from firebase_admin import firestore

from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.repository import ExamDefinitionRepository
from app.domain.institution.models import MembershipStatus, RoleScope
from app.domain.institution.normalization import normalize_identity_key
from app.domain.institution.repository import InstitutionRepository, MembershipRepository


class InstructorAssignmentError(Exception):
    def __init__(self, code: str, problems: Optional[list[dict]] = None) -> None:
        super().__init__(code)
        self.code = code
        self.problems = problems or []


def assign_instructors(db, *, definition_id: str, identity_keys: Sequence[str], actor_uid: str) -> list[str]:
    """Replace a definition's co-instructors. Each identity key must belong to a
    linked, ACTIVE FACULTY/ADMIN member of the definition's institution; a
    teacher who has not signed in yet has no uid to assign. The change and its
    audit event commit together."""
    definitions = ExamDefinitionRepository(db)
    definition = definitions.get(definition_id)
    if definition is None:
        raise InstructorAssignmentError("definition_not_found")
    if not definition.institution_id:
        raise InstructorAssignmentError("definition_not_institution_scoped")

    institution = InstitutionRepository(db).get(definition.institution_id)
    policy = institution.identity_policy if institution else None
    keys = [normalize_identity_key(key, policy) for key in identity_keys]
    members = MembershipRepository(db).get_many(definition.institution_id, keys)

    uids: list[str] = []
    problems: list[dict] = []
    for key in dict.fromkeys(keys):
        member = members.get(key)
        if member is None:
            problems.append({"identity_key": key, "reason": "not_a_member"})
        elif member.status != MembershipStatus.ACTIVE:
            problems.append({"identity_key": key, "reason": "membership_not_active"})
        elif member.role_scope not in (RoleScope.FACULTY, RoleScope.ADMIN):
            problems.append({"identity_key": key, "reason": "not_faculty"})
        elif not member.uid:
            problems.append({"identity_key": key, "reason": "not_linked_yet"})
        else:
            uids.append(member.uid)
    if problems:
        raise InstructorAssignmentError("instructors_not_assignable", problems)
    uids = list(dict.fromkeys(uids))

    now = datetime.now(timezone.utc)
    audit = AuthzAuditRepository(db)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(transaction) -> None:
        current = definitions.get_in_transaction(transaction, definition_id)
        if current is None:
            raise InstructorAssignmentError("definition_not_found")
        definitions.set_instructors_in_transaction(transaction, definition_id, uids, now)
        audit.append_in_transaction(
            transaction,
            build_audit_event(
                "instructors_set", actor_uid=actor_uid, institution_id=current.institution_id, now=now,
                detail={"definition_id": definition_id, "previous": current.instructor_uids, "instructor_uids": uids},
            ),
        )

    _txn(transaction)
    return uids


def resolve_enrolled_uids_from_institution(
    db, institution_id: str, *, role_scope: RoleScope = RoleScope.STUDENT
) -> list[str]:
    """Only ACTIVE memberships that have actually logged in at least once
    (uid provisioned) can be enrolled — a student who has never completed
    OTP verification has no Firebase UID yet to admit into an attempt."""
    repo = MembershipRepository(db)
    existing_keys = repo.existing_identity_keys(institution_id)
    uids: list[str] = []
    for identity_key in existing_keys:
        membership = repo.get(institution_id, identity_key)
        if (
            membership
            and membership.status == MembershipStatus.ACTIVE
            and membership.role_scope == role_scope
            and membership.uid
        ):
            uids.append(membership.uid)
    return uids
