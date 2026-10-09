"""Pure policy tests for the Prompt 1 exam role/scope model (ADR 0017).

No Firestore, no FastAPI: these pin the rules every dependency builds on —
verified-email staff grants, role mapping onto the existing `RoleScope`,
owner/institution resource scope, student institution scope, token
membership freshness and creation-time institution selection.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.authz.policy import (
    ExamPrincipal,
    ExamRole,
    InstitutionRequiredError,
    ScopeDeniedError,
    StaffGrants,
    can_manage_resource,
    can_participate_as_student,
    check_token_membership_freshness,
    max_grantable_role_scope,
    resolve_creation_institution,
    resolve_staff_grants,
)
from app.domain.institution.errors import MembershipNotActiveError, StaleAuthorizationVersionError
from app.domain.institution.models import Membership, MembershipStatus, RoleScope

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _m(institution_id: str, role: RoleScope, *, status: MembershipStatus = MembershipStatus.ACTIVE,
       uid: str = "u1", version: int = 1) -> Membership:
    return Membership(
        institution_id=institution_id, identity_key=f"ID-{institution_id}-{role.value}", contact_email="x@college.edu",
        role_scope=role, status=status, uid=uid, authorization_version=version, created_at=NOW, updated_at=NOW,
    )


def _principal(*memberships: Membership, uid: str = "u1", super_admin: bool = False, teacher: bool = False) -> ExamPrincipal:
    return ExamPrincipal(
        uid=uid, email="p@college.edu", email_verified=True,
        bootstrap_super_admin=super_admin, bootstrap_teacher=teacher, memberships=tuple(memberships),
    )


# ── staff grants: verified allowlist only ─────────────────────────────────────

def test_unverified_allowlisted_email_gets_no_authority() -> None:
    grants = resolve_staff_grants(
        email="owner@college.edu", email_verified=False,
        super_admin_emails={"owner@college.edu"}, teacher_emails={"owner@college.edu"},
    )
    assert grants == StaffGrants(super_admin=False, teacher=False)
    assert not grants.any


def test_verified_email_gets_each_allowlisted_role_independently() -> None:
    both = resolve_staff_grants(
        email="owner@college.edu", email_verified=True,
        super_admin_emails={"owner@college.edu"}, teacher_emails={"owner@college.edu"},
    )
    only_teacher = resolve_staff_grants(
        email="prof@college.edu", email_verified=True,
        super_admin_emails={"owner@college.edu"}, teacher_emails={"prof@college.edu"},
    )
    assert both.super_admin and both.teacher
    assert only_teacher.teacher and not only_teacher.super_admin


def test_email_matching_is_case_and_whitespace_insensitive() -> None:
    grants = resolve_staff_grants(
        email="  Prof@College.EDU ", email_verified=True,
        super_admin_emails=set(), teacher_emails={"prof@college.edu"},
    )
    assert grants.teacher


def test_missing_email_gets_nothing_even_when_verified() -> None:
    assert not resolve_staff_grants(
        email="", email_verified=True, super_admin_emails={""}, teacher_emails={""}
    ).any


def test_dev_escape_hatch_grants_unverified_only_when_explicitly_allowed() -> None:
    kwargs = dict(email="prof@college.edu", email_verified=False, super_admin_emails=set(), teacher_emails={"prof@college.edu"})
    assert not resolve_staff_grants(**kwargs).any
    assert resolve_staff_grants(**kwargs, allow_unverified=True).teacher


def test_non_allowlisted_verified_email_gets_nothing() -> None:
    assert not resolve_staff_grants(
        email="student@college.edu", email_verified=True,
        super_admin_emails={"owner@college.edu"}, teacher_emails={"prof@college.edu"},
    ).any


# ── role mapping onto the existing RoleScope model ────────────────────────────

def test_faculty_and_admin_memberships_map_to_teacher() -> None:
    assert ExamRole.TEACHER in _principal(_m("i1", RoleScope.FACULTY)).roles()
    assert ExamRole.TEACHER in _principal(_m("i1", RoleScope.ADMIN)).roles()


def test_student_membership_maps_to_student_only() -> None:
    assert _principal(_m("i1", RoleScope.STUDENT)).roles() == [ExamRole.STUDENT]


def test_proctor_membership_grants_nothing() -> None:
    assert _principal(_m("i1", RoleScope.PROCTOR)).roles() == []


def test_revoked_membership_grants_nothing() -> None:
    principal = _principal(_m("i1", RoleScope.FACULTY, status=MembershipStatus.REVOKED))
    assert not principal.is_teacher
    assert principal.roles() == []


def test_super_admin_implies_teacher_and_lists_both_roles() -> None:
    principal = _principal(super_admin=True)
    assert principal.is_teacher
    assert principal.roles() == [ExamRole.TEACHER, ExamRole.SUPER_ADMIN]


def test_bootstrap_teacher_is_teacher_but_not_super_admin() -> None:
    principal = _principal(teacher=True)
    assert principal.is_teacher and not principal.is_super_admin


# ── resource scope: owner / institution admin / super admin ───────────────────

def test_owner_teacher_can_manage_own_resource() -> None:
    principal = _principal(teacher=True, uid="owner")
    assert can_manage_resource(principal, owner_uid="owner", institution_id=None)


def test_other_teacher_cannot_manage_someone_elses_resource() -> None:
    principal = _principal(teacher=True, uid="other")
    assert not can_manage_resource(principal, owner_uid="owner", institution_id=None)


def test_faculty_member_of_same_institution_is_not_enough_without_ownership() -> None:
    principal = _principal(_m("i1", RoleScope.FACULTY), uid="colleague")
    assert not can_manage_resource(principal, owner_uid="owner", institution_id="i1")


def test_admin_of_the_resources_institution_can_manage_it() -> None:
    principal = _principal(_m("i1", RoleScope.ADMIN), uid="admin")
    assert can_manage_resource(principal, owner_uid="owner", institution_id="i1")


def test_admin_of_a_different_institution_cannot() -> None:
    principal = _principal(_m("i2", RoleScope.ADMIN), uid="admin")
    assert not can_manage_resource(principal, owner_uid="owner", institution_id="i1")


def test_super_admin_can_manage_everything() -> None:
    principal = _principal(super_admin=True, uid="root")
    assert can_manage_resource(principal, owner_uid="owner", institution_id="i1")
    assert can_manage_resource(principal, owner_uid=None, institution_id=None)


def test_non_teacher_owner_cannot_manage_even_their_own_resource() -> None:
    """A lost teacher role (revoked membership / removed from the allowlist)
    ends access immediately, even for the original owner."""
    principal = _principal(uid="owner")
    assert not can_manage_resource(principal, owner_uid="owner", institution_id=None)


def test_unscoped_resource_is_only_reachable_by_owner_or_super_admin() -> None:
    admin = _principal(_m("i1", RoleScope.ADMIN), uid="admin")
    assert not can_manage_resource(admin, owner_uid="owner", institution_id=None)


# ── student institution scope ────────────────────────────────────────────────

def test_unscoped_run_needs_no_institution_membership() -> None:
    assert can_participate_as_student(_principal(), run_institution_id=None)


def test_scoped_run_requires_active_student_membership_in_that_institution() -> None:
    assert can_participate_as_student(_principal(_m("i1", RoleScope.STUDENT)), run_institution_id="i1")
    assert not can_participate_as_student(
        _principal(_m("i1", RoleScope.STUDENT, status=MembershipStatus.REVOKED)), run_institution_id="i1"
    )
    assert not can_participate_as_student(_principal(_m("i2", RoleScope.STUDENT)), run_institution_id="i1")
    assert not can_participate_as_student(_principal(_m("i1", RoleScope.FACULTY)), run_institution_id="i1")
    assert not can_participate_as_student(_principal(), run_institution_id="i1")


# ── roster authority ─────────────────────────────────────────────────────────

def test_max_grantable_role_scope_matrix() -> None:
    # Teacher assignment (granting FACULTY/ADMIN) is a super-admin function (ADR 0018).
    assert max_grantable_role_scope(_principal(super_admin=True), "i1") == RoleScope.ADMIN
    assert max_grantable_role_scope(_principal(_m("i1", RoleScope.ADMIN)), "i1") == RoleScope.STUDENT
    assert max_grantable_role_scope(_principal(_m("i1", RoleScope.FACULTY)), "i1") == RoleScope.STUDENT
    assert max_grantable_role_scope(_principal(_m("i2", RoleScope.ADMIN)), "i1") is None
    assert max_grantable_role_scope(_principal(teacher=True), "i1") is None  # bootstrap teacher is not an institution member
    assert max_grantable_role_scope(
        _principal(_m("i1", RoleScope.ADMIN, status=MembershipStatus.REVOKED)), "i1"
    ) is None


# ── token membership freshness ───────────────────────────────────────────────

def test_fresh_membership_with_matching_version_passes() -> None:
    membership = check_token_membership_freshness(
        [_m("i1", RoleScope.STUDENT, version=3)], institution_id="i1", token_authz_version=3
    )
    assert membership.authorization_version == 3


def test_stale_authorization_version_is_rejected() -> None:
    with pytest.raises(StaleAuthorizationVersionError):
        check_token_membership_freshness([_m("i1", RoleScope.STUDENT, version=2)], institution_id="i1", token_authz_version=1)


def test_revoked_membership_is_rejected() -> None:
    with pytest.raises(MembershipNotActiveError):
        check_token_membership_freshness(
            [_m("i1", RoleScope.STUDENT, status=MembershipStatus.REVOKED, version=2)],
            institution_id="i1", token_authz_version=2,
        )


def test_missing_membership_is_rejected() -> None:
    with pytest.raises(MembershipNotActiveError):
        check_token_membership_freshness([_m("i2", RoleScope.STUDENT)], institution_id="i1", token_authz_version=1)


def test_absent_token_version_still_requires_an_active_membership() -> None:
    check_token_membership_freshness([_m("i1", RoleScope.STUDENT, version=5)], institution_id="i1", token_authz_version=None)
    with pytest.raises(MembershipNotActiveError):
        check_token_membership_freshness(
            [_m("i1", RoleScope.STUDENT, status=MembershipStatus.REVOKED)], institution_id="i1", token_authz_version=None
        )


# ── creation-time institution selection ──────────────────────────────────────

def test_explicit_institution_must_be_one_the_teacher_belongs_to() -> None:
    teacher = _principal(_m("i1", RoleScope.FACULTY))
    assert resolve_creation_institution(teacher, "i1") == "i1"
    with pytest.raises(ScopeDeniedError):
        resolve_creation_institution(teacher, "i2")


def test_super_admin_may_pick_any_institution_or_none() -> None:
    root = _principal(super_admin=True)
    assert resolve_creation_institution(root, "i9") == "i9"
    assert resolve_creation_institution(root, None) is None


def test_single_teaching_institution_is_the_default_scope() -> None:
    assert resolve_creation_institution(_principal(_m("i1", RoleScope.FACULTY)), None) == "i1"


def test_multiple_teaching_institutions_require_an_explicit_choice() -> None:
    teacher = _principal(_m("i1", RoleScope.FACULTY), _m("i2", RoleScope.ADMIN))
    with pytest.raises(InstitutionRequiredError):
        resolve_creation_institution(teacher, None)


def test_bootstrap_teacher_without_membership_creates_unscoped_resources() -> None:
    assert resolve_creation_institution(_principal(teacher=True), None) is None


def test_non_member_cannot_pick_an_institution() -> None:
    with pytest.raises(ScopeDeniedError):
        resolve_creation_institution(_principal(teacher=True), "i1")
