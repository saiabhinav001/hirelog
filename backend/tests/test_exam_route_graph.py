"""Route-graph regression tests (the technique the adversarial review used to
find that no route re-checked membership): walk every registered route's
dependency graph and assert the authorization invariants structurally, so a
new route cannot silently skip them."""

from __future__ import annotations

import re

from fastapi.routing import APIRoute, APIWebSocketRoute

from app.api import exams_dependencies as deps
from app.api import exams_scope
from app.exam_main import app

PUBLIC_PATHS = {
    "/", "/health/live", "/health/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/api/exams/v1/auth/challenge", "/api/exams/v1/auth/verify",
    "/api/exams/v1/live/ws",  # authenticated by a single-use ticket, re-validated while open
}


def _calls(dependant):
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _exam_routes():
    for route in app.routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)) and route.path.startswith("/api/exams/v1"):
            yield route


def _guards(route):
    return [c for c in _calls(route.dependant) if getattr(c, "__name__", "") == "enforce_faculty_scope"]


def test_every_exam_route_is_feature_flagged() -> None:
    missing = [r.path for r in _exam_routes() if deps.require_exams_enabled not in set(_calls(r.dependant))]
    assert missing == []


def test_every_non_public_exam_route_requires_a_verified_identity() -> None:
    missing = [
        f"{getattr(r, 'methods', '')} {r.path}" for r in _exam_routes()
        if r.path not in PUBLIC_PATHS and deps.get_exam_identity not in set(_calls(r.dependant))
    ]
    assert missing == []


def test_every_non_public_exam_route_re_checks_session_freshness() -> None:
    """`get_current_exam_user` is where revoked/stale institution-backed
    sessions are rejected (review §1) — it must be in every authenticated graph."""
    missing = [
        f"{getattr(r, 'methods', '')} {r.path}" for r in _exam_routes()
        if r.path not in PUBLIC_PATHS and deps.get_current_exam_user not in set(_calls(r.dependant))
    ]
    assert missing == []


def test_every_faculty_route_runs_the_scope_guard() -> None:
    missing = [
        f"{sorted(r.methods)} {r.path}" for r in _exam_routes()
        if "/faculty/" in r.path and not _guards(r)
    ]
    assert missing == []


def test_every_faculty_path_parameter_is_scoped_or_explicitly_exempt() -> None:
    """A new `{something_id}` on a faculty route with no resolver would fail
    closed at runtime (500); this catches it in CI instead."""
    problems = []
    for route in _exam_routes():
        if "/faculty/" not in route.path:
            continue
        resolvers = _guards(route)[0].scope_resolvers
        for name in re.findall(r"{([a-z_]+)}", route.path):
            if name not in resolvers and name not in exams_scope.NON_RESOURCE_PARAMS:
                problems.append(f"{route.path}: {name}")
    assert problems == []


def test_every_student_run_route_checks_enrolment_and_membership() -> None:
    gated = {deps.require_student_run_access}
    missing = []
    for route in _exam_routes():
        if "/student/" in route.path and "{run_id}" in route.path:
            if not (gated & set(_calls(route.dependant))):
                missing.append(f"{sorted(route.methods)} {route.path}")
    assert missing == []


def test_student_routes_with_a_body_or_query_run_id_check_access_in_the_handler() -> None:
    """The judge routes take `run_id` outside the path, so the structural check
    above cannot see them — pin the handler-level calls instead."""
    import inspect

    from app.api.routes import exams_judge

    for fn in (exams_judge.public_run, exams_judge.get_public_run):
        assert "assert_student_run_access" in inspect.getsource(fn), fn.__name__


def test_unauthenticated_requests_are_rejected_not_served() -> None:
    from fastapi.testclient import TestClient

    from app.core.config import settings

    original = settings.EXAMS_ENABLED
    settings.EXAMS_ENABLED = True
    try:
        client = TestClient(app)
        for path in ("/api/exams/v1/me", "/api/exams/v1/student/runs", "/api/exams/v1/faculty/definitions"):
            assert client.get(path).status_code in (401, 403)
    finally:
        settings.EXAMS_ENABLED = original
