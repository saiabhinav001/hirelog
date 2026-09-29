"""Pure normalization + roster-import decision tests: leading-zero
preservation, duplicate/collision detection, cross-institution/role-grant
authorization, and resumable-apply bounded batching."""

from __future__ import annotations

from app.domain.institution.models import RoleScope, RosterRowInput
from app.domain.institution.normalization import make_membership_doc_id, normalize_email, normalize_identity_key
from app.domain.institution.roster_import import can_grant_role, plan_apply_batch, plan_dry_run


def test_normalize_preserves_leading_zeros() -> None:
    assert normalize_identity_key("007A") == "007A"
    assert normalize_identity_key(" 0012 ") == "0012"


def test_normalize_is_case_and_whitespace_insensitive() -> None:
    assert normalize_identity_key("cs-2024\t007") == "CS-2024 007"
    assert normalize_identity_key("CS-2024   007") == "CS-2024 007"


def test_make_membership_doc_id_is_stable_and_scoped_per_institution() -> None:
    a = make_membership_doc_id("inst-1", "007")
    b = make_membership_doc_id("inst-2", "007")
    assert a != b  # same identity_key in two institutions never collides
    assert make_membership_doc_id("inst-1", " 007 ") == a


def test_normalize_email_lowercases_and_trims() -> None:
    assert normalize_email("  Student@College.EDU ") == "student@college.edu"


def test_can_grant_role_matrix() -> None:
    assert can_grant_role(RoleScope.ADMIN, RoleScope.FACULTY)
    assert can_grant_role(RoleScope.ADMIN, RoleScope.ADMIN)
    assert can_grant_role(RoleScope.FACULTY, RoleScope.STUDENT)
    assert not can_grant_role(RoleScope.FACULTY, RoleScope.ADMIN)
    assert not can_grant_role(RoleScope.STUDENT, RoleScope.FACULTY)


def _row(identity_key: str, email: str = "a@college.edu", role_scope: str = "student") -> RosterRowInput:
    return RosterRowInput(identity_key=identity_key, contact_email=email, role_scope=role_scope)


def test_dry_run_detects_duplicate_within_file() -> None:
    rows = [_row("007"), _row("007")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set())
    assert results[0].ok
    assert not results[1].ok
    assert results[1].error == "duplicate_identity_key_in_file"


def test_dry_run_rejects_unauthorized_role_grant() -> None:
    rows = [_row("001", role_scope="admin")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.FACULTY, existing_identity_keys=set())
    assert not results[0].ok
    assert "not_authorized_to_grant" in results[0].error


def test_dry_run_allows_authorized_role_grant() -> None:
    rows = [_row("001", role_scope="student")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.FACULTY, existing_identity_keys=set())
    assert results[0].ok
    assert results[0].action == "create"


def test_dry_run_marks_existing_identity_as_update() -> None:
    rows = [_row("001")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys={"001"})
    assert results[0].ok
    assert results[0].action == "update"


def test_dry_run_rejects_invalid_email() -> None:
    rows = [_row("001", email="not-an-email")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set())
    assert not results[0].ok
    assert results[0].error == "invalid_contact_email"


def test_dry_run_rejects_unknown_role_scope() -> None:
    rows = [_row("001", role_scope="super-admin")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set())
    assert not results[0].ok
    assert "unknown_role_scope" in results[0].error


def test_leading_zeros_preserved_through_full_dry_run_plan() -> None:
    rows = [_row("00042")]
    results = plan_dry_run(rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set())
    assert results[0].identity_key == "00042"


def test_apply_batch_is_bounded_and_returns_resumable_cursor() -> None:
    rows = [_row(f"{i:03d}") for i in range(10)]
    results, next_cursor = plan_apply_batch(
        rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set(), cursor=0, batch_size=4
    )
    assert len(results) == 4
    assert next_cursor == 4

    results2, next_cursor2 = plan_apply_batch(
        rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set(), cursor=4, batch_size=4
    )
    assert len(results2) == 4
    assert next_cursor2 == 8

    results3, next_cursor3 = plan_apply_batch(
        rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set(), cursor=8, batch_size=4
    )
    assert len(results3) == 2  # only the remaining 2 rows
    assert next_cursor3 == 10


def test_apply_batch_resumed_from_same_cursor_is_deterministic() -> None:
    """Simulates a worker restart: calling with the SAME cursor again
    produces the same plan (idempotent re-plan; the caller's upsert-by-key
    write is what makes actually re-applying it harmless)."""
    rows = [_row(f"{i:03d}") for i in range(5)]
    first, cursor1 = plan_apply_batch(
        rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set(), cursor=0, batch_size=3
    )
    second, cursor2 = plan_apply_batch(
        rows, requester_max_role_scope=RoleScope.ADMIN, existing_identity_keys=set(), cursor=0, batch_size=3
    )
    assert [r.identity_key for r in first] == [r.identity_key for r in second]
    assert cursor1 == cursor2
