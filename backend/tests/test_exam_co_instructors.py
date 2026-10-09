"""Course-scoped teacher assignment ("definition = course", Prompt 2, docs/adr/0018,
decision D-2): co-instructor policy, scope, assignment rules and routes.

Assignment is by a super-admin or an ADMIN of the definition's institution — never
by the owner or a co-instructor — and only to linked, ACTIVE FACULTY/ADMIN members
of that institution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_faculty
from app.domain.authz.policy import ExamPrincipal, can_administer_institution, can_manage_resource
from app.domain.exams import institution_enrollment as enrollment
from app.domain.exams.institution_enrollment import InstructorAssignmentError, assign_instructors
from app.domain.exams.models import ExamDefinition
from app.domain.institution.models import IdentityKeyPolicy, Institution, Membership, MembershipStatus, RoleScope

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _m(institution_id: str, role: RoleScope, *, uid: str | None = "caller", key: str = "K1",
       status: MembershipStatus = MembershipStatus.ACTIVE) -> Membership:
    return Membership(institution_id=institution_id, identity_key=key, contact_email="m@college.edu", role_scope=role,
                      status=status, uid=uid, authorization_version=1, created_at=NOW, updated_at=NOW)


def _principal(uid: str = "caller", *, teacher: bool = False, super_admin: bool = False, memberships=()) -> ExamPrincipal:
    return ExamPrincipal(uid=uid, email="c@college.edu", email_verified=True, bootstrap_super_admin=super_admin,
                         bootstrap_teacher=teacher, memberships=tuple(memberships))


def _as(uid: str = "caller", **kwargs) -> dict:
    return {"uid": uid, deps._PRINCIPAL_KEY: _principal(uid, **kwargs)}


# ── policy ───────────────────────────────────────────────────────────────────

def test_a_co_instructor_who_is_still_a_teacher_can_manage_the_resource() -> None:
    principal = _principal("co", memberships=[_m("i1", RoleScope.FACULTY, uid="co")])
    assert can_manage_resource(principal, owner_uid="owner", institution_id="i1", co_owner_uids=["co"])
    assert not can_manage_resource(principal, owner_uid="owner", institution_id="i1")  # not assigned


def test_a_co_instructor_who_lost_the_teacher_role_loses_access() -> None:
    revoked = _principal("co", memberships=[_m("i1", RoleScope.FACULTY, uid="co", status=MembershipStatus.REVOKED)])
    assert not can_manage_resource(revoked, owner_uid="owner", institution_id="i1", co_owner_uids=["co"])
    assert not can_manage_resource(_principal("co"), owner_uid="owner", institution_id="i1", co_owner_uids=["co"])


def test_who_may_administer_an_institution() -> None:
    assert can_administer_institution(_principal(super_admin=True), "i1")
    assert can_administer_institution(_principal(memberships=[_m("i1", RoleScope.ADMIN)]), "i1")
    assert not can_administer_institution(_principal(memberships=[_m("i2", RoleScope.ADMIN)]), "i1")
    assert not can_administer_institution(_principal(memberships=[_m("i1", RoleScope.FACULTY)]), "i1")
    assert not can_administer_institution(_principal(teacher=True), "i1")
    assert not can_administer_institution(_principal(memberships=[_m("i1", RoleScope.ADMIN)]), None)


# ── scope through the definition chain ───────────────────────────────────────

class _Store:
    def __init__(self, *items) -> None:
        self.items = {item.id: item for item in items}

    def get(self, key):
        return self.items.get(key)

    get_blueprint = get

    def list_all(self):
        return list(self.items.values())


def _definition(*, instructors=("co",), institution_id: str | None = "i1") -> SimpleNamespace:
    return SimpleNamespace(id="d1", owner_uid="owner", institution_id=institution_id, instructor_uids=list(instructors))


def _patch(monkeypatch, **stores) -> None:
    for name, store in stores.items():
        monkeypatch.setattr(scope, name, lambda _db, store=store: store)


def _co_instructor(uid: str = "co") -> dict:
    return _as(uid, memberships=[_m("i1", RoleScope.FACULTY, uid=uid)])


def test_a_co_instructor_reaches_the_definition_its_runs_and_its_blueprints(monkeypatch) -> None:
    run = SimpleNamespace(id="r1", definition_id="d1", institution_id="i1", enrolled_uids=[])
    blueprint = SimpleNamespace(id="b1", exam_definition_id="d1", created_by="owner")
    _patch(monkeypatch, ExamDefinitionRepository=_Store(_definition()), ExamRunRepository=_Store(run),
           GenerationRepository=_Store(blueprint))
    user = _co_instructor()
    assert scope.authorize_definition(user, "d1").id == "d1"
    assert scope.authorize_run(user, "r1").id == "r1"
    assert scope.authorize_blueprint(user, "b1").id == "b1"


def test_a_teacher_who_is_not_assigned_is_still_denied(monkeypatch) -> None:
    _patch(monkeypatch, ExamDefinitionRepository=_Store(_definition()))
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(_co_instructor("someone-else"), "d1")
    assert exc.value.status_code == 403


def test_a_co_instructor_whose_membership_was_revoked_is_denied(monkeypatch) -> None:
    _patch(monkeypatch, ExamDefinitionRepository=_Store(_definition()))
    user = _as("co", memberships=[_m("i1", RoleScope.FACULTY, uid="co", status=MembershipStatus.REVOKED)])
    with pytest.raises(HTTPException) as exc:
        scope.authorize_definition(user, "d1")
    assert exc.value.status_code == 403


def test_only_institution_administration_may_change_the_instructor_list(monkeypatch) -> None:
    _patch(monkeypatch, ExamDefinitionRepository=_Store(_definition()))
    owner = _as("owner", memberships=[_m("i1", RoleScope.FACULTY, uid="owner")])
    allowed = [
        _as("root", super_admin=True),
        _as("adm", memberships=[_m("i1", RoleScope.ADMIN, uid="adm")]),
    ]
    denied = [
        owner,                                                       # the owner cannot hand on teaching access
        _co_instructor(),                                            # nor can a co-instructor
        _as("adm2", memberships=[_m("i2", RoleScope.ADMIN, uid="adm2")]),  # ADMIN of another institution
        _as("boot", teacher=True),                                   # bootstrap teacher
    ]
    for user in allowed:
        assert scope.authorize_instructor_assignment(user, "d1").id == "d1"
    for user in denied:
        with pytest.raises(HTTPException) as exc:
            scope.authorize_instructor_assignment(user, "d1")
        assert exc.value.status_code == 403


def test_an_unscoped_definition_can_only_have_its_instructors_changed_by_a_super_admin(monkeypatch) -> None:
    _patch(monkeypatch, ExamDefinitionRepository=_Store(_definition(institution_id=None)))
    assert scope.authorize_instructor_assignment(_as("root", super_admin=True), "d1")
    with pytest.raises(HTTPException) as exc:
        scope.authorize_instructor_assignment(_as("adm", memberships=[_m("i1", RoleScope.ADMIN, uid="adm")]), "d1")
    assert exc.value.status_code == 403


def test_an_unknown_definition_is_404_for_instructor_assignment(monkeypatch) -> None:
    _patch(monkeypatch, ExamDefinitionRepository=_Store())
    with pytest.raises(HTTPException) as exc:
        scope.authorize_instructor_assignment(_as("root", super_admin=True), "nope")
    assert exc.value.status_code == 404


# ── assigning instructors (domain service) ───────────────────────────────────

class _Defs:
    def __init__(self, definition: ExamDefinition | None) -> None:
        self.definition = definition
        self.set_calls: list[list[str]] = []

    def get(self, definition_id):
        return self.definition if self.definition and self.definition.id == definition_id else None

    def get_in_transaction(self, _txn, definition_id):
        return self.get(definition_id)

    def set_instructors_in_transaction(self, _txn, _definition_id, uids, _now) -> None:
        self.set_calls.append(list(uids))
        self.definition = self.definition.model_copy(update={"instructor_uids": list(uids)})


class _Members:
    def __init__(self, members: dict[str, Membership]) -> None:
        self.members = members

    def get_many(self, _institution_id, keys):
        return {k: self.members[k] for k in keys if k in self.members}


class _Audit:
    def __init__(self) -> None:
        self.events: list = []

    def append_in_transaction(self, _txn, event) -> None:
        self.events.append(event)


class _Db:
    def transaction(self):
        return object()


def _definition_model(institution_id: str | None = "i1", instructors=()) -> ExamDefinition:
    return ExamDefinition(id="d1", title="Algorithms", owner_uid="owner", created_at=NOW, updated_at=NOW,
                          institution_id=institution_id, instructor_uids=list(instructors))


def _wire(monkeypatch, *, definition, members, policy: IdentityKeyPolicy | None = None):
    defs, audit = _Defs(definition), _Audit()
    institution = Institution(id="i1", name="College", created_at=NOW, updated_at=NOW, identity_policy=policy)
    monkeypatch.setattr(enrollment, "ExamDefinitionRepository", lambda _db: defs)
    monkeypatch.setattr(enrollment, "InstitutionRepository", lambda _db: SimpleNamespace(get=lambda _i: institution))
    monkeypatch.setattr(enrollment, "MembershipRepository", lambda _db: _Members(members))
    monkeypatch.setattr(enrollment, "AuthzAuditRepository", lambda _db: audit)
    monkeypatch.setattr(enrollment.firestore, "transactional", lambda fn: fn)
    return defs, audit


def _faculty_members() -> dict[str, Membership]:
    return {
        "T1": _m("i1", RoleScope.FACULTY, uid="u-t1", key="T1"),
        "T2": _m("i1", RoleScope.ADMIN, uid="u-t2", key="T2"),
        "NEW": _m("i1", RoleScope.FACULTY, uid=None, key="NEW"),
        "OLD": _m("i1", RoleScope.FACULTY, uid="u-old", key="OLD", status=MembershipStatus.REVOKED),
        "S1": _m("i1", RoleScope.STUDENT, uid="u-s1", key="S1"),
    }


def test_assigning_resolves_identity_keys_to_uids_and_audits_with_the_previous_list(monkeypatch) -> None:
    defs, audit = _wire(monkeypatch, definition=_definition_model(instructors=["u-old-1"]), members=_faculty_members())
    uids = assign_instructors(_Db(), definition_id="d1", identity_keys=["t1", " T2 ", "T1"], actor_uid="root")

    assert uids == ["u-t1", "u-t2"]  # normalized, de-duplicated, in request order
    assert defs.definition.instructor_uids == ["u-t1", "u-t2"]
    event = audit.events[0]
    assert event.kind == "instructors_set" and event.actor_uid == "root" and event.institution_id == "i1"
    assert event.detail == {"definition_id": "d1", "previous": ["u-old-1"], "instructor_uids": ["u-t1", "u-t2"]}


def test_assigning_an_empty_list_clears_the_instructors(monkeypatch) -> None:
    defs, _audit = _wire(monkeypatch, definition=_definition_model(instructors=["u-t1"]), members=_faculty_members())
    assert assign_instructors(_Db(), definition_id="d1", identity_keys=[], actor_uid="root") == []
    assert defs.definition.instructor_uids == []


def test_only_linked_active_faculty_or_admin_members_can_be_assigned(monkeypatch) -> None:
    defs, audit = _wire(monkeypatch, definition=_definition_model(), members=_faculty_members())
    with pytest.raises(InstructorAssignmentError) as exc:
        assign_instructors(_Db(), definition_id="d1", identity_keys=["T1", "NEW", "OLD", "S1", "GHOST"], actor_uid="root")
    assert exc.value.code == "instructors_not_assignable"
    assert exc.value.problems == [
        {"identity_key": "NEW", "reason": "not_linked_yet"},
        {"identity_key": "OLD", "reason": "membership_not_active"},
        {"identity_key": "S1", "reason": "not_faculty"},
        {"identity_key": "GHOST", "reason": "not_a_member"},
    ]
    assert defs.set_calls == [] and audit.events == []  # nothing changes unless every key is valid


def test_assignment_needs_an_institution_scoped_existing_definition(monkeypatch) -> None:
    _wire(monkeypatch, definition=_definition_model(institution_id=None), members=_faculty_members())
    with pytest.raises(InstructorAssignmentError) as unscoped:
        assign_instructors(_Db(), definition_id="d1", identity_keys=["T1"], actor_uid="root")
    assert unscoped.value.code == "definition_not_institution_scoped"
    with pytest.raises(InstructorAssignmentError) as missing:
        assign_instructors(_Db(), definition_id="nope", identity_keys=["T1"], actor_uid="root")
    assert missing.value.code == "definition_not_found"


def test_assignment_uses_the_institutions_identity_policy(monkeypatch) -> None:
    members = {"21BCE0001": _m("i1", RoleScope.FACULTY, uid="u-1", key="21BCE0001")}
    _wire(monkeypatch, definition=_definition_model(), members=members, policy=IdentityKeyPolicy(remove_characters="-/ "))
    assert assign_instructors(_Db(), definition_id="d1", identity_keys=["21-bce-0001"], actor_uid="root") == ["u-1"]


# ── routes ───────────────────────────────────────────────────────────────────

def test_assigned_courses_include_definitions_the_teacher_co_instructs(monkeypatch) -> None:
    owned = _definition_model().model_copy(update={"id": "mine"})
    shared = _definition_model().model_copy(update={"id": "shared"})
    both = _definition_model().model_copy(update={"id": "both"})
    repository = SimpleNamespace(list_by_owner=lambda uid: [owned, both], list_by_instructor=lambda uid: [shared, both])
    monkeypatch.setattr(exams_faculty, "_service", lambda: SimpleNamespace(definitions=repository))

    body = exams_faculty.list_my_definitions({"uid": "t1"})
    assert sorted(d["id"] for d in body["results"]) == ["both", "mine", "shared"]


def test_the_set_instructors_route_maps_domain_errors_to_http(monkeypatch) -> None:
    payload = exams_faculty.SetInstructorsRequest(identity_keys=["T1"])

    def _raise(code: str, problems=None):
        def inner(*_args, **_kwargs):
            raise InstructorAssignmentError(code, problems)
        return inner

    monkeypatch.setattr(exams_faculty, "assign_instructors", _raise("definition_not_found"))
    with pytest.raises(HTTPException) as missing:
        exams_faculty.set_instructors("d1", payload, user={"uid": "root"}, _reauth={})
    assert missing.value.status_code == 404

    problems = [{"identity_key": "T1", "reason": "not_linked_yet"}]
    monkeypatch.setattr(exams_faculty, "assign_instructors", _raise("instructors_not_assignable", problems))
    with pytest.raises(HTTPException) as invalid:
        exams_faculty.set_instructors("d1", payload, user={"uid": "root"}, _reauth={})
    assert invalid.value.status_code == 422 and invalid.value.detail == {"code": "instructors_not_assignable", "problems": problems}

    monkeypatch.setattr(exams_faculty, "assign_instructors", lambda *_a, **_k: ["u-t1"])
    assert exams_faculty.set_instructors("d1", payload, user={"uid": "root"}, _reauth={}) == {
        "definition_id": "d1", "instructor_uids": ["u-t1"],
    }


def test_the_instructor_request_is_bounded_and_strict() -> None:
    with pytest.raises(ValueError):
        exams_faculty.SetInstructorsRequest(identity_keys=[f"T{i}" for i in range(21)])
    with pytest.raises(ValueError):
        exams_faculty.SetInstructorsRequest.model_validate({"identity_keys": [], "owner_uid": "me"})
