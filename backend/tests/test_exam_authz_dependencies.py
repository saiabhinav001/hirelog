"""Dependency-level authorization tests for Prompt 1 (ADR 0017).

Covers the regression categories named in the prompt: forged role, forged
e-mail / college ID, direct profile mutation, revoked role with a stale token,
a valid staff session, and legacy-user compatibility. These call the
dependency functions directly with fakes, so they exercise the API
authorization logic on its own — NOT firestore.rules (privileged server SDKs
bypass client rules, so API checks must hold independently).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api import dependencies as api_deps
from app.api import exams_dependencies as deps
from app.api.routes import exams_me
from app.domain.institution.models import Membership, MembershipStatus, RoleScope

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Binding:
    def __init__(self, outcome: str = "bound") -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def ensure_bound(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


class _FailingBinding:
    def ensure_bound(self, **_kwargs):
        raise RuntimeError("firestore unavailable")


class _MembershipRepo:
    """Stands in for `MembershipRepository(db)`: callable ctor + `find_by_uid`."""

    def __init__(self, memberships: list[Membership] | None = None, *, forbid: bool = False) -> None:
        self._memberships = memberships or []
        self._forbid = forbid
        self.lookups = 0

    def __call__(self, _db):
        return self

    def find_by_uid(self, uid: str) -> list[Membership]:
        self.lookups += 1
        assert not self._forbid, "membership lookup must not happen on this path"
        return [m for m in self._memberships if m.uid == uid]


def _membership(institution_id: str = "inst-1", role: RoleScope = RoleScope.STUDENT, *, uid: str = "u1",
                status: MembershipStatus = MembershipStatus.ACTIVE, version: int = 1,
                contact_email: str = "student@college.edu") -> Membership:
    return Membership(
        institution_id=institution_id, identity_key="001", contact_email=contact_email, role_scope=role,
        status=status, uid=uid, authorization_version=version, created_at=NOW, updated_at=NOW,
    )


def _user(uid: str = "u1", *, token_email: str = "", email_verified: bool = False, **extra) -> dict:
    return {"uid": uid, "token_email": token_email, "email_verified": email_verified, **extra}


def _configure(monkeypatch, *, teachers: str = "", supers: str = "", binding=None,
               memberships: list[Membership] | None = None, forbid_memberships: bool = False):
    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", teachers)
    monkeypatch.setattr(deps.settings, "EXAM_SUPER_ADMIN_EMAILS", supers)
    monkeypatch.setattr(deps.settings, "EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL", False)
    monkeypatch.setattr(deps.settings, "ENV", "development")
    binding = binding if binding is not None else _Binding()
    monkeypatch.setattr(deps, "_staff_binding_service", lambda: binding)
    repo = _MembershipRepo(memberships, forbid=forbid_memberships)
    monkeypatch.setattr("app.domain.institution.repository.MembershipRepository", repo)
    return binding, repo


# ── verified-email staff authority (forged role / forged e-mail) ──────────────

def test_unverified_allowlisted_email_is_denied_and_never_reaches_the_binding(monkeypatch) -> None:
    binding, _ = _configure(monkeypatch, teachers="prof@college.edu")
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(_user(token_email="prof@college.edu", email_verified=False))
    assert exc.value.status_code == 403
    assert binding.calls == []


def test_forged_stored_profile_email_cannot_grant_authority(monkeypatch) -> None:
    """`users/{uid}.email` is client-writable at creation (firestore.rules allow
    the key). Authority must come from the verified TOKEN e-mail only."""
    binding, _ = _configure(monkeypatch, teachers="prof@college.edu", supers="owner@college.edu")
    attacker = _user(uid="attacker", token_email="attacker@evil.test", email_verified=True,
                     email="owner@college.edu")  # forged stored profile e-mail
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(attacker)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        deps.require_exam_super_admin(_user(uid="attacker", token_email="attacker@evil.test",
                                            email_verified=True, email="owner@college.edu"))
    assert binding.calls == []


def test_token_email_grants_authority_regardless_of_the_stored_profile_email(monkeypatch) -> None:
    binding, _ = _configure(monkeypatch, teachers="prof@college.edu")
    user = _user(token_email="Prof@College.edu", email_verified=True, email="something-else@x.test")
    assert deps.require_exam_faculty(user) is user
    assert binding.calls[0]["email"] == "prof@college.edu" and binding.calls[0]["roles"] == ["teacher"]


def test_super_admin_allowlist_grants_super_admin_and_teacher(monkeypatch) -> None:
    binding, _ = _configure(monkeypatch, supers="owner@college.edu")
    user = _user(token_email="owner@college.edu", email_verified=True)
    assert deps.require_exam_super_admin(user) is user
    assert deps.require_exam_faculty(user) is user
    assert binding.calls[0]["roles"] == ["super_admin"]
    assert deps.resolve_principal(user).roles()[-1].value == "super_admin"


def test_teacher_allowlist_does_not_grant_super_admin(monkeypatch) -> None:
    _configure(monkeypatch, teachers="prof@college.edu")
    user = _user(token_email="prof@college.edu", email_verified=True)
    assert deps.require_exam_faculty(user) is user
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_super_admin(user)
    assert exc.value.status_code == 403


def test_binding_conflict_removes_all_staff_authority(monkeypatch) -> None:
    _configure(monkeypatch, supers="owner@college.edu", binding=_Binding("conflict"))
    for gate in (deps.require_exam_faculty, deps.require_exam_super_admin):
        with pytest.raises(HTTPException) as exc:
            gate(_user(uid="second-account", token_email="owner@college.edu", email_verified=True))
        assert exc.value.status_code == 403


def test_binding_backend_failure_fails_closed(monkeypatch) -> None:
    _configure(monkeypatch, teachers="prof@college.edu", binding=_FailingBinding())
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(_user(token_email="prof@college.edu", email_verified=True))
    assert exc.value.status_code == 503


def test_removing_the_email_from_the_allowlist_ends_authority_on_the_next_request(monkeypatch) -> None:
    _configure(monkeypatch, teachers="prof@college.edu")
    deps.require_exam_faculty(_user(token_email="prof@college.edu", email_verified=True))

    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", "")
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(_user(token_email="prof@college.edu", email_verified=True))
    assert exc.value.status_code == 403


def test_dev_escape_hatch_allows_unverified_staff_only_outside_production(monkeypatch) -> None:
    _configure(monkeypatch, teachers="prof@college.edu")
    monkeypatch.setattr(deps.settings, "EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL", True)

    dev_user = _user(token_email="prof@college.edu", email_verified=False)
    assert deps.require_exam_faculty(dev_user) is dev_user

    monkeypatch.setattr(deps.settings, "ENV", "production")
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(_user(token_email="prof@college.edu", email_verified=False))
    assert exc.value.status_code == 403


def test_exam_authority_ignores_the_legacy_users_role_field(monkeypatch) -> None:
    """Direct profile/database mutation of `users/{uid}.role` can never create
    exam authority, and a legacy `viewer` can still hold a real membership."""
    _configure(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(_user(role="placement_cell", email="x@y.z"))
    assert exc.value.status_code == 403

    _configure(monkeypatch, memberships=[_membership("inst-1", RoleScope.FACULTY, uid="u1")])
    viewer_with_membership = _user(role="viewer")
    assert deps.require_exam_faculty(viewer_with_membership) is viewer_with_membership


def test_membership_staff_authority_does_not_need_the_binding_service(monkeypatch) -> None:
    binding, _ = _configure(monkeypatch, memberships=[_membership("inst-1", RoleScope.ADMIN, uid="u1")])
    user = _user()
    assert deps.require_exam_faculty(user) is user
    assert binding.calls == []


def test_plain_student_and_proctor_memberships_are_not_faculty(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership("inst-1", RoleScope.STUDENT), _membership("inst-2", RoleScope.PROCTOR)])
    with pytest.raises(HTTPException) as exc:
        deps.require_exam_faculty(_user())
    assert exc.value.status_code == 403


def test_valid_staff_profile_refresh_keeps_exam_authority(monkeypatch) -> None:
    """A staff user re-syncing their profile (legacy `users/{uid}` write) is
    unaffected — authority is recomputed from token + memberships every request."""
    _configure(monkeypatch, teachers="prof@college.edu")
    first = _user(token_email="prof@college.edu", email_verified=True, role="viewer", name="Old")
    refreshed = _user(token_email="prof@college.edu", email_verified=True, role="viewer", name="New Name")
    assert deps.require_exam_faculty(first) is first
    assert deps.require_exam_faculty(refreshed) is refreshed


# ── membership freshness (revoked role with a stale token) ────────────────────

def test_active_membership_with_matching_version_passes(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership(version=2)])
    user = _user(exam_institution_id="inst-1", exam_authz_version=2)
    assert deps.get_current_exam_user(user, None) is user


def test_revoked_membership_is_rejected_even_with_a_valid_token(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership(status=MembershipStatus.REVOKED, version=2)])
    with pytest.raises(HTTPException) as exc:
        deps.get_current_exam_user(_user(exam_institution_id="inst-1", exam_authz_version=2), None)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "membership_not_active"


def test_token_older_than_a_revocation_bump_is_rejected(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership(version=3)])
    with pytest.raises(HTTPException) as exc:
        deps.get_current_exam_user(_user(exam_institution_id="inst-1", exam_authz_version=2), None)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "stale_authorization_version"


def test_membership_removed_entirely_is_rejected(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[])
    with pytest.raises(HTTPException) as exc:
        deps.get_current_exam_user(_user(exam_institution_id="inst-1", exam_authz_version=1), None)
    assert exc.value.status_code == 403


def test_freshness_applies_to_staff_sessions_too(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership(role=RoleScope.FACULTY, status=MembershipStatus.REVOKED, version=2)])
    with pytest.raises(HTTPException) as exc:
        deps.get_current_exam_user(_user(exam_institution_id="inst-1", exam_authz_version=1), None)
    assert exc.value.status_code == 403


def test_legacy_session_without_exam_claims_triggers_no_membership_lookup(monkeypatch) -> None:
    _, repo = _configure(monkeypatch, forbid_memberships=True)
    user = _user(role="viewer")
    assert deps.get_current_exam_user(user, None) is user
    assert repo.lookups == 0


def test_disabled_module_is_503(monkeypatch) -> None:
    monkeypatch.setattr(deps.settings, "EXAMS_ENABLED", False)
    with pytest.raises(HTTPException) as exc:
        deps.require_exams_enabled()
    assert exc.value.status_code == 503


# ── student run scope (enrollment + institution membership) ───────────────────

def _runs(monkeypatch, *runs) -> None:
    by_id = {run.id: run for run in runs}
    monkeypatch.setattr(deps, "ExamRunRepository", lambda _db: SimpleNamespace(get=lambda run_id: by_id.get(run_id)))


def _run(run_id: str = "r1", *, enrolled=("u1",), institution_id=None) -> SimpleNamespace:
    return SimpleNamespace(id=run_id, enrolled_uids=list(enrolled), institution_id=institution_id)


def test_unknown_run_is_404(monkeypatch) -> None:
    _configure(monkeypatch)
    _runs(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        deps.authorize_student_for_run(_user(), "missing")
    assert exc.value.status_code == 404


def test_student_not_enrolled_in_the_run_is_403(monkeypatch) -> None:
    _configure(monkeypatch)
    _runs(monkeypatch, _run(enrolled=("someone-else",)))
    with pytest.raises(HTTPException) as exc:
        deps.authorize_student_for_run(_user(), "r1")
    assert exc.value.status_code == 403


def test_enrolled_student_on_an_unscoped_run_needs_no_membership_lookup(monkeypatch) -> None:
    _, repo = _configure(monkeypatch, forbid_memberships=True)
    _runs(monkeypatch, _run())
    assert deps.authorize_student_for_run(_user(), "r1").id == "r1"
    assert repo.lookups == 0


def test_enrolled_student_with_active_membership_passes_on_a_scoped_run(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership("inst-1", RoleScope.STUDENT)])
    _runs(monkeypatch, _run(institution_id="inst-1"))
    assert deps.authorize_student_for_run(_user(), "r1").institution_id == "inst-1"


@pytest.mark.parametrize("memberships", [
    [_membership("inst-1", RoleScope.STUDENT, status=MembershipStatus.REVOKED)],  # revoked mid-session
    [_membership("inst-2", RoleScope.STUDENT)],                                     # other institution
    [_membership("inst-1", RoleScope.FACULTY)],                                     # wrong role scope
    [],                                                                              # no membership at all
])
def test_scoped_run_rejects_students_without_a_current_active_student_membership(monkeypatch, memberships) -> None:
    _configure(monkeypatch, memberships=memberships)
    _runs(monkeypatch, _run(institution_id="inst-1"))
    with pytest.raises(HTTPException) as exc:
        deps.authorize_student_for_run(_user(), "r1")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "membership_not_active"


def test_matching_roster_email_in_the_token_grants_no_membership_or_scope(monkeypatch) -> None:
    """Forged college ID / e-mail: knowing a roster contact e-mail (even holding
    a verified token for it) does not create membership — only OTP/link
    provisioning sets `membership.uid`."""
    roster_row = _membership("inst-1", RoleScope.STUDENT, uid="someone-who-did-otp", contact_email="victim@college.edu")
    _configure(monkeypatch, memberships=[roster_row])
    _runs(monkeypatch, _run(enrolled=("impostor",), institution_id="inst-1"))
    impostor = _user(uid="impostor", token_email="victim@college.edu", email_verified=True)

    assert deps.resolve_principal(impostor).memberships == ()
    with pytest.raises(HTTPException) as exc:
        deps.authorize_student_for_run(impostor, "r1")
    assert exc.value.status_code == 403


def test_student_scope_dependency_returns_the_user(monkeypatch) -> None:
    _configure(monkeypatch)
    _runs(monkeypatch, _run())
    user = _user()
    assert deps.require_student_run_scope("r1", user) is user


# ── /me and identity surface ──────────────────────────────────────────────────

def test_me_reports_server_computed_roles(monkeypatch) -> None:
    _configure(monkeypatch, supers="owner@college.edu",
               memberships=[_membership("inst-1", RoleScope.STUDENT, uid="u-root")])
    user = _user(uid="u-root", token_email="owner@college.edu", email_verified=True, email="stored@x.test")
    body = exams_me.get_me(user)

    assert body["exam_roles"] == ["student", "teacher", "super_admin"]
    assert body["is_super_admin"] is True and body["is_bootstrap_faculty"] is True
    assert body["email_verified"] is True
    assert body["has_any_exam_membership"] is True
    assert body["memberships"][0]["institution_id"] == "inst-1"


def test_me_for_a_legacy_user_is_empty_and_error_free(monkeypatch) -> None:
    _configure(monkeypatch)
    body = exams_me.get_me(_user(uid="legacy", token_email="legacy@gmail.com", email_verified=True, role="contributor"))
    assert body["exam_roles"] == []
    assert body["memberships"] == [] and body["has_any_exam_membership"] is False
    assert body["is_bootstrap_faculty"] is False and body["is_super_admin"] is False


def test_me_does_not_report_unverified_allowlisted_email_as_staff(monkeypatch) -> None:
    _configure(monkeypatch, teachers="prof@college.edu")
    body = exams_me.get_me(_user(token_email="prof@college.edu", email_verified=False))
    assert body["is_bootstrap_faculty"] is False and body["exam_roles"] == []


def test_me_does_not_credit_a_revoked_membership_with_a_role(monkeypatch) -> None:
    _configure(monkeypatch, memberships=[_membership("inst-1", RoleScope.FACULTY, status=MembershipStatus.REVOKED)])
    body = exams_me.get_me(_user())
    assert body["exam_roles"] == []
    assert body["memberships"][0]["status"] == "revoked"


def test_uncached_user_exposes_the_verified_token_identity_not_the_stored_email(monkeypatch) -> None:
    monkeypatch.setattr(api_deps.firebase_auth, "verify_id_token", lambda _token: {
        "uid": "u1", "email": "Real@College.edu", "email_verified": True, "auth_time": 1,
        "firebase": {"sign_in_provider": "google.com"},
    })

    def forbidden(uid, email, name=None):
        raise AssertionError("an exam request must not read the stored (client-writable) profile")

    monkeypatch.setattr(api_deps, "_get_or_create_user", forbidden)
    user = api_deps.get_current_user_uncached(HTTPAuthorizationCredentials(scheme="Bearer", credentials="t"))

    assert user["token_email"] == "real@college.edu"
    assert user["email_verified"] is True and user["sign_in_provider"] == "google.com"
    assert "role" not in user  # authority never comes from a stored profile field


def test_uncached_user_without_verification_claim_defaults_to_unverified(monkeypatch) -> None:
    monkeypatch.setattr(api_deps.firebase_auth, "verify_id_token", lambda _token: {"uid": "u1", "email": "a@b.co"})
    monkeypatch.setattr(api_deps, "_get_or_create_user", lambda uid, email, name=None: {"uid": uid, "role": "viewer"})
    user = api_deps.get_current_user_uncached(HTTPAuthorizationCredentials(scheme="Bearer", credentials="t"))
    assert user["email_verified"] is False and user["sign_in_provider"] is None
