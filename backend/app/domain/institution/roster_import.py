"""Pure roster-import decisions: dry-run validation and bounded, resumable
apply planning. No I/O here — the service layer supplies existing state
(already-seen identity keys, already-applied cursor) and persists the result.

Removing a row from a re-uploaded file is NEVER a delete here — apply only
ever creates/updates rows present in the current batch. Deactivating a
membership is a separate, explicit action (see auth_service.py), never an
implicit side effect of a roster re-import.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, Optional, Sequence

from app.domain.institution.models import (
    IdentityKeyPolicy,
    Membership,
    MembershipStatus,
    RoleScope,
    RosterRowInput,
    RosterRowResult,
)
from app.domain.institution.normalization import (
    identity_key_problem,
    is_valid_contact_email,
    normalize_email,
    normalize_identity_key,
)
from app.domain.institution.roster_csv import RosterParseError, RosterRowCandidate

_ROLE_SCOPE_PRIORITY = {
    RoleScope.STUDENT: 0,
    RoleScope.FACULTY: 1,
    RoleScope.PROCTOR: 1,
    RoleScope.ADMIN: 2,
}


def can_grant_role(requester_max: RoleScope, requested: RoleScope) -> bool:
    return _ROLE_SCOPE_PRIORITY[requested] <= _ROLE_SCOPE_PRIORITY[requester_max]


def _fail(index: int, identity_key: str, error: str, **extra) -> RosterRowResult:
    return RosterRowResult(row_index=index, identity_key=identity_key, ok=False, error=error, **extra)


def _structural_result(
    index: int,
    row: RosterRowCandidate,
    *,
    requester_max_role_scope: RoleScope,
    policy: Optional[IdentityKeyPolicy],
    seen: dict[str, int],
) -> RosterRowResult:
    """Everything decidable from the file alone (no stored state)."""
    if isinstance(row, RosterParseError):
        return _fail(
            index, normalize_identity_key(row.identity_key, policy), row.error, line_number=row.line_number
        )

    identity_key = normalize_identity_key(row.identity_key, policy)
    problem = identity_key_problem(identity_key, policy)
    if problem:
        return _fail(index, identity_key, f"identity_key_invalid:{problem}")

    if identity_key in seen:
        return _fail(index, identity_key, "duplicate_identity_key_in_file", first_seen_row=seen[identity_key])

    try:
        requested_scope = RoleScope(row.role_scope)
    except ValueError:
        return _fail(index, identity_key, f"unknown_role_scope:{row.role_scope}")

    if not can_grant_role(requester_max_role_scope, requested_scope):
        return _fail(index, identity_key, f"uploader_not_authorized_to_grant:{requested_scope.value}")

    if not is_valid_contact_email(normalize_email(row.contact_email)):
        return _fail(index, identity_key, "invalid_contact_email")

    return RosterRowResult(row_index=index, identity_key=identity_key, ok=True)


def _structural_pass(
    rows: Sequence[RosterRowCandidate], *, requester_max_role_scope: RoleScope, policy: Optional[IdentityKeyPolicy]
) -> list[RosterRowResult]:
    """Runs over the WHOLE file even when only one batch is being applied, so a
    duplicate or colliding identity is caught no matter which batch it is in."""
    seen: dict[str, int] = {}
    results: list[RosterRowResult] = []
    for index, row in enumerate(rows):
        result = _structural_result(
            index, row, requester_max_role_scope=requester_max_role_scope, policy=policy, seen=seen
        )
        if result.ok:
            seen[result.identity_key] = index
        results.append(result)
    return results


def classify_row(
    result: RosterRowResult,
    row: RosterRowInput,
    *,
    existing_memberships: Optional[Mapping[str, Membership]],
    existing_identity_keys: frozenset[str] | set[str] = frozenset(),
    requester_max_role_scope: RoleScope,
    can_change_contact_email: bool,
) -> RosterRowResult:
    """Decide create / update / skip_unchanged against what is already stored.
    An import may add rows and edit what its uploader is allowed to edit; it may
    never undo a revocation, outrank its uploader, or re-point the OTP
    destination of an existing identity without administrator authority."""
    key = result.identity_key
    if existing_memberships is None:
        return result.model_copy(update={"action": "update" if key in existing_identity_keys else "create"})

    existing = existing_memberships.get(key)
    if existing is None:
        return result.model_copy(update={"action": "create"})

    def reject(error: str) -> RosterRowResult:
        return result.model_copy(update={"ok": False, "action": "", "error": error})

    if existing.status == MembershipStatus.REVOKED:
        return reject("membership_revoked_reinstate_required")
    if not can_grant_role(requester_max_role_scope, existing.role_scope):
        return reject(f"uploader_not_authorized_to_modify:{existing.role_scope.value}")

    email_changed = normalize_email(row.contact_email) != existing.contact_email
    if email_changed and not can_change_contact_email:
        return reject("contact_email_change_requires_admin")
    if not email_changed and RoleScope(row.role_scope) == existing.role_scope:
        return result.model_copy(update={"action": "skip_unchanged"})
    return result.model_copy(update={"action": "update"})


def plan_dry_run(
    rows: Sequence[RosterRowCandidate],
    *,
    requester_max_role_scope: RoleScope,
    existing_identity_keys: frozenset[str] | set[str] = frozenset(),
    existing_memberships: Optional[Mapping[str, Membership]] = None,
    can_change_contact_email: bool = False,
    policy: Optional[IdentityKeyPolicy] = None,
) -> list[RosterRowResult]:
    """Validates the WHOLE batch and returns a plan — applies nothing. Pass
    `existing_memberships` (keyed by normalized identity key) to enforce the
    stored-state rules; with only `existing_identity_keys` rows are classified
    create/update and nothing else."""
    structural = _structural_pass(rows, requester_max_role_scope=requester_max_role_scope, policy=policy)
    return [
        classify_row(
            result, rows[index], existing_memberships=existing_memberships,
            existing_identity_keys=existing_identity_keys, requester_max_role_scope=requester_max_role_scope,
            can_change_contact_email=can_change_contact_email,
        )
        if result.ok
        else result
        for index, result in enumerate(structural)
    ]


def plan_apply_batch(
    rows: Sequence[RosterRowCandidate],
    *,
    requester_max_role_scope: RoleScope,
    existing_identity_keys: frozenset[str] | set[str] = frozenset(),
    existing_memberships: Optional[Mapping[str, Membership]] = None,
    can_change_contact_email: bool = False,
    policy: Optional[IdentityKeyPolicy] = None,
    cursor: int,
    batch_size: int,
) -> tuple[list[RosterRowResult], int]:
    """Bounded, resumable: plans only rows[cursor:cursor+batch_size], but judges
    them against the whole file (a duplicate in an earlier batch still counts).
    Returns (results_for_this_batch, next_cursor). A crash after this call
    (before the caller persists `next_cursor`) is safe to retry — the same
    slice is re-planned, and rows already written classify as `skip_unchanged`."""
    end = min(len(rows), cursor + max(1, batch_size))
    start = min(cursor, end)
    structural = _structural_pass(rows, requester_max_role_scope=requester_max_role_scope, policy=policy)
    results = [
        classify_row(
            structural[index], rows[index], existing_memberships=existing_memberships,
            existing_identity_keys=existing_identity_keys, requester_max_role_scope=requester_max_role_scope,
            can_change_contact_email=can_change_contact_email,
        )
        if structural[index].ok
        else structural[index]
        for index in range(start, end)
    ]
    return results, end


def batch_identity_keys(
    rows: Sequence[RosterRowCandidate],
    *,
    policy: Optional[IdentityKeyPolicy] = None,
    cursor: int = 0,
    batch_size: Optional[int] = None,
) -> list[str]:
    """Normalized keys of the importable rows in rows[cursor:cursor+batch_size]
    (all remaining rows when `batch_size` is None) — what the caller must load
    from storage before planning."""
    end = len(rows) if batch_size is None else min(len(rows), cursor + max(1, batch_size))
    keys: dict[str, None] = {}
    for row in rows[cursor:end]:
        if isinstance(row, RosterParseError):
            continue
        key = normalize_identity_key(row.identity_key, policy)
        if identity_key_problem(key, policy) is None:
            keys[key] = None
    return list(keys)


def rows_digest(rows: Sequence[RosterRowCandidate], *, policy: Optional[IdentityKeyPolicy] = None) -> str:
    """Fingerprint of what the dry-run saw. Apply must present rows with the same
    digest, so a job can only ever apply the file that was validated."""
    canonical: list[list] = []
    for row in rows:
        if isinstance(row, RosterParseError):
            canonical.append(["err", row.line_number, row.error])
        else:
            canonical.append(
                ["row", normalize_identity_key(row.identity_key, policy), normalize_email(row.contact_email), row.role_scope]
            )
    payload = json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
