"""HTTP-level tests for the institution management routes (Prompt 2, docs/adr/0018):
the same dry-run/apply routes accept JSON or a multipart CSV, the scope check runs
before any body is read, and the new read / recovery / teacher-assignment routes map
domain outcomes to the right status codes.

Authorization dependencies are overridden with a pre-seeded principal; who is allowed
is covered by the policy, scope and route-guard tests, so this file is about the HTTP
behavior of the handlers themselves (the services are stand-ins).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_institution_admin as routes
from app.domain.authz.policy import ExamPrincipal
from app.domain.institution.errors import RosterRowRejectedError, RosterRowsChangedError, UnauthorizedRoleGrantError
from app.domain.institution.models import (
    Institution,
    Membership,
    MembershipStatus,
    RoleScope,
    RosterImportJob,
    RosterImportStatus,
    RosterRowInput,
    RosterRowResult,
)
from app.domain.institution.roster_csv import MAX_CSV_BYTES
from app.exam_main import app

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
BASE = "/api/exams/v1/faculty/institutions"
INST = "i1"


def _member(key: str, *, role=RoleScope.STUDENT, status=MembershipStatus.ACTIVE, uid=None, email="student@college.edu") -> Membership:
    return Membership(institution_id=INST, identity_key=key, contact_email=email, role_scope=role, status=status, uid=uid,
                      authorization_version=2, created_at=NOW, updated_at=NOW)


def _job(**overrides) -> RosterImportJob:
    data = dict(id="job1", institution_id=INST, uploaded_by="uploader", requester_max_role_scope="student",
                status=RosterImportStatus.DRY_RUN, total_rows=2, applied_cursor=0, row_results=[],
                created_at=NOW, updated_at=NOW)
    data.update(overrides)
    return RosterImportJob(**data)


class _Roster:
    """Stands in for `RosterService`; records what the route handed it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.apply_error: Exception | None = None
        self.assign_error: Exception | None = None

    def dry_run(self, **kwargs):
        self.calls.append(("dry_run", kwargs))
        return _job(total_rows=len(kwargs["rows"]))

    def apply_batch(self, job_id, **kwargs):
        self.calls.append(("apply_batch", {"job_id": job_id, **kwargs}))
        if self.apply_error:
            raise self.apply_error
        return _job(status=RosterImportStatus.APPLYING, applied_cursor=1)

    def assign_teacher(self, **kwargs):
        self.calls.append(("assign_teacher", kwargs))
        if self.assign_error:
            raise self.assign_error
        return RosterRowResult(row_index=0, identity_key=kwargs["identity_key"].upper(), ok=True, action="create")


class _Jobs:
    def __init__(self, job: RosterImportJob | None) -> None:
        self.job = job

    def get(self, job_id):
        return self.job if self.job and self.job.id == job_id else None


class _Service:
    """Stands in for `InstitutionAuthService`'s recovery actions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error: Exception | None = None

    def _act(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.error:
            raise self.error
        return _member(kwargs["identity_key"].upper())

    def revoke(self, **kwargs):
        return self._act("revoke", **kwargs)

    def reinstate(self, **kwargs):
        return self._act("reinstate", **kwargs)

    def unlink(self, **kwargs):
        return self._act("unlink", **kwargs)


class _Audit:
    def __init__(self) -> None:
        self.events: list = []

    def append(self, event) -> None:
        self.events.append(event)


def _principal(*, super_admin=False, memberships=()) -> ExamPrincipal:
    return ExamPrincipal(uid="uploader", email="u@college.edu", email_verified=True, bootstrap_super_admin=super_admin,
                         bootstrap_teacher=False, memberships=tuple(memberships))


@pytest.fixture
def api(monkeypatch):
    """TestClient with every authorization dependency replaced by one pre-seeded principal."""
    state = {"principal": _principal(super_admin=True), "deny_scope": False}

    def _user() -> dict:
        return {"uid": "uploader", deps._PRINCIPAL_KEY: state["principal"]}

    def _scope(institution_id: str) -> dict:
        if state["deny_scope"]:
            raise HTTPException(status_code=403, detail="scope_denied")
        return _user()

    app.dependency_overrides[deps.require_exams_enabled] = lambda: None
    app.dependency_overrides[scope.require_institution_roster_scope] = _scope
    app.dependency_overrides[scope.require_institution_admin_scope] = _scope
    app.dependency_overrides[deps.require_exam_faculty] = _user
    app.dependency_overrides[deps.require_exam_super_admin] = _user
    app.dependency_overrides[deps.require_recent_staff_reauth] = _user

    roster, service, audit = _Roster(), _Service(), _Audit()
    monkeypatch.setattr(routes, "RosterService", lambda _db: roster)
    monkeypatch.setattr(routes, "InstitutionAuthService", lambda _db: service)
    monkeypatch.setattr(routes, "AuthzAuditRepository", lambda _db: audit)

    class _Handles:
        pass

    handles = _Handles()
    handles.client, handles.state, handles.roster, handles.service, handles.audit = TestClient(app), state, roster, service, audit
    yield handles
    app.dependency_overrides.clear()


def _csv(*lines: str) -> bytes:
    return "\n".join(lines).encode("utf-8")


# ── dry-run and apply: JSON or CSV on the same routes ───────────────────────

def test_the_json_dry_run_is_unchanged(api) -> None:
    response = api.client.post(f"{BASE}/{INST}/roster/dry-run", json={"rows": [{"identity_key": "007", "contact_email": "a@college.edu"}]})
    assert response.status_code == 200
    name, kwargs = api.roster.calls[0]
    assert name == "dry_run" and kwargs["rows"] == [RosterRowInput(identity_key="007", contact_email="a@college.edu")]
    assert kwargs["requester_max_role_scope"] == RoleScope.ADMIN and kwargs["can_change_contact_email"] is True


def test_a_csv_upload_reaches_the_same_service_with_leading_zeros_intact(api) -> None:
    csv_bytes = _csv("identity_key,contact_email,role_scope", "007A,a@college.edu,", "00012,b@college.edu,student")
    response = api.client.post(f"{BASE}/{INST}/roster/dry-run", files={"file": ("roster.csv", csv_bytes, "text/csv")})
    assert response.status_code == 200
    rows = api.roster.calls[0][1]["rows"]
    assert [r.identity_key for r in rows] == ["007A", "00012"] and [r.role_scope for r in rows] == ["student", "student"]


def test_a_csv_apply_passes_batch_size_and_the_actor(api, monkeypatch) -> None:
    monkeypatch.setattr(routes, "RosterImportRepository", lambda _db: _Jobs(_job()))
    response = api.client.post(
        f"{BASE}/{INST}/roster/job1/apply",
        files={"file": ("roster.csv", _csv("identity_key,contact_email", "001,a@college.edu"), "text/csv")},
        data={"batch_size": "50"},
    )
    assert response.status_code == 200
    name, kwargs = api.roster.calls[0]
    assert name == "apply_batch" and kwargs["batch_size"] == 50 and kwargs["actor_uid"] == "uploader"


def test_row_level_csv_problems_still_produce_a_job(api) -> None:
    csv_bytes = _csv("identity_key,contact_email", "001,a@college.edu", "002")
    response = api.client.post(f"{BASE}/{INST}/roster/dry-run", files={"file": ("r.csv", csv_bytes, "text/csv")})
    assert response.status_code == 200
    rows = api.roster.calls[0][1]["rows"]
    assert isinstance(rows[0], RosterRowInput) and rows[1].line_number == 3


@pytest.mark.parametrize("csv_bytes,code", [
    pytest.param(_csv("identity_key,contact_email,password", "001,a@college.edu,hunter2"), "unknown_columns", id="password-column"),
    pytest.param(_csv("identity_key", "001"), "missing_columns", id="missing-column"),
    pytest.param(b"identity_key,contact_email\n\xff,a@college.edu\n", "not_utf8", id="not-utf8"),
])
def test_unusable_csv_files_are_rejected_with_a_code(api, csv_bytes: bytes, code: str) -> None:
    response = api.client.post(f"{BASE}/{INST}/roster/dry-run", files={"file": ("r.csv", csv_bytes, "text/csv")})
    assert response.status_code == 422 and response.json()["detail"]["code"] == code
    assert api.roster.calls == []


def test_a_file_just_over_the_csv_limit_is_rejected_after_reading(api) -> None:
    """Within the multipart allowance, so it is read, then refused by the parser."""
    response = api.client.post(f"{BASE}/{INST}/roster/dry-run", files={"file": ("r.csv", b"x" * (MAX_CSV_BYTES + 1), "text/csv")})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "file_too_large"
    assert api.roster.calls == []


def test_an_enormous_upload_is_refused_before_it_is_parsed(api) -> None:
    response = api.client.post(f"{BASE}/{INST}/roster/dry-run", files={"file": ("r.csv", b"x" * (MAX_CSV_BYTES + 200_000), "text/csv")})
    assert response.status_code == 413 and response.json()["detail"]["code"] == "file_too_large"


def test_a_multipart_request_without_the_file_field_and_a_bad_batch_size_are_422(api) -> None:
    no_file = api.client.post(f"{BASE}/{INST}/roster/dry-run", data={"batch_size": "5"}, files={"other": ("x.txt", b"hi", "text/plain")})
    assert no_file.status_code == 422 and no_file.json()["detail"]["code"] == "csv_file_required"
    bad_batch = api.client.post(
        f"{BASE}/{INST}/roster/dry-run",
        files={"file": ("r.csv", _csv("identity_key,contact_email", "001,a@college.edu"), "text/csv")},
        data={"batch_size": "0"},
    )
    assert bad_batch.status_code == 422 and bad_batch.json()["detail"]["code"] == "invalid_batch_size"


def test_other_content_types_and_broken_json_are_rejected(api) -> None:
    assert api.client.post(f"{BASE}/{INST}/roster/dry-run", content=b"a,b", headers={"content-type": "text/csv"}).status_code == 415
    assert api.client.post(f"{BASE}/{INST}/roster/dry-run", content=b"{nope", headers={"content-type": "application/json"}).status_code == 422
    assert api.client.post(f"{BASE}/{INST}/roster/dry-run", json={"rows": []}).status_code == 422
    assert api.roster.calls == []


def test_the_scope_check_runs_before_the_body_is_read(api) -> None:
    api.state["deny_scope"] = True
    for kwargs in (
        {"content": b"{nope", "headers": {"content-type": "application/json"}},   # would be 422 if parsed first
        {"content": b"a,b", "headers": {"content-type": "text/csv"}},             # would be 415 if parsed first
    ):
        assert api.client.post(f"{BASE}/{INST}/roster/dry-run", **kwargs).status_code == 403
    assert api.roster.calls == []


def test_apply_is_bound_to_the_path_institution_and_reports_changed_rows(api, monkeypatch) -> None:
    body = {"rows": [{"identity_key": "001", "contact_email": "a@college.edu"}]}
    monkeypatch.setattr(routes, "RosterImportRepository", lambda _db: _Jobs(_job(institution_id="someone-elses")))
    assert api.client.post(f"{BASE}/{INST}/roster/job1/apply", json=body).status_code == 404

    monkeypatch.setattr(routes, "RosterImportRepository", lambda _db: _Jobs(_job()))
    api.roster.apply_error = RosterRowsChangedError("the submitted rows differ")
    conflict = api.client.post(f"{BASE}/{INST}/roster/job1/apply", json=body)
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "roster_rows_changed"

    api.roster.apply_error = LookupError("roster import job job1 not found")
    assert api.client.post(f"{BASE}/{INST}/roster/job1/apply", json=body).status_code == 404


def test_roster_job_status_summarizes_and_lists_errors(api, monkeypatch) -> None:
    results = [
        RosterRowResult(row_index=0, identity_key="001", ok=True, action="create"),
        RosterRowResult(row_index=1, identity_key="002", ok=True, action="skip_unchanged"),
        RosterRowResult(row_index=2, identity_key="003", ok=False, error="membership_revoked_reinstate_required"),
    ]
    monkeypatch.setattr(routes, "RosterImportRepository", lambda _db: _Jobs(_job(row_results=results, total_rows=3)))
    body = api.client.get(f"{BASE}/{INST}/roster/job1").json()
    assert body["summary"] == {"ok": 2, "failed": 1, "create": 1, "update": 0, "skip_unchanged": 1}
    assert body["errors"] == [{"row_index": 2, "identity_key": "003", "ok": False, "action": "", "error": "membership_revoked_reinstate_required"}]
    assert "row_results" not in body
    assert len(api.client.get(f"{BASE}/{INST}/roster/job1?include_rows=true").json()["row_results"]) == 3
    assert api.client.get(f"{BASE}/other/roster/job1").status_code == 404


# ── reads ───────────────────────────────────────────────────────────────────

def test_memberships_are_listed_with_masked_emails_no_uids_filters_and_paging(api, monkeypatch) -> None:
    members = [_member(f"S{i:02d}", uid=f"uid-{i}", email=f"student{i}@college.edu") for i in range(5)]
    members.append(_member("T01", role=RoleScope.FACULTY))
    members.append(_member("S99", status=MembershipStatus.REVOKED))
    monkeypatch.setattr(routes, "MembershipRepository", lambda _db: type("R", (), {"list_by_institution": lambda _s, _i: members})())

    page = api.client.get(f"{BASE}/{INST}/memberships?role_scope=student&status=active&limit=2").json()
    assert [m["identity_key"] for m in page["results"]] == ["S00", "S01"] and page["next_after"] == "S01"
    assert page["results"][0]["contact_email_masked"] == "s***@college.edu" and page["results"][0]["linked"] is True
    assert "uid" not in page["results"][0] and "student0@college.edu" not in str(page)

    rest = api.client.get(f"{BASE}/{INST}/memberships?role_scope=student&status=active&limit=10&after=S01").json()
    assert [m["identity_key"] for m in rest["results"]] == ["S02", "S03", "S04"] and rest["next_after"] is None
    revoked = api.client.get(f"{BASE}/{INST}/memberships?status=revoked").json()
    assert [m["identity_key"] for m in revoked["results"]] == ["S99"]
    assert api.client.get(f"{BASE}/{INST}/memberships?limit=0").status_code == 422


def test_institutions_are_listed_by_standing(api, monkeypatch) -> None:
    all_institutions = [Institution(id=i, name=n, created_at=NOW, updated_at=NOW) for i, n in (("i2", "Beta"), ("i1", "alpha"), ("i3", "Gamma"))]
    repo = type("R", (), {"list_all": lambda _s: list(all_institutions),
                          "get": lambda _s, iid: next((i for i in all_institutions if i.id == iid), None)})()
    monkeypatch.setattr(routes, "InstitutionRepository", lambda _db: repo)

    everything = api.client.get(BASE).json()["results"]
    assert [i["id"] for i in everything] == ["i1", "i2", "i3"]  # super-admin: all, by name

    api.state["principal"] = _principal(memberships=[_member("T1", role=RoleScope.FACULTY, uid="uploader").model_copy(update={"institution_id": "i2"})])
    assert [i["id"] for i in api.client.get(BASE).json()["results"]] == ["i2"]


# ── create institution ──────────────────────────────────────────────────────

def test_creating_an_institution_stores_its_identity_policy_and_audits_it(api, monkeypatch) -> None:
    created: list = []
    monkeypatch.setattr(routes, "InstitutionRepository", lambda _db: type("R", (), {"create": lambda _s, inst: created.append(inst)})())

    response = api.client.post(BASE, json={"name": "College", "identity_policy": {"remove_characters": "-/", "min_length": 6}})
    assert response.status_code == 200
    assert created[0].identity_policy.remove_characters == "-/" and created[0].identity_policy.min_length == 6
    assert response.json()["identity_policy"]["max_length"] == 64
    event = api.audit.events[0]
    assert event.kind == "institution_created" and event.actor_uid == "uploader" and event.institution_id == created[0].id
    assert event.detail["identity_policy"]["remove_characters"] == "-/"


def test_an_inconsistent_or_unknown_policy_is_rejected(api) -> None:
    assert api.client.post(BASE, json={"name": "C", "identity_policy": {"min_length": 10, "max_length": 5}}).status_code == 422
    assert api.client.post(BASE, json={"name": "C", "identity_policy": {"regex": ".*"}}).status_code == 422


def test_a_plain_institution_still_works_without_a_policy(api, monkeypatch) -> None:
    created: list = []
    monkeypatch.setattr(routes, "InstitutionRepository", lambda _db: type("R", (), {"create": lambda _s, inst: created.append(inst)})())
    assert api.client.post(BASE, json={"name": "Plain College"}).status_code == 200
    assert created[0].identity_policy is None


# ── recovery actions ────────────────────────────────────────────────────────

def test_revoke_keeps_its_response_shape_and_forwards_the_reason_and_actor(api) -> None:
    response = api.client.post(f"{BASE}/{INST}/memberships/revoke", json={"identity_key": "s01", "reason": "left"})
    assert response.status_code == 200
    assert response.json() == {"status": "revoked", "institution_id": INST, "identity_key": "s01"}
    name, kwargs = api.service.calls[0]
    assert name == "revoke" and kwargs["actor_uid"] == "uploader" and kwargs["reason"] == "left"
    assert kwargs["requester_max_role_scope"] == RoleScope.ADMIN  # a super-admin may change any role


def test_revoke_without_a_reason_still_works_for_existing_clients(api) -> None:
    assert api.client.post(f"{BASE}/{INST}/memberships/revoke", json={"identity_key": "s01"}).status_code == 200


def test_unlink_and_reinstate_require_a_reason(api) -> None:
    for action in ("unlink", "reinstate"):
        assert api.client.post(f"{BASE}/{INST}/memberships/{action}", json={"identity_key": "s01"}).status_code == 422
        assert api.client.post(f"{BASE}/{INST}/memberships/{action}", json={"identity_key": "s01", "reason": "ok"}).status_code == 422  # too short
        ok = api.client.post(f"{BASE}/{INST}/memberships/{action}", json={"identity_key": "s01", "reason": "verified in person"})
        assert ok.status_code == 200 and ok.json()["status"] == {"unlink": "unlinked", "reinstate": "reinstated"}[action]


def test_recovery_actions_cannot_target_a_membership_that_outranks_the_caller(api) -> None:
    api.state["principal"] = _principal(memberships=[_member("A1", role=RoleScope.ADMIN, uid="uploader")])
    api.service.error = UnauthorizedRoleGrantError("not permitted to change a faculty membership")
    response = api.client.post(f"{BASE}/{INST}/memberships/unlink", json={"identity_key": "t1", "reason": "wrong account"})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "unauthorized_role_grant"
    assert api.service.calls[0][1]["requester_max_role_scope"] == RoleScope.STUDENT  # an institution ADMIN manages students only

    api.service.error = LookupError("membership 'X' not found")
    assert api.client.post(f"{BASE}/{INST}/memberships/revoke", json={"identity_key": "x"}).status_code == 404


# ── teacher assignment ──────────────────────────────────────────────────────

def test_teacher_assignment_succeeds_and_passes_the_actor_and_reason(api) -> None:
    response = api.client.post(f"{BASE}/{INST}/teachers", json={"identity_key": "t01", "contact_email": "t@college.edu", "reason": "new hire"})
    assert response.status_code == 200
    assert response.json() == {"status": "assigned", "institution_id": INST, "identity_key": "T01", "role_scope": "faculty", "action": "create"}
    kwargs = api.roster.calls[0][1]
    assert kwargs["role_scope"] == RoleScope.FACULTY and kwargs["actor_uid"] == "uploader" and kwargs["reason"] == "new hire"


def test_teacher_assignment_failures_map_to_conflict_or_unprocessable(api) -> None:
    body = {"identity_key": "t01", "contact_email": "t@college.edu", "role_scope": "admin"}
    api.roster.assign_error = RosterRowRejectedError("membership_revoked_reinstate_required")
    revoked = api.client.post(f"{BASE}/{INST}/teachers", json=body)
    assert revoked.status_code == 409 and revoked.json()["detail"]["reason"] == "membership_revoked_reinstate_required"
    api.roster.assign_error = RosterRowRejectedError("identity_key_invalid:too_short")
    assert api.client.post(f"{BASE}/{INST}/teachers", json=body).status_code == 422


def test_teacher_assignment_only_names_faculty_or_admin_and_nothing_else(api) -> None:
    for bad in ({"role_scope": "student"}, {"role_scope": "proctor"}, {"is_super_admin": True}, {"uid": "x"}):
        response = api.client.post(f"{BASE}/{INST}/teachers", json={"identity_key": "t01", "contact_email": "t@college.edu", **bad})
        assert response.status_code == 422, bad
    assert api.roster.calls == []
