"""Pure roster-import decisions: dry-run validation and bounded, resumable
apply planning. No I/O here — the service layer supplies existing state
(already-seen identity keys, already-applied cursor) and persists the result.

Removing a row from a re-uploaded file is NEVER a delete here — apply only
ever creates/updates rows present in the current batch. Deactivating a
membership is a separate, explicit action (see auth_service.py), never an
implicit side effect of a roster re-import.
"""

from __future__ import annotations

from app.domain.institution.models import RosterRowInput, RosterRowResult, RoleScope
from app.domain.institution.normalization import normalize_email, normalize_identity_key

_ROLE_SCOPE_PRIORITY = {
    RoleScope.STUDENT: 0,
    RoleScope.FACULTY: 1,
    RoleScope.PROCTOR: 1,
    RoleScope.ADMIN: 2,
}


def can_grant_role(requester_max: RoleScope, requested: RoleScope) -> bool:
    return _ROLE_SCOPE_PRIORITY[requested] <= _ROLE_SCOPE_PRIORITY[requester_max]


def _validate_one_row(
    row_index: int, row: RosterRowInput, *, requester_max_role_scope: RoleScope, seen_keys: set[str],
    existing_identity_keys: frozenset[str] | set[str] = frozenset(),
) -> RosterRowResult:
    identity_key = normalize_identity_key(row.identity_key)

    if identity_key in seen_keys:
        return RosterRowResult(row_index=row_index, identity_key=identity_key, ok=False, error="duplicate_identity_key_in_file")

    try:
        requested_scope = RoleScope(row.role_scope)
    except ValueError:
        return RosterRowResult(row_index=row_index, identity_key=identity_key, ok=False, error=f"unknown_role_scope:{row.role_scope}")

    # Overwriting an existing member can re-point their contact email (and so
    # their OTP sign-in, including a linked legacy account) or change their
    # role — only an institution admin may do that, never a faculty importer.
    if identity_key in existing_identity_keys and requester_max_role_scope != RoleScope.ADMIN:
        return RosterRowResult(row_index=row_index, identity_key=identity_key, ok=False, error="existing_membership_requires_admin")

    if not can_grant_role(requester_max_role_scope, requested_scope):
        return RosterRowResult(
            row_index=row_index, identity_key=identity_key, ok=False,
            error=f"uploader_not_authorized_to_grant:{requested_scope.value}",
        )

    email = normalize_email(row.contact_email)
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return RosterRowResult(row_index=row_index, identity_key=identity_key, ok=False, error="invalid_contact_email")

    return RosterRowResult(row_index=row_index, identity_key=identity_key, ok=True)


def plan_dry_run(
    rows: list[RosterRowInput], *, requester_max_role_scope: RoleScope, existing_identity_keys: set[str]
) -> list[RosterRowResult]:
    """Validates the WHOLE batch and returns a plan — applies nothing."""
    seen: set[str] = set()
    results: list[RosterRowResult] = []
    for index, row in enumerate(rows):
        result = _validate_one_row(
            index, row, requester_max_role_scope=requester_max_role_scope, seen_keys=seen,
            existing_identity_keys=existing_identity_keys,
        )
        if result.ok:
            seen.add(result.identity_key)
            result = result.model_copy(
                update={"action": "update" if result.identity_key in existing_identity_keys else "create"}
            )
        results.append(result)
    return results


def plan_apply_batch(
    rows: list[RosterRowInput],
    *,
    requester_max_role_scope: RoleScope,
    existing_identity_keys: set[str],
    cursor: int,
    batch_size: int,
) -> tuple[list[RosterRowResult], int]:
    """Bounded, resumable: validates + plans only rows[cursor:cursor+batch_size].
    Returns (results_for_this_batch, next_cursor). A crash after this call
    (before the caller persists `next_cursor`) is safe to retry — the same
    slice is simply re-planned, and only rows that end up `ok=True` cause any
    write in the caller, which upserts by identity_key (idempotent)."""

    end = min(len(rows), cursor + max(1, batch_size))
    slice_rows = rows[cursor:end]

    seen: set[str] = set()
    results: list[RosterRowResult] = []
    for offset, row in enumerate(slice_rows):
        row_index = cursor + offset
        result = _validate_one_row(
            row_index, row, requester_max_role_scope=requester_max_role_scope, seen_keys=seen,
            existing_identity_keys=existing_identity_keys,
        )
        if result.ok:
            seen.add(result.identity_key)
            result = result.model_copy(
                update={"action": "update" if result.identity_key in existing_identity_keys else "create"}
            )
        results.append(result)
    return results, end
