"""Pure roster planner tests for Prompt 2 (docs/adr/0018): per-institution
normalization, collisions, duplicates across batches, stored-state rules and
replay. Complements test_institution_roster.py (the original planner tests)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.institution.models import IdentityKeyPolicy, Membership, MembershipStatus, RoleScope, RosterRowInput
from app.domain.institution.normalization import (
    identity_key_problem,
    is_valid_contact_email,
    normalize_identity_key,
)
from app.domain.institution.roster_csv import RosterParseError
from app.domain.institution.roster_import import (
    batch_identity_keys,
    plan_apply_batch,
    plan_dry_run,
    rows_digest,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
SEPARATORS = IdentityKeyPolicy(remove_characters="-/. ")


def _row(key: str, email: str = "a@college.edu", role: str = "student") -> RosterRowInput:
    return RosterRowInput(identity_key=key, contact_email=email, role_scope=role)


def _member(key: str, *, email: str = "a@college.edu", role: RoleScope = RoleScope.STUDENT,
            status: MembershipStatus = MembershipStatus.ACTIVE, uid: str | None = None) -> Membership:
    return Membership(institution_id="i1", identity_key=key, contact_email=email, role_scope=role, status=status,
                      uid=uid, authorization_version=1, created_at=NOW, updated_at=NOW)


def _plan(rows, existing=None, *, max_scope=RoleScope.ADMIN, can_change_email=False, policy=None):
    return plan_dry_run(rows, requester_max_role_scope=max_scope, existing_memberships=existing or {},
                        can_change_contact_email=can_change_email, policy=policy)


# ── per-institution normalization ────────────────────────────────────────────

def test_policy_removes_the_institutions_separator_characters() -> None:
    assert normalize_identity_key("21-bce/0001 .", SEPARATORS) == "21BCE0001"
    assert normalize_identity_key("21-bce/0001", None) == "21-BCE/0001"  # no policy: universal step only


@pytest.mark.parametrize("raw", ["21-bce-0001", "  007a ", "A - B  C", "00012", "x.y/z"])
def test_policy_output_is_a_fixed_point_of_the_universal_step(raw: str) -> None:
    """Membership doc ids re-normalize without the policy, so they must not change."""
    once = normalize_identity_key(raw, SEPARATORS)
    assert normalize_identity_key(once) == once


def test_same_id_in_two_institutions_never_collides_even_with_different_policies() -> None:
    from app.domain.institution.normalization import make_membership_doc_id

    assert make_membership_doc_id("i1", "21BCE0001") != make_membership_doc_id("i2", "21BCE0001")


def test_formatting_variants_collide_and_are_reported_against_the_first_row() -> None:
    results = _plan([_row("21-BCE-0001"), _row("21bce0001", "b@college.edu"), _row("21/bce/0001", "c@college.edu")],
                    policy=SEPARATORS)
    assert results[0].ok and results[0].identity_key == "21BCE0001"
    assert [(r.ok, r.error, r.first_seen_row) for r in results[1:]] == [
        (False, "duplicate_identity_key_in_file", 0),
        (False, "duplicate_identity_key_in_file", 0),
    ]


def test_without_a_policy_hyphen_variants_stay_distinct() -> None:
    results = _plan([_row("21-BCE-0001"), _row("21BCE0001", "b@college.edu")])
    assert all(r.ok for r in results)


def test_invisible_and_control_characters_are_rejected() -> None:
    results = _plan([_row("21BCE\u200b0001"), _row("21BCE\x07001")])
    assert [r.error for r in results] == ["identity_key_invalid:invalid_characters"] * 2


def test_policy_length_and_charset_rules_apply() -> None:
    policy = IdentityKeyPolicy(remove_characters="-", min_length=6, max_length=10, charset="alphanumeric")
    results = _plan([_row("AB-12"), _row("ABCDEFGHIJK"), _row("AB_123456"), _row("AB-123456")], policy=policy)
    assert [r.error for r in results] == [
        "identity_key_invalid:too_short",
        "identity_key_invalid:too_long",
        "identity_key_invalid:not_alphanumeric",
        None,
    ]


def test_an_inverted_policy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IdentityKeyPolicy(min_length=10, max_length=5)


def test_identity_key_problem_directly() -> None:
    assert identity_key_problem("") == "empty"
    assert identity_key_problem("007A") is None


def test_contact_email_shape() -> None:
    assert is_valid_contact_email("a@college.edu")
    for bad in ("a@b", "a b@college.edu", "a@college.edu\r\nbcc: x@y.z", "@college.edu", "a@@college.edu", "a" * 250 + "@x.io"):
        assert not is_valid_contact_email(bad), bad


# ── duplicates and parse errors across the whole file ───────────────────────

def test_a_duplicate_is_caught_even_when_it_falls_in_a_later_batch() -> None:
    rows = [_row(f"{i:03d}", f"s{i}@college.edu") for i in range(9)] + [_row("000", "other@college.edu")]
    batch_one, cursor = plan_apply_batch(rows, requester_max_role_scope=RoleScope.ADMIN, cursor=0, batch_size=5)
    batch_two, end = plan_apply_batch(rows, requester_max_role_scope=RoleScope.ADMIN, cursor=cursor, batch_size=5)
    assert all(r.ok for r in batch_one)
    assert [r.row_index for r in batch_two] == [5, 6, 7, 8, 9] and end == 10
    assert batch_two[-1].error == "duplicate_identity_key_in_file" and batch_two[-1].first_seen_row == 0


def test_parse_errors_are_reported_in_place_and_do_not_reserve_the_key() -> None:
    rows = [RosterParseError(line_number=4, identity_key="007a", error="csv_row_has_1_fields_expected_2"), _row("007A")]
    results = _plan(rows)
    assert (results[0].ok, results[0].error, results[0].line_number) == (False, "csv_row_has_1_fields_expected_2", 4)
    assert results[0].identity_key == "007A"
    assert results[1].ok  # the failed line did not make the later row a duplicate


def test_leading_zero_ids_are_kept_distinct() -> None:
    results = _plan([_row("007"), _row("7", "b@college.edu"), _row("0007", "c@college.edu")])
    assert [r.identity_key for r in results] == ["007", "7", "0007"] and all(r.ok for r in results)


# ── role grants ─────────────────────────────────────────────────────────────

def test_student_level_uploaders_cannot_grant_faculty_or_admin() -> None:
    results = _plan([_row("001", role="faculty"), _row("002", "b@college.edu", role="admin")], max_scope=RoleScope.STUDENT)
    assert [r.error for r in results] == ["uploader_not_authorized_to_grant:faculty", "uploader_not_authorized_to_grant:admin"]


def test_a_super_admin_level_uploader_can_grant_faculty() -> None:
    assert _plan([_row("001", role="faculty")], max_scope=RoleScope.ADMIN)[0].ok


# ── stored-state rules ──────────────────────────────────────────────────────

def test_an_import_never_undoes_a_revocation() -> None:
    existing = {"001": _member("001", status=MembershipStatus.REVOKED)}
    result = _plan([_row("001")], existing)[0]
    assert (result.ok, result.error) == (False, "membership_revoked_reinstate_required")


def test_an_uploader_cannot_modify_a_member_who_outranks_them() -> None:
    existing = {"001": _member("001", role=RoleScope.FACULTY), "002": _member("002", role=RoleScope.ADMIN)}
    results = _plan([_row("001"), _row("002", "b@college.edu")], existing, max_scope=RoleScope.STUDENT, can_change_email=True)
    assert [r.error for r in results] == ["uploader_not_authorized_to_modify:faculty", "uploader_not_authorized_to_modify:admin"]


def test_changing_an_existing_contact_email_needs_administrator_authority() -> None:
    existing = {"001": _member("001", email="old@college.edu", uid="u1")}
    denied = _plan([_row("001", "new@college.edu")], existing, can_change_email=False)[0]
    allowed = _plan([_row("001", "new@college.edu")], existing, can_change_email=True)[0]
    assert (denied.ok, denied.error) == (False, "contact_email_change_requires_admin")
    assert (allowed.ok, allowed.action) == (True, "update")


def test_the_same_email_is_unchanged_regardless_of_case_and_whitespace() -> None:
    existing = {"001": _member("001", email="old@college.edu")}
    result = _plan([_row("001", "  OLD@College.edu ")], existing)[0]
    assert (result.ok, result.action) == (True, "skip_unchanged")


def test_promotion_is_an_update_that_needs_the_right_to_grant_the_new_role() -> None:
    existing = {"001": _member("001")}
    assert _plan([_row("001", role="faculty")], existing, max_scope=RoleScope.ADMIN)[0].action == "update"
    assert _plan([_row("001", role="faculty")], existing, max_scope=RoleScope.STUDENT)[0].error == (
        "uploader_not_authorized_to_grant:faculty"
    )


def test_replaying_a_plan_after_it_was_applied_changes_nothing() -> None:
    rows = [_row("001", "a@college.edu"), _row("002", "b@college.edu"), _row("003", "c@college.edu")]
    first = _plan(rows)
    assert [r.action for r in first] == ["create"] * 3
    stored = {r.identity_key: _member(r.identity_key, email=rows[i].contact_email) for i, r in enumerate(first)}
    replay = _plan(rows, stored)
    assert [(r.ok, r.action) for r in replay] == [(True, "skip_unchanged")] * 3


def test_a_legacy_key_set_still_classifies_create_versus_update() -> None:
    results = plan_dry_run([_row("001"), _row("002", "b@college.edu")], requester_max_role_scope=RoleScope.ADMIN,
                           existing_identity_keys={"001"})
    assert [r.action for r in results] == ["update", "create"]


# ── digest and batch keys ───────────────────────────────────────────────────

def test_digest_is_stable_normalized_and_sensitive_to_every_field() -> None:
    base = [_row("007a", "A@College.edu"), _row("008", "b@college.edu", "student")]
    same = [_row("007A", "a@college.edu"), _row("008", "b@college.edu", "student")]
    assert rows_digest(base) == rows_digest(same)
    assert rows_digest(base) != rows_digest([base[0], _row("008", "other@college.edu")])
    assert rows_digest(base) != rows_digest([base[0], _row("008", "b@college.edu", "faculty")])
    assert rows_digest(base) != rows_digest(base[:1])
    assert rows_digest(base) != rows_digest(list(reversed(base)))
    with_error = [*base, RosterParseError(9, "x", "csv_row_has_1_fields_expected_2")]
    assert rows_digest(with_error) != rows_digest(base)


def test_digest_depends_on_the_institution_policy() -> None:
    rows = [_row("21-BCE-0001")]
    assert rows_digest(rows) != rows_digest(rows, policy=SEPARATORS)


def test_batch_identity_keys_are_unique_normalized_and_skip_bad_rows() -> None:
    rows = [_row("21-bce-0001"), _row("21BCE0001", "b@college.edu"), RosterParseError(3, "x", "e"), _row("bad\u200bid", "c@college.edu"),
            _row("22BCE0002", "d@college.edu")]
    assert batch_identity_keys(rows, policy=SEPARATORS) == ["21BCE0001", "22BCE0002"]
    assert batch_identity_keys(rows, policy=SEPARATORS, cursor=4, batch_size=10) == ["22BCE0002"]
