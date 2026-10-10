"""Reconciliation tests: Prompt 2 institution membership plugged into the
Prompt 3/4 faculty-authority check and enrollment-snapshot seam, without
modifying either's core domain code."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.domain.institution.models import Membership, MembershipStatus, RoleScope

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _FakeMembershipRepoCtor:
    """Stands in for `MembershipRepository(db)` — ignores `db`, returns
    canned memberships for `find_by_uid`/`existing_identity_keys`/`get`."""

    def __init__(self, memberships: list[Membership]) -> None:
        self._memberships = memberships

    def __call__(self, _db):
        return self

    def find_by_uid(self, uid: str) -> list[Membership]:
        return [m for m in self._memberships if m.uid == uid]

    def existing_identity_keys(self, institution_id: str) -> set[str]:
        return {m.identity_key for m in self._memberships if m.institution_id == institution_id}

    def get(self, institution_id: str, identity_key: str):
        for m in self._memberships:
            if m.institution_id == institution_id and m.identity_key == identity_key:
                return m
        return None


def _membership(**overrides) -> Membership:
    defaults = dict(
        institution_id="inst-1", identity_key="001", contact_email="s@college.edu",
        role_scope=RoleScope.STUDENT, status=MembershipStatus.ACTIVE, uid="uid-1",
        authorization_version=1, created_at=NOW, updated_at=NOW,
    )
    defaults.update(overrides)
    return Membership(**defaults)


# ── require_exam_faculty: institution membership as an additional grant path ──

def test_require_exam_faculty_still_accepts_bootstrap_email_allowlist(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", "prof@college.edu")
    user = {"uid": "uid-x", "email": "prof@college.edu", "email_verified": True}
    assert deps.require_exam_faculty(user) == user


def test_require_exam_faculty_rejects_allowlisted_but_unverified_email(monkeypatch) -> None:
    """Email/Password sign-up in the shared Firebase project can mint a token
    for any address without proving ownership — an allow-listed address is not
    authority unless the token says it is verified."""
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", "prof@college.edu")
    monkeypatch.setattr("app.domain.institution.repository.MembershipRepository", _FakeMembershipRepoCtor([]))
    user = {"uid": "uid-x", "email": "prof@college.edu", "email_verified": False}
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(user)
    assert exc.value.status_code == 403


def test_require_exam_faculty_grants_via_institution_faculty_membership(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", "")
    fake_repo = _FakeMembershipRepoCtor([_membership(uid="uid-fac", role_scope=RoleScope.FACULTY)])
    monkeypatch.setattr("app.domain.institution.repository.MembershipRepository", fake_repo)
    user = {"uid": "uid-fac", "email": "notinallowlist@college.edu"}
    assert deps.require_exam_faculty(user) == user


def test_require_exam_faculty_rejects_plain_student_membership(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", "")
    fake_repo = _FakeMembershipRepoCtor([_membership(uid="uid-stu", role_scope=RoleScope.STUDENT)])
    monkeypatch.setattr("app.domain.institution.repository.MembershipRepository", fake_repo)
    user = {"uid": "uid-stu", "email": "student@college.edu"}
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(user)
    assert exc.value.status_code == 403


# ── require_active_institution_membership ─────────────────────────────────────

def test_require_active_institution_membership_succeeds(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(
        "app.domain.institution.auth_service.MembershipRepository",
        _FakeMembershipRepoCtor([_membership(uid="uid-1", authorization_version=1)]),
    )
    user = {"uid": "uid-1", "exam_authz_version": 1}
    result = deps.require_active_institution_membership("inst-1", user)
    assert result["membership"].identity_key == "001"


def test_require_active_institution_membership_rejects_wrong_institution(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(
        "app.domain.institution.auth_service.MembershipRepository",
        _FakeMembershipRepoCtor([_membership(institution_id="inst-1", uid="uid-1")]),
    )
    user = {"uid": "uid-1", "exam_authz_version": 1}
    with pytest.raises(HTTPException) as exc:
        deps.require_active_institution_membership("some-other-institution", user)
    assert exc.value.status_code == 403


def test_require_active_institution_membership_rejects_stale_authz_version(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(
        "app.domain.institution.auth_service.MembershipRepository",
        _FakeMembershipRepoCtor([_membership(uid="uid-1", authorization_version=2)]),
    )
    user = {"uid": "uid-1", "exam_authz_version": 1}  # token predates the revocation bump
    with pytest.raises(HTTPException) as exc:
        deps.require_active_institution_membership("inst-1", user)
    assert exc.value.status_code == 403


# ── require_recent_staff_reauth ────────────────────────────────────────────────

def test_require_recent_staff_reauth_accepts_fresh_auth_time(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    now_ts = datetime.now(timezone.utc).timestamp()
    user = {"uid": "u1", "auth_time": now_ts - 5}
    assert deps.require_recent_staff_reauth(user) == user


def test_require_recent_staff_reauth_rejects_stale_auth_time(monkeypatch) -> None:
    from app.api import exams_dependencies as deps

    monkeypatch.setattr(deps.settings, "EXAM_STAFF_REAUTH_MAX_AGE_SECONDS", 60)
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).timestamp()
    user = {"uid": "u1", "auth_time": stale_ts}
    with pytest.raises(HTTPException) as exc:
        deps.require_recent_staff_reauth(user)
    assert exc.value.status_code == 401


def test_require_recent_staff_reauth_rejects_missing_auth_time() -> None:
    from app.api import exams_dependencies as deps

    with pytest.raises(HTTPException) as exc:
        deps.require_recent_staff_reauth({"uid": "u1"})
    assert exc.value.status_code == 401


# ── Enrollment bridge (exams/institution_enrollment.py) ───────────────────────

def test_resolve_enrolled_uids_only_includes_active_provisioned_students(monkeypatch) -> None:
    from app.domain.exams.institution_enrollment import resolve_enrolled_uids_from_institution

    memberships = [
        _membership(identity_key="001", uid="uid-1", role_scope=RoleScope.STUDENT, status=MembershipStatus.ACTIVE),
        _membership(identity_key="002", uid=None, role_scope=RoleScope.STUDENT, status=MembershipStatus.ACTIVE),  # never logged in
        _membership(identity_key="003", uid="uid-3", role_scope=RoleScope.STUDENT, status=MembershipStatus.REVOKED),
        _membership(identity_key="004", uid="uid-4", role_scope=RoleScope.FACULTY, status=MembershipStatus.ACTIVE),
    ]
    monkeypatch.setattr(
        "app.domain.exams.institution_enrollment.MembershipRepository", _FakeMembershipRepoCtor(memberships)
    )
    uids = resolve_enrolled_uids_from_institution(object(), "inst-1", role_scope=RoleScope.STUDENT)
    assert uids == ["uid-1"]
