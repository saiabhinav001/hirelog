"""Route-table guards for exam authorization (Prompt 1, ADR 0017).

These introspect the REAL FastAPI app instead of hand-listing routes, so a
route added later without the right dependency fails here rather than shipping
as an unguarded endpoint. They are deliberately strict tripwires: a new path
parameter or a new route family forces an explicit authorization decision
(add the scope dependency, or add the route to the documented exception list
below with the reason).

API authorization is tested here and in the sibling `test_exam_authz_*` files.
`firestore.rules` is tested separately (see test_exam_authz_profile_and_rules.py):
the exam API uses privileged server SDKs that bypass client rules, so the API
checks must hold on their own.
"""

from __future__ import annotations

import inspect
import re
from typing import NamedTuple

from fastapi.routing import APIRoute

from app.api.routes import exams_live
from app.exam_main import app

PREFIX = "/api/exams/v1"


def _name(call) -> str:
    return getattr(call, "__name__", type(call).__name__)


def _flatten(dependant, acc: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            acc.add(_name(sub.call))
        _flatten(sub, acc)
    return acc


class _Route(NamedTuple):
    method: str
    path: str  # relative to PREFIX
    route: APIRoute
    deps: set[str]  # every dependency reachable from the route (nested included)
    router_level: set[str]  # dependencies declared directly on the route/router


def _exam_routes() -> list[_Route]:
    found = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(PREFIX + "/"):
            found.append(
                _Route(
                    method="/".join(sorted(route.methods)),
                    path=route.path[len(PREFIX):],
                    route=route,
                    deps=_flatten(route.dependant, set()),
                    router_level={_name(d.dependency) for d in route.dependencies},
                )
            )
    return found


ROUTES = _exam_routes()
BY_KEY = {(r.method, r.path): r for r in ROUTES}


def _label(r: _Route) -> str:
    return f"{r.method} {r.path}"


# ── baseline guards ──────────────────────────────────────────────────────────

def test_inventory_is_large_enough_for_the_guards_to_be_meaningful() -> None:
    assert len(ROUTES) >= 85
    assert {r.path.split("/")[1] for r in ROUTES} >= {"faculty", "student", "live", "me", "auth"}


def test_every_exam_route_declares_the_feature_flag_at_router_level() -> None:
    """Nested-only declarations are not enough: an unauthenticated caller can be
    rejected by the bearer scheme (403) before a nested flag dependency runs."""
    offenders = [_label(r) for r in ROUTES if "require_exams_enabled" not in r.router_level]
    assert offenders == []


PUBLIC_ROUTES = {("POST", "/auth/challenge"), ("POST", "/auth/verify")}  # pre-login OTP exchange


def test_only_the_otp_pair_is_reachable_without_an_authenticated_exam_user() -> None:
    assert PUBLIC_ROUTES <= set(BY_KEY)
    offenders = [
        _label(r) for r in ROUTES if (r.method, r.path) not in PUBLIC_ROUTES and "get_current_exam_user" not in r.deps
    ]
    assert offenders == []


def test_every_faculty_route_requires_a_staff_role() -> None:
    staff = {"require_exam_faculty", "require_exam_super_admin"}
    offenders = [_label(r) for r in ROUTES if r.path.startswith("/faculty/") and not (staff & r.deps)]
    assert offenders == []


def test_institution_creation_is_super_admin_only() -> None:
    route = BY_KEY[("POST", "/faculty/institutions")]
    assert "require_exam_super_admin" in route.deps
    assert "require_exam_faculty" not in route.deps


# ── resource scope on faculty path ids ───────────────────────────────────────

SCOPE_FOR_PATH_PARAM = {
    "definition_id": "require_definition_scope",
    "blueprint_id": "require_blueprint_scope",
    "candidate_id": "require_candidate_scope",
    "request_id": "require_generation_request_scope",
    "source_id": "require_source_scope",
    "submission_id": "require_submission_scope",
    "review_id": "require_short_answer_review_scope",
    "snapshot_id": "require_snapshot_scope",
    "run_id": "require_run_scope",
}
# Path parameters that are NOT resource ids of their own: a position, a target
# inside an already-scoped run, a command verb, or a single-use ticket whose
# issuing route is itself scoped (ticket redemption is bound to the issuer).
UNSCOPED_PATH_PARAMS = {"slot_index", "question_id", "uid", "command", "ticket_id"}


def _scope_requirements(method: str, path: str, params: list[str]) -> tuple[set[str], list[str]]:
    required: set[str] = set()
    unknown: list[str] = []
    for param in params:
        if param == "definition_id":
            # Changing a definition's instructors is an institution-administration act, not owner access.
            required.add(
                "require_instructor_admin_scope"
                if method == "PUT" and path.endswith("/instructors")
                else "require_definition_scope"
            )
        elif param in SCOPE_FOR_PATH_PARAM:
            required.add(SCOPE_FOR_PATH_PARAM[param])
        elif param == "appeal_id":
            required.add("require_grade_appeal_scope" if "/gradebook/" in path else "require_proctoring_appeal_scope")
        elif param == "job_id" and "/export/" in path:
            required.add("require_export_job_scope")
        elif param == "job_id" and "/roster/" in path:
            continue  # roster job is bound to the (scoped) path institution inside the handler
        elif param == "institution_id":
            required.add(
                "require_institution_admin_scope"
                if re.search(r"/memberships/(revoke|reinstate|unlink)$", path)
                else "require_institution_roster_scope"
            )
        elif param in UNSCOPED_PATH_PARAMS:
            continue
        else:
            unknown.append(param)
    return required, unknown


def test_every_faculty_path_id_is_bound_to_its_scope_dependency() -> None:
    problems: list[str] = []
    for r in ROUTES:
        if not r.path.startswith("/faculty/"):
            continue
        required, unknown = _scope_requirements(r.method, r.path, re.findall(r"{(\w+)}", r.path))
        problems += [f"{_label(r)}: no authorization decision for path parameter '{p}'" for p in unknown]
        problems += [f"{_label(r)}: missing {dep}" for dep in sorted(required - r.deps)]
    assert problems == []


def test_roster_apply_still_binds_the_job_to_the_path_institution() -> None:
    source = inspect.getsource(BY_KEY[("POST", "/faculty/institutions/{institution_id}/roster/{job_id}/apply")].route.endpoint)
    assert "job.institution_id != institution_id" in source


# Actions that change who may do what: a valid token is not enough, the sign-in must be recent
# (a recency check on the Firebase session — not MFA).
REAUTH_ROUTES = {
    ("POST", "/faculty/institutions"),
    ("POST", "/faculty/institutions/{institution_id}/roster/{job_id}/apply"),
    ("POST", "/faculty/institutions/{institution_id}/memberships/revoke"),
    ("POST", "/faculty/institutions/{institution_id}/memberships/reinstate"),
    ("POST", "/faculty/institutions/{institution_id}/memberships/unlink"),
    ("POST", "/faculty/institutions/{institution_id}/teachers"),
    ("PUT", "/faculty/definitions/{definition_id}/instructors"),
}


def test_authority_changing_actions_require_recent_reauthentication() -> None:
    assert REAUTH_ROUTES <= set(BY_KEY)
    assert [f"{m} {p}" for m, p in sorted(REAUTH_ROUTES) if "require_recent_staff_reauth" not in BY_KEY[(m, p)].deps] == []


def test_teacher_assignment_is_super_admin_only() -> None:
    assert "require_exam_super_admin" in BY_KEY[("POST", "/faculty/institutions/{institution_id}/teachers")].deps


def test_roster_bodies_are_read_only_after_the_institution_scope_check() -> None:
    """The CSV/JSON body dependency itself depends on the scope check, so an
    unauthorized caller never gets their upload parsed."""
    for key in (("POST", "/faculty/institutions/{institution_id}/roster/dry-run"),
                ("POST", "/faculty/institutions/{institution_id}/roster/{job_id}/apply")):
        body_deps = [
            sub for sub in BY_KEY[key].route.dependant.dependencies if _name(sub.call) == "_read_roster_payload"
        ]
        assert body_deps, key
        assert "require_institution_roster_scope" in {_name(d.call) for d in body_deps[0].dependencies}


# ── student scope ────────────────────────────────────────────────────────────

# Student routes that do not carry `require_student_run_scope`, and why:
STUDENT_WITHOUT_RUN_DEP = {
    ("GET", "/student/runs"),  # lists the caller's own runs; filters by institution membership inline
    ("POST", "/student/judge/public-run"),  # run_id arrives in the body; authorize_student_for_run inline
    ("POST", "/student/proctoring/snapshots/upload/{ticket_id}"),  # single-use upload ticket bound to uid+run at issue
}


def test_student_routes_are_bound_to_run_enrollment_and_institution_scope() -> None:
    assert STUDENT_WITHOUT_RUN_DEP <= set(BY_KEY)
    offenders = [
        _label(r)
        for r in ROUTES
        if r.path.startswith("/student/")
        and (r.method, r.path) not in STUDENT_WITHOUT_RUN_DEP
        and "require_student_run_scope" not in r.deps
    ]
    assert offenders == []


def test_lockdown_admission_gate_still_composes_the_student_scope_check() -> None:
    gated = [r for r in ROUTES if "require_seb_admission" in r.deps]
    assert len(gated) >= 6
    assert [_label(r) for r in gated if "require_student_run_scope" not in r.deps] == []


# ── routes whose resource id arrives in the body ─────────────────────────────

INLINE_SCOPE_TOKENS = {
    ("POST", "/faculty/definitions"): ("resolve_new_definition_institution",),
    ("GET", "/faculty/definitions"): ("list_by_owner", 'user["uid"]'),
    ("POST", "/faculty/runs"): ("authorize_definition", "validate_enrollment_scope"),
    ("POST", "/faculty/blueprints"): ("authorize_definition",),
    ("POST", "/faculty/generation-requests"): ("authorize_blueprint", "authorize_source"),
    ("POST", "/faculty/blueprints/{blueprint_id}/slots/{slot_index}/reserve-variants"): (
        "authorize_run",
        "validate_variant_targets",
    ),
    ("POST", "/faculty/kt/sources/from-upload"): ("authorize_upload",),
    ("POST", "/faculty/kt/sources/from-url"): ('created_by=user["uid"]',),
    ("POST", "/faculty/kt/uploads"): ('uploaded_by=user["uid"]',),
    ("GET", "/faculty/kt/sources"): ("filter_accessible_sources",),
    ("GET", "/faculty/judge/short-answer-reviews/pending"): ("filter_reviews_for_user",),
    ("GET", "/faculty/institutions"): ("teacher_institution_ids", "is_super_admin"),
    ("POST", "/student/judge/public-run"): ("authorize_student_for_run",),
    ("GET", "/student/runs"): ("resolve_principal", "can_participate_as_student"),
    ("POST", "/live/ticket"): ("_resolve_role",),
}


def test_body_scoped_routes_still_call_their_scope_checks() -> None:
    problems: list[str] = []
    for key, tokens in INLINE_SCOPE_TOKENS.items():
        route = BY_KEY.get(key)
        if route is None:
            problems.append(f"{key}: route not found (stale list?)")
            continue
        source = inspect.getsource(route.route.endpoint)
        problems += [f"{key}: endpoint no longer uses {token}" for token in tokens if token not in source]
    assert problems == []


def test_live_ticket_role_resolution_checks_scope_before_minting() -> None:
    issue = inspect.getsource(exams_live.issue_ticket)
    resolver = inspect.getsource(exams_live._resolve_role)
    assert issue.index("_resolve_role") < issue.index("mint_ticket")
    assert "user_can_manage_run" in resolver and "can_participate_as_student" in resolver


# ── no client-writable authority in request bodies ───────────────────────────

AUTHORITY_FIELDS = {
    "role", "roles", "exam_roles", "role_scope", "is_super_admin", "is_bootstrap_faculty", "is_teacher",
    "is_admin", "is_faculty", "authorization_version", "exam_authz_version", "permissions",
}
# Roster rows legitimately name the role a row should receive; the server caps it
# at the uploader's own standing (`requester_max_role_scope`, see policy tests).
# Teacher assignment names FACULTY/ADMIN, but only a super-admin can call it.
ALLOWED_AUTHORITY_FIELDS = {("RosterRowInput", "role_scope"), ("AssignTeacherRequest", "role_scope")}


def _collect_refs(node, acc: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            acc.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _collect_refs(value, acc)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, acc)


def test_no_exam_request_body_can_carry_role_or_authority_fields() -> None:
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})

    pending: set[str] = set()
    for path, item in spec["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for operation in item.values():
            if isinstance(operation, dict) and "requestBody" in operation:
                _collect_refs(operation["requestBody"], pending)

    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        _collect_refs(schemas.get(name, {}), pending)

    assert seen, "no request schemas found - the introspection is broken"
    violations = [
        f"{name}.{prop}"
        for name in sorted(seen)
        for prop in schemas.get(name, {}).get("properties", {})
        if prop in AUTHORITY_FIELDS and (name, prop) not in ALLOWED_AUTHORITY_FIELDS
    ]
    assert violations == []
