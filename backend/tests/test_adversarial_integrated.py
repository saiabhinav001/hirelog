"""Integrated authorization probes (final adversarial review).

Unlike the route tests, which replace the whole authorization chain with a stub, these override ONLY token
verification (`get_current_user_uncached`) and let the real dependency chain run against the in-memory
Firestore. They sweep every registered exam route, so a route added later without the right dependency fails here.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api import exams_scope
from app.api.routes import exams_student as student_routes
from app.core.config import settings
from app.domain.exams.student_service import ExamStudentService
from app.domain.institution.models import Membership, MembershipStatus, RoleScope
from app.domain.institution.repository import MembershipRepository
from app.exam_main import app
from exam_inmemory import T0, make_env, seed_attempt, seed_run

RUN = "run-1"
INST = "inst-1"
PUBLIC = {("GET", "/"), ("GET", "/health/live"), ("GET", "/health/ready"), ("POST", "/api/exams/v1/auth/challenge"), ("POST", "/api/exams/v1/auth/verify")}


def _routes() -> list[tuple[str, str]]:
    found = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method, route.path))
    return found


def _fill(path: str) -> str:
    out = []
    for part in path.split("/"):
        out.append(RUN if part == "{run_id}" else "x" if part.startswith("{") else part)
    return "/".join(out)


PROTECTED = [r for r in _routes() if r not in PUBLIC]
# Not run-scoped by design: the caller's own run list (filtered by membership) and a short-link locator.
OWN_LIST = ("GET", "/api/exams/v1/student/runs")
LOCATOR = ("GET", "/api/exams/v1/student/short-links/{code}")
STUDENT_ROUTES = [r for r in PROTECTED if r[1].startswith("/api/exams/v1/student/") and r not in (OWN_LIST, LOCATOR)]
STAFF_ROUTES = [r for r in PROTECTED if r[1].startswith("/api/exams/v1/faculty/") or r[1].startswith("/api/exams/v1/ops")]
RUN_SCOPED_STAFF = [r for r in STAFF_ROUTES if "{run_id}" in r[1]]


def _membership(role=RoleScope.STUDENT, status=MembershipStatus.ACTIVE, uid="s1", version=1, email="s1@college.edu", institution=INST) -> Membership:
    return Membership(institution_id=institution, identity_key=f"id-{uid}", contact_email=email, role_scope=role, status=status,
                      uid=uid, authorization_version=version, created_at=T0, updated_at=T0)


class World:
    def __init__(self, env, claims) -> None:
        self.env, self.claims = env, claims
        self.client = TestClient(app)

    def member(self, **kwargs) -> None:
        MembershipRepository(self.env.db).upsert(_membership(**kwargs))

    def call(self, method: str, path: str, **headers):
        filled = _fill(path)
        if "/snapshots/upload/" in filled:
            return self.client.request(method, filled, files={"file": ("a.png", b"x", "image/png")}, headers=headers or None)
        body = {"run_id": RUN, "question_id": "rev-1:slot:0", "language_key": "python3.11", "source_code": "print(1)"}
        return self.client.request(method, f"{filled}?run_id={RUN}&question_id=rev-1:slot:0", json=body, headers=headers or None)


def _not_denied(world: World, routes) -> dict:
    """Routes that did not answer 403. The evidence upload is authorized by its single-use ticket, so an
    unknown ticket is a 401 there; every other student route is refused by the run scope."""
    ticket_gated = "/snapshots/upload/"
    return {
        (m, p): s for (m, p) in routes
        if (s := world.call(m, p).status_code) != 403 and not (ticket_gated in p and s == 401)
    }


@pytest.fixture
def world(monkeypatch):
    env = make_env(monkeypatch)
    monkeypatch.setattr(settings, "EXAMS_ENABLED", True)
    monkeypatch.setattr(settings, "EXAM_FACULTY_EMAILS", "")
    monkeypatch.setattr(settings, "EXAM_SUPER_ADMIN_EMAILS", "")
    monkeypatch.setattr(settings, "EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL", False)
    monkeypatch.setattr(deps, "db", env.db)
    monkeypatch.setattr(exams_scope, "db", env.db)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.api.routes.") and hasattr(module, "db"):
            monkeypatch.setattr(module, "db", env.db)  # nothing may reach a real datastore from these probes
    monkeypatch.setattr(student_routes, "_service", lambda: ExamStudentService(env.db, clock=env.clock))
    claims = {"uid": "s1", "token_email": "", "email_verified": False, "exam_institution_id": INST, "exam_authz_version": 1}
    app.dependency_overrides[deps.get_current_user_uncached] = lambda: dict(claims)
    seed_run(env.db, run_id=RUN, enrolled=("s1",), institution_id=INST)
    seed_attempt(env.db, RUN, "s1")
    yield World(env, claims)
    app.dependency_overrides.clear()


# -- the harness is real: a valid member gets through, so the denials below mean something ---------

def test_a_current_member_enrolled_in_the_run_is_admitted_through_the_real_chain(world) -> None:
    world.member()
    assert world.client.get(f"/api/exams/v1/student/runs/{RUN}/state").status_code == 200


# -- revoked / stale / impostor identities -----------------------------------------------------------------

def test_a_revoked_member_with_a_valid_token_is_refused_on_every_student_route(world) -> None:
    world.member(status=MembershipStatus.REVOKED, version=2)
    before = world.env.db.get("exam_runs", RUN, "attempts", "s1")
    statuses = {(m, p): world.call(m, p).status_code for m, p in STUDENT_ROUTES}
    assert {k: v for k, v in statuses.items() if v != 403} == {}
    assert world.env.db.get("exam_runs", RUN, "attempts", "s1") == before  # nothing was written


def test_a_token_older_than_a_role_change_is_refused_on_every_student_route(world) -> None:
    world.member(version=3)  # the token still carries version 1
    assert _not_denied(world, STUDENT_ROUTES) == {}


def test_a_verified_email_matching_a_roster_contact_does_not_create_membership(world) -> None:
    world.member(uid="the-real-student", email="victim@college.edu")
    world.claims.update(uid="impostor", token_email="victim@college.edu", email_verified=True, exam_institution_id=None)
    seed_attempt(world.env.db, RUN, "impostor")
    assert _not_denied(world, STUDENT_ROUTES) == {}


def test_a_member_of_another_institution_cannot_use_this_institutions_run(world) -> None:
    world.member(institution="inst-2")
    world.claims["exam_institution_id"] = "inst-2"
    assert _not_denied(world, STUDENT_ROUTES) == {}


@pytest.mark.parametrize("setup", ["revoked", "other_institution", "no_membership"])
def test_the_own_run_list_does_not_show_a_scoped_run_to_someone_without_a_current_membership(world, setup) -> None:
    world.member()
    assert world.call(*OWN_LIST).json()["results"][0]["run_id"] == RUN  # control: a member sees it
    world.member(**{"revoked": dict(status=MembershipStatus.REVOKED, version=2),
                    "other_institution": dict(institution="inst-2"),
                    "no_membership": dict(uid="someone-else")}[setup])
    if setup != "revoked":
        MembershipRepository(world.env.db).upsert(_membership(status=MembershipStatus.REVOKED, version=2, uid="s1"))
    world.claims["exam_institution_id"] = None
    assert world.call(*OWN_LIST).json() == {"results": []}


# -- every protected route denies a user with no authority -----------------------------------------------

def test_every_non_public_route_rejects_a_request_with_no_credentials(world) -> None:
    app.dependency_overrides.pop(deps.get_current_user_uncached)
    allowed = {401, 403}
    assert {(m, p): s for (m, p) in PROTECTED if (s := world.call(m, p).status_code) not in allowed} == {}


def test_a_signed_in_user_with_no_membership_and_no_allowlist_entry_is_denied_every_staff_route(world) -> None:
    world.claims.update(uid="nobody", exam_institution_id=None)
    assert {(m, p): s for (m, p) in STAFF_ROUTES if (s := world.call(m, p).status_code) != 403} == {}


def test_a_student_membership_is_not_staff_authority(world) -> None:
    world.member()
    assert {(m, p): s for (m, p) in STAFF_ROUTES if (s := world.call(m, p).status_code) != 403} == {}


def test_an_unverified_allowlisted_email_is_not_staff_authority(world, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_FACULTY_EMAILS", "prof@college.edu")
    monkeypatch.setattr(settings, "EXAM_SUPER_ADMIN_EMAILS", "owner@college.edu")
    for email in ("prof@college.edu", "owner@college.edu"):
        world.claims.update(uid="spoof", token_email=email, email_verified=False, exam_institution_id=None)
        assert {(m, p): s for (m, p) in STAFF_ROUTES if (s := world.call(m, p).status_code) != 403} == {}


def test_a_teacher_of_another_institution_is_denied_every_run_scoped_staff_route(world) -> None:
    world.member(role=RoleScope.FACULTY, uid="t1", institution="inst-2")
    world.claims.update(uid="t1", exam_institution_id="inst-2")
    denied = {(m, p): s for (m, p) in RUN_SCOPED_STAFF if (s := world.call(m, p).status_code) != 403}
    assert denied == {}


def test_a_teacher_with_a_revoked_membership_is_denied_every_staff_route(world) -> None:
    world.member(role=RoleScope.FACULTY, uid="t1", status=MembershipStatus.REVOKED, version=2)
    world.claims.update(uid="t1", exam_authz_version=1)
    assert {(m, p): s for (m, p) in STAFF_ROUTES if (s := world.call(m, p).status_code) != 403} == {}


def test_a_verified_super_admin_is_admitted_and_a_verified_teacher_is_not_to_the_super_admin_route(world, monkeypatch) -> None:
    """Positive control for the staff sweeps: the same chain that refuses the callers above admits the right one."""
    monkeypatch.setattr(settings, "EXAM_SUPER_ADMIN_EMAILS", "owner@college.edu")
    monkeypatch.setattr(settings, "EXAM_FACULTY_EMAILS", "prof@college.edu")
    route = ("GET", "/api/exams/v1/ops/queue-health")
    world.claims.update(uid="owner", token_email="owner@college.edu", email_verified=True, exam_institution_id=None)
    assert world.call(*route).status_code == 200
    world.claims.update(uid="prof", token_email="prof@college.edu", email_verified=True)
    assert world.call(*route).status_code == 403


def test_every_student_get_answered_to_a_member_is_no_store(world) -> None:
    world.member()
    uncached = {}
    for method, path in [r for r in STUDENT_ROUTES + [OWN_LIST] if r[0] == "GET"]:
        response = world.call(method, path)
        if "no-store" not in response.headers.get("cache-control", ""):
            uncached[(method, path)] = (response.status_code, response.headers.get("cache-control"))
    assert uncached == {}


def test_the_sweeps_cover_the_routes_that_exist() -> None:
    assert len(STUDENT_ROUTES) >= 20 and len(STAFF_ROUTES) >= 80 and len(RUN_SCOPED_STAFF) >= 30
