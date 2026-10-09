"""Resource-scope authorization tests for Prompt 1 (ADR 0017): cross-owner /
cross-institution ("cross-course") access to every exam resource family.

The caller's principal is injected through the same per-request memo the real
dependency chain uses, so these tests isolate the SCOPE rules from token,
allowlist and membership loading (covered in test_exam_authz_dependencies.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_live
from app.domain.authz.policy import ExamPrincipal
from app.domain.institution.models import Membership, MembershipStatus, RoleScope
from app.domain.live.models import LiveRole

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Store:
    """Dict-backed stand-in for the repositories (`get`, `get_blueprint`, …)."""

    def __init__(self, *items, key: str = "id") -> None:
        self.items = {getattr(item, key): item for item in items}

    def get(self, key):
        return self.items.get(key)

    get_blueprint = get_request = get_candidate = get

    def list_all(self):
        return list(self.items.values())

    def list_by_institution(self, institution_id):
        return [m for m in self.items.values() if m.institution_id == institution_id]


class _Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"repository access ({name}) must not happen on this path")


def _patch(monkeypatch, name: str, store) -> None:
    monkeypatch.setattr(scope, name, lambda _db: store)


def _membership(institution_id: str, role: RoleScope, *, uid: str = "caller",
                status: MembershipStatus = MembershipStatus.ACTIVE, identity_key: str = "001") -> Membership:
    return Membership(
        institution_id=institution_id, identity_key=identity_key, contact_email="m@college.edu", role_scope=role,
        status=status, uid=uid, authorization_version=1, created_at=NOW, updated_at=NOW,
    )


def _as(uid: str = "caller", *, teacher: bool = False, super_admin: bool = False, memberships=()) -> dict:
    principal = ExamPrincipal(
        uid=uid, email="c@college.edu", email_verified=True,
        bootstrap_super_admin=super_admin, bootstrap_teacher=teacher, memberships=tuple(memberships),
    )
    return {"uid": uid, deps._PRINCIPAL_KEY: principal}


def _definition(definition_id="d1", owner="owner", institution_id=None, instructor_uids=()) -> SimpleNamespace:
    return SimpleNamespace(
        id=definition_id, owner_uid=owner, institution_id=institution_id, instructor_uids=list(instructor_uids)
    )


def _run(run_id="r1", definition_id="d1", institution_id=None, enrolled=("s1",)) -> SimpleNamespace:
    return SimpleNamespace(id=run_id, definition_id=definition_id, institution_id=institution_id, enrolled_uids=list(enrolled))


def _status(exc: pytest.ExceptionInfo[HTTPException]) -> int:
    return exc.value.status_code


# ── definitions ──────────────────────────────────────────────────────────────

def test_owner_teacher_can_open_their_own_definition(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    assert scope.authorize_definition(_as("owner", teacher=True), "d1").id == "d1"


def test_other_teacher_is_forbidden_from_someone_elses_definition(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(_as("intruder", teacher=True), "d1")
    assert _status(exc) == 403 and exc.value.detail["code"] == "scope_denied"


def test_super_admin_can_open_any_definition(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition(institution_id="i1")))
    assert scope.authorize_definition(_as("root", super_admin=True), "d1").id == "d1"


def test_admin_of_the_definitions_institution_can_open_it_but_other_admins_cannot(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition(institution_id="i1")))
    assert scope.authorize_definition(_as("adm", memberships=[_membership("i1", RoleScope.ADMIN, uid="adm")]), "d1")
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(_as("adm2", memberships=[_membership("i2", RoleScope.ADMIN, uid="adm2")]), "d1")
    assert _status(exc) == 403


def test_faculty_colleague_in_the_same_institution_is_still_forbidden(monkeypatch) -> None:
    """Cross-course access: sharing an institution is not ownership."""
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition(institution_id="i1")))
    colleague = _as("colleague", memberships=[_membership("i1", RoleScope.FACULTY, uid="colleague")])
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(colleague, "d1")
    assert _status(exc) == 403


def test_owner_who_lost_the_teacher_role_is_forbidden(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(_as("owner"), "d1")  # no teacher role any more
    assert _status(exc) == 403


def test_unknown_definition_is_404(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store())
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(_as(teacher=True), "nope")
    assert _status(exc) == 404


# ── runs (resolved through their definition) ─────────────────────────────────

def test_run_access_follows_definition_ownership(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store(_run()))
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    assert scope.authorize_run(_as("owner", teacher=True), "r1").id == "r1"
    with pytest.raises(HTTPException) as exc:
        scope.authorize_run(_as("intruder", teacher=True), "r1")
    assert _status(exc) == 403


def test_run_institution_admin_access_prefers_the_runs_own_institution(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store(_run(institution_id="i1")))
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition(institution_id=None)))
    admin = _as("adm", memberships=[_membership("i1", RoleScope.ADMIN, uid="adm")])
    assert scope.authorize_run(admin, "r1").id == "r1"


def test_super_admin_run_access_does_not_need_the_definition(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store(_run()))
    _patch(monkeypatch, "ExamDefinitionRepository", _Forbidden())
    assert scope.authorize_run(_as("root", super_admin=True), "r1").id == "r1"


def test_run_with_a_missing_definition_is_only_reachable_by_super_admins(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store(_run(definition_id="gone")))
    _patch(monkeypatch, "ExamDefinitionRepository", _Store())
    with pytest.raises(HTTPException) as exc:
        scope.authorize_run(_as("owner", teacher=True), "r1")
    assert _status(exc) == 403


def test_unknown_run_is_404(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store())
    with pytest.raises(HTTPException) as exc:
        scope.authorize_run(_as(teacher=True), "nope")
    assert _status(exc) == 404


# ── blueprints, candidates (answer keys!), generation requests ────────────────

def _generation_world(monkeypatch, *, definition_owner: str = "owner") -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition(owner=definition_owner)))
    generation = _Store(
        SimpleNamespace(id="b1", exam_definition_id="d1", created_by="owner"),
        SimpleNamespace(id="c1", blueprint_id="b1"),
        SimpleNamespace(id="g1", blueprint_id="b1"),
    )
    _patch(monkeypatch, "GenerationRepository", generation)


def test_blueprint_candidate_and_request_follow_the_definition_owner(monkeypatch) -> None:
    _generation_world(monkeypatch)
    owner = _as("owner", teacher=True)
    assert scope.authorize_blueprint(owner, "b1").id == "b1"
    assert scope.authorize_candidate(owner, "c1").id == "c1"
    assert scope.authorize_generation_request(owner, "g1").id == "g1"

    intruder = _as("intruder", teacher=True)
    for call, target in ((scope.authorize_blueprint, "b1"), (scope.authorize_candidate, "c1"),
                         (scope.authorize_generation_request, "g1")):
        with pytest.raises(HTTPException) as exc:
            call(intruder, target)
        assert _status(exc) == 403


def test_blueprint_with_a_dangling_definition_falls_back_to_its_creator(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store())
    _patch(monkeypatch, "GenerationRepository",
           _Store(SimpleNamespace(id="b1", exam_definition_id="gone", created_by="creator")))
    assert scope.authorize_blueprint(_as("creator", teacher=True), "b1").id == "b1"
    with pytest.raises(HTTPException) as exc:
        scope.authorize_blueprint(_as("other", teacher=True), "b1")
    assert _status(exc) == 403


def test_unknown_candidate_and_request_are_404(monkeypatch) -> None:
    _generation_world(monkeypatch)
    for call in (scope.authorize_candidate, scope.authorize_generation_request, scope.authorize_blueprint):
        with pytest.raises(HTTPException) as exc:
            call(_as(teacher=True), "missing")
        assert _status(exc) == 404


# ── private source library ───────────────────────────────────────────────────

def _source_world(monkeypatch) -> None:
    _patch(monkeypatch, "ApprovedSourceRepository", _Store(
        SimpleNamespace(id="s1", created_by="owner", upload_id="up1"),
        SimpleNamespace(id="s-legacy", created_by=None, upload_id="up-legacy"),
        SimpleNamespace(id="s-orphan", created_by=None, upload_id=None),
    ))
    _patch(monkeypatch, "UploadedSourceRepository", _Store(
        SimpleNamespace(id="up1", uploaded_by="owner"),
        SimpleNamespace(id="up-legacy", uploaded_by="legacy-owner"),
    ))


def test_source_and_upload_access_is_owner_only(monkeypatch) -> None:
    _source_world(monkeypatch)
    assert scope.authorize_source(_as("owner", teacher=True), "s1").id == "s1"
    assert scope.authorize_upload(_as("owner", teacher=True), "up1").id == "up1"
    for call, target in ((scope.authorize_source, "s1"), (scope.authorize_upload, "up1")):
        with pytest.raises(HTTPException) as exc:
            call(_as("intruder", teacher=True), target)
        assert _status(exc) == 403


def test_legacy_source_without_created_by_resolves_its_owner_through_the_upload(monkeypatch) -> None:
    _source_world(monkeypatch)
    assert scope.authorize_source(_as("legacy-owner", teacher=True), "s-legacy").id == "s-legacy"
    with pytest.raises(HTTPException) as exc:
        scope.authorize_source(_as("owner", teacher=True), "s-legacy")
    assert _status(exc) == 403


def test_source_with_no_resolvable_owner_is_super_admin_only(monkeypatch) -> None:
    _source_world(monkeypatch)
    with pytest.raises(HTTPException):
        scope.authorize_source(_as("owner", teacher=True), "s-orphan")
    assert scope.authorize_source(_as("root", super_admin=True), "s-orphan").id == "s-orphan"


def test_source_listing_only_shows_the_callers_sources(monkeypatch) -> None:
    _source_world(monkeypatch)
    all_sources = scope.ApprovedSourceRepository(None).list_all()

    owner_view = scope.filter_accessible_sources(_as("owner", teacher=True), all_sources)
    assert [s.id for s in owner_view] == ["s1"]
    assert [s.id for s in scope.filter_accessible_sources(_as("legacy-owner", teacher=True), all_sources)] == ["s-legacy"]
    assert len(scope.filter_accessible_sources(_as("root", super_admin=True), all_sources)) == 3


# ── run-owned records ────────────────────────────────────────────────────────

@pytest.mark.parametrize("authorize_name,store_name,record", [
    ("authorize_submission", "SubmissionRepository", SimpleNamespace(id="x1", run_id="r1")),
    ("authorize_short_answer_review", "ShortAnswerReviewRepository", SimpleNamespace(id="x1", run_id="r1")),
    ("authorize_grade_appeal", "GradeAppealRepository", SimpleNamespace(id="x1", run_id="r1")),
    ("authorize_proctoring_appeal", "AppealRepository", SimpleNamespace(id="x1", run_id="r1")),
    ("authorize_export_job", "ExportJobRepository", SimpleNamespace(id="x1", run_id="r1")),
    ("authorize_snapshot", "EvidenceSnapshotRepository", SimpleNamespace(id="x1", run_id="r1")),
])
def test_run_owned_records_follow_their_runs_owner(monkeypatch, authorize_name, store_name, record) -> None:
    _patch(monkeypatch, store_name, _Store(record))
    _patch(monkeypatch, "ExamRunRepository", _Store(_run()))
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    authorize = getattr(scope, authorize_name)

    assert authorize(_as("owner", teacher=True), "x1").id == "x1"
    with pytest.raises(HTTPException) as forbidden:
        authorize(_as("intruder", teacher=True), "x1")  # e.g. another teacher's camera evidence / answer export
    assert _status(forbidden) == 403
    with pytest.raises(HTTPException) as missing:
        authorize(_as("owner", teacher=True), "nope")
    assert _status(missing) == 404


def test_pending_short_answer_reviews_are_filtered_to_runs_the_caller_manages(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store(_run("r1", "d1"), _run("r2", "d2")))
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition("d1", "owner"), _definition("d2", "someone-else")))
    reviews = [SimpleNamespace(id="a", run_id="r1"), SimpleNamespace(id="b", run_id="r2"),
               SimpleNamespace(id="c", run_id="r1"), SimpleNamespace(id="d", run_id="ghost")]

    visible = scope.filter_reviews_for_user(_as("owner", teacher=True), reviews)
    assert [r.id for r in visible] == ["a", "c"]


# ── institutions ─────────────────────────────────────────────────────────────

def test_roster_scope_matrix(monkeypatch) -> None:
    _patch(monkeypatch, "InstitutionRepository", _Store(SimpleNamespace(id="i1")))
    assert scope.authorize_institution_roster(_as("root", super_admin=True), "i1") == RoleScope.ADMIN
    # Institution ADMINs and FACULTY manage STUDENT rows; FACULTY/ADMIN grants are super-admin only.
    assert scope.authorize_institution_roster(
        _as("adm", memberships=[_membership("i1", RoleScope.ADMIN, uid="adm")]), "i1") == RoleScope.STUDENT
    assert scope.authorize_institution_roster(
        _as("fac", memberships=[_membership("i1", RoleScope.FACULTY, uid="fac")]), "i1") == RoleScope.STUDENT


def test_roster_scope_rejects_non_members_without_revealing_whether_the_institution_exists(monkeypatch) -> None:
    _patch(monkeypatch, "InstitutionRepository", _Forbidden())
    for caller in (_as("stranger", teacher=True),
                   _as("other", memberships=[_membership("i2", RoleScope.ADMIN, uid="other")])):
        with pytest.raises(HTTPException) as exc:
            scope.authorize_institution_roster(caller, "i1")
        assert _status(exc) == 403


def test_authorized_caller_gets_404_for_an_unknown_institution(monkeypatch) -> None:
    _patch(monkeypatch, "InstitutionRepository", _Store())
    with pytest.raises(HTTPException) as exc:
        scope.authorize_institution_roster(_as("root", super_admin=True), "ghost")
    assert _status(exc) == 404


def test_only_admins_and_super_admins_may_administer_an_institution(monkeypatch) -> None:
    _patch(monkeypatch, "InstitutionRepository", _Store(SimpleNamespace(id="i1")))
    scope.authorize_institution_admin(_as("root", super_admin=True), "i1")
    scope.authorize_institution_admin(_as("adm", memberships=[_membership("i1", RoleScope.ADMIN, uid="adm")]), "i1")
    with pytest.raises(HTTPException) as exc:
        scope.authorize_institution_admin(_as("fac", memberships=[_membership("i1", RoleScope.FACULTY, uid="fac")]), "i1")
    assert _status(exc) == 403


# ── creation-time scope ──────────────────────────────────────────────────────

def test_new_definition_defaults_to_the_teachers_only_institution(monkeypatch) -> None:
    caller = _as("fac", memberships=[_membership("i1", RoleScope.FACULTY, uid="fac")])
    assert scope.resolve_new_definition_institution(caller, None) == "i1"


def test_new_definition_in_a_foreign_institution_is_forbidden(monkeypatch) -> None:
    _patch(monkeypatch, "InstitutionRepository", _Store(SimpleNamespace(id="i2")))
    caller = _as("fac", memberships=[_membership("i1", RoleScope.FACULTY, uid="fac")])
    with pytest.raises(HTTPException) as exc:
        scope.resolve_new_definition_institution(caller, "i2")
    assert _status(exc) == 403


def test_ambiguous_institution_is_a_422(monkeypatch) -> None:
    caller = _as("fac", memberships=[_membership("i1", RoleScope.FACULTY, uid="fac", identity_key="1"),
                                     _membership("i2", RoleScope.ADMIN, uid="fac", identity_key="2")])
    with pytest.raises(HTTPException) as exc:
        scope.resolve_new_definition_institution(caller, None)
    assert _status(exc) == 422 and exc.value.detail["code"] == "institution_required"


def test_super_admin_must_name_an_existing_institution(monkeypatch) -> None:
    _patch(monkeypatch, "InstitutionRepository", _Store(SimpleNamespace(id="i1")))
    assert scope.resolve_new_definition_institution(_as("root", super_admin=True), "i1") == "i1"
    with pytest.raises(HTTPException) as exc:
        scope.resolve_new_definition_institution(_as("root", super_admin=True), "ghost")
    assert _status(exc) == 404


def test_bootstrap_teacher_without_membership_creates_unscoped_definitions(monkeypatch) -> None:
    assert scope.resolve_new_definition_institution(_as("boot", teacher=True), None) is None


def test_unscoped_run_enrollment_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(scope, "MembershipRepository", lambda _db: _Forbidden())
    scope.validate_enrollment_scope(None, ["any-uid", "another"])


def test_scoped_run_only_enrolls_active_student_members(monkeypatch) -> None:
    members = [
        _membership("i1", RoleScope.STUDENT, uid="ok", identity_key="1"),
        _membership("i1", RoleScope.STUDENT, uid="revoked", identity_key="2", status=MembershipStatus.REVOKED),
        _membership("i1", RoleScope.FACULTY, uid="teacher", identity_key="3"),
        _membership("i1", RoleScope.STUDENT, uid=None, identity_key="4"),  # never logged in
        _membership("i2", RoleScope.STUDENT, uid="elsewhere", identity_key="5"),
    ]
    _patch(monkeypatch, "MembershipRepository", _Store(*members, key="identity_key"))

    scope.validate_enrollment_scope("i1", ["ok"])
    with pytest.raises(HTTPException) as exc:
        scope.validate_enrollment_scope("i1", ["ok", "revoked", "teacher", "elsewhere", "stranger"])
    assert _status(exc) == 422
    assert exc.value.detail["code"] == "enrollment_outside_institution"
    assert exc.value.detail["uids"] == ["revoked", "teacher", "elsewhere", "stranger"]


def test_variants_can_only_be_reserved_for_enrolled_students() -> None:
    run = _run(enrolled=("s1", "s2"))
    scope.validate_variant_targets(run, ["s1", "s2", "s1"])
    with pytest.raises(HTTPException) as exc:
        scope.validate_variant_targets(run, ["s1", "outsider"])
    assert _status(exc) == 422 and exc.value.detail["uids"] == ["outsider"]


# ── dependency wrappers ──────────────────────────────────────────────────────

def test_scope_dependencies_return_the_user_or_raise(monkeypatch) -> None:
    _patch(monkeypatch, "ExamRunRepository", _Store(_run()))
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    owner = _as("owner", teacher=True)
    assert scope.require_run_scope("r1", owner) is owner
    with pytest.raises(HTTPException):
        scope.require_run_scope("r1", _as("intruder", teacher=True))


# ── live ticket role (resolved through the same scope rules) ─────────────────

def test_live_faculty_role_is_only_granted_for_runs_the_teacher_manages(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    assert exams_live._resolve_role(_run(), _as("owner", teacher=True)) == LiveRole.FACULTY
    with pytest.raises(HTTPException) as exc:
        exams_live._resolve_role(_run(), _as("intruder", teacher=True))
    assert _status(exc) == 403


def test_live_student_role_needs_enrollment_and_current_membership_on_scoped_runs() -> None:
    assert exams_live._resolve_role(_run(enrolled=("s1",)), _as("s1")) == LiveRole.STUDENT

    scoped = _run(institution_id="i1", enrolled=("s1",))
    active = _as("s1", memberships=[_membership("i1", RoleScope.STUDENT, uid="s1")])
    assert exams_live._resolve_role(scoped, active) == LiveRole.STUDENT

    revoked = _as("s1", memberships=[_membership("i1", RoleScope.STUDENT, uid="s1", status=MembershipStatus.REVOKED)])
    with pytest.raises(HTTPException) as exc:
        exams_live._resolve_role(scoped, revoked)
    assert _status(exc) == 403


def test_live_role_is_denied_to_users_who_are_neither_enrolled_nor_managers(monkeypatch) -> None:
    _patch(monkeypatch, "ExamDefinitionRepository", _Store(_definition()))
    with pytest.raises(HTTPException) as exc:
        exams_live._resolve_role(_run(), _as("stranger"))
    assert _status(exc) == 403
