"""Bridges Prompt 2 institution membership into Prompt 3's enrollment
snapshot — the exact seam ADR 0008 reserved ("swap the source of this
snapshot... without changing anything below this point"). Does not modify
`exams/commands.py`/`models.py` at all; it only resolves a UID list that the
existing `ExamCommandService.create_run(..., enrolled_uids=...)` already
accepts.
"""

from __future__ import annotations

from app.domain.institution.models import MembershipStatus, RoleScope
from app.domain.institution.repository import MembershipRepository


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
