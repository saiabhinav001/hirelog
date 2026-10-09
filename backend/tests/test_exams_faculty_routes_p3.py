"""Prompt 3 (ADR 0019): HTTP behaviour of the new/extended faculty routes.

Authorization dependencies are replaced by one pre-seeded teacher (who may do
what is covered by the scope and route-guard tests); the data layer is the
in-memory Firestore from `exam_inmemory.py`, so the handlers run the real
command service end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_faculty as faculty_routes
from app.api.routes import exams_generation as generation_routes
from app.core.config import settings
from app.domain.exams.models import AttemptStatus, RunStatus
from app.domain.exams.policy import ExamPolicy
from app.domain.generation.models import (
    BlueprintSlot,
    CandidateStatus,
    ExamBlueprint,
    GenerationCandidate,
    ItemType,
)
from app.exam_main import app
from exam_inmemory import audit_kinds, make_env, outbox_kinds, seed_attempt, seed_run

BASE = "/api/exams/v1/faculty"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
MIN = timedelta(minutes=1)


@pytest.fixture
def api(monkeypatch):
    env = make_env(monkeypatch)
    user = {"uid": "teacher-1"}
    monkeypatch.setattr(faculty_routes, "db", env.db)
    monkeypatch.setattr(generation_routes, "db", env.db)
    monkeypatch.setattr(generation_routes, "authorize_definition", lambda _user, _id: None)
    monkeypatch.setattr(generation_routes, "authorize_run", lambda _user, run_id: env.service.runs.get(run_id))

    app.dependency_overrides[deps.require_exams_enabled] = lambda: None
    app.dependency_overrides[deps.require_exam_faculty] = lambda: user
    app.dependency_overrides[scope.require_run_scope] = lambda run_id: user
    app.dependency_overrides[scope.require_definition_scope] = lambda definition_id: user
    app.dependency_overrides[scope.require_blueprint_scope] = lambda blueprint_id: user
    env.client = TestClient(app)
    yield env
    app.dependency_overrides.clear()


def _live_attempt(env, uid="s1", *, admitted_minutes_ago=10, minutes=60, **kw):
    return seed_attempt(env.db, "run-1", uid, admitted_at=NOW - admitted_minutes_ago * MIN, minutes=minutes, **kw)


def _seed_run(env, **kw):
    return seed_run(env.db, now=NOW, **kw)


# -- extend -----------------------------------------------------------------------------------

def test_extend_returns_the_new_deadline_and_is_idempotent_over_http(api) -> None:
    _seed_run(api)
    _live_attempt(api)
    body = {"extra_minutes": 15, "reason": "network outage", "idempotency_key": "e1"}
    first = api.client.post(f"{BASE}/runs/run-1/attempts/s1/extend", json=body)
    again = api.client.post(f"{BASE}/runs/run-1/attempts/s1/extend", json=body)

    assert first.status_code == again.status_code == 200
    assert first.json()["reused_existing_receipt"] is False and again.json()["reused_existing_receipt"] is True
    assert first.json()["deadline"] == again.json()["deadline"]
    assert first.json()["manual_extension_minutes"] == 15


def test_the_legacy_extend_body_still_works(api) -> None:
    _seed_run(api)
    _live_attempt(api)
    response = api.client.post(f"{BASE}/runs/run-1/attempts/s1/extend", json={"extra_minutes": 5})
    assert response.status_code == 200 and response.json()["status"] == "extended"


def test_extending_an_expired_attempt_is_a_409_with_a_stable_reason(api) -> None:
    _seed_run(api)
    _live_attempt(api, admitted_minutes_ago=120)
    response = api.client.post(f"{BASE}/runs/run-1/attempts/s1/extend", json={"extra_minutes": 5})
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "command_rejected", "command": "extend", "reason": "deadline_passed"}


def test_extending_an_unknown_attempt_is_a_404(api) -> None:
    _seed_run(api)
    assert api.client.post(f"{BASE}/runs/run-1/attempts/ghost/extend", json={"extra_minutes": 5}).status_code == 404


@pytest.mark.parametrize("minutes", [0, -3, 24 * 60 + 1])
def test_extension_minutes_are_bounded_at_the_edge(api, minutes) -> None:
    _seed_run(api)
    _live_attempt(api)
    assert api.client.post(f"{BASE}/runs/run-1/attempts/s1/extend", json={"extra_minutes": minutes}).status_code == 422


# -- terminate / recover -------------------------------------------------------------------------

def test_terminate_requires_a_reason_and_a_key(api) -> None:
    _seed_run(api)
    _live_attempt(api)
    url = f"{BASE}/runs/run-1/attempts/s1/terminate"
    assert api.client.post(url, json={"idempotency_key": "k"}).status_code == 422
    assert api.client.post(url, json={"idempotency_key": "k", "reason": "x"}).status_code == 422
    assert api.client.post(url, json={"reason": "caught cheating"}).status_code == 422
    assert api.db.get("exam_runs", "run-1", "attempts", "s1")["status"] == "in_progress"  # nothing written


def test_terminate_then_retry_then_second_terminate(api) -> None:
    _seed_run(api)
    _live_attempt(api)
    url = f"{BASE}/runs/run-1/attempts/s1/terminate"
    body = {"idempotency_key": "k1", "reason": "caught cheating"}
    first, retry = api.client.post(url, json=body), api.client.post(url, json=body)
    other = api.client.post(url, json={"idempotency_key": "k2", "reason": "caught cheating"})

    assert first.status_code == retry.status_code == 200
    assert first.json()["status"] == "expired" and retry.json()["reused_existing_receipt"] is True
    assert other.status_code == 409 and other.json()["detail"]["reason"] == "attempt_resolved"
    assert outbox_kinds(api.db) == ["attempt_finalized"] and audit_kinds(api.db) == ["exam_attempt_terminated"]


def test_recover_only_works_for_sealed_attempts(api) -> None:
    _seed_run(api, enrolled=("s1", "s2"))
    _live_attempt(api, "s1")
    _live_attempt(api, "s2", status=AttemptStatus.SUBMITTED)
    body = {"idempotency_key": "r1", "reason": "grading trigger lost"}
    live = api.client.post(f"{BASE}/runs/run-1/attempts/s1/recover", json=body)
    sealed = api.client.post(f"{BASE}/runs/run-1/attempts/s2/recover", json=body)
    assert live.status_code == 409 and live.json()["detail"]["reason"] == "attempt_not_sealed"
    assert sealed.status_code == 200 and sealed.json()["status"] == "submitted"


# -- grant / revoke ---------------------------------------------------------------------------------

def test_grant_and_revoke_over_http(api) -> None:
    _seed_run(api, enrolled=("s1",))
    grant = api.client.post(f"{BASE}/runs/run-1/access/grant", json={"uid": "s2", "idempotency_key": "g1"})
    assert grant.status_code == 200 and grant.json()["changed"] is True
    assert api.db.get("exam_runs", "run-1")["enrolled_uids"] == ["s1", "s2"]
    revoke = api.client.post(f"{BASE}/runs/run-1/access/revoke", json={"uid": "s2", "idempotency_key": "g2"})
    assert revoke.status_code == 200 and revoke.json()["changed"] is True
    assert api.db.get("exam_runs", "run-1")["enrolled_uids"] == ["s1"]


def test_revoking_a_student_with_an_attempt_is_a_409(api) -> None:
    _seed_run(api, enrolled=("s1",))
    _live_attempt(api)
    response = api.client.post(f"{BASE}/runs/run-1/access/revoke", json={"uid": "s1", "idempotency_key": "g"})
    assert response.status_code == 409 and response.json()["detail"]["reason"] == "attempt_exists"


def test_granting_to_an_unknown_run_is_a_404(api) -> None:
    assert api.client.post(f"{BASE}/runs/nope/access/grant", json={"uid": "s2", "idempotency_key": "g"}).status_code == 404


def test_an_institution_run_grant_still_checks_institution_membership(api, monkeypatch) -> None:
    from fastapi import HTTPException

    _seed_run(api, institution_id="inst-1")
    seen = []

    def deny(institution_id, uids):
        seen.append((institution_id, list(uids)))
        raise HTTPException(status_code=422, detail={"code": "enrollment_outside_institution"})

    monkeypatch.setattr(faculty_routes, "validate_enrollment_scope", deny)
    response = api.client.post(f"{BASE}/runs/run-1/access/grant", json={"uid": "outsider", "idempotency_key": "g"})
    assert response.status_code == 422 and seen == [("inst-1", ["outsider"])]
    assert api.db.get("exam_runs", "run-1")["enrolled_uids"] == ["s1"]


# -- run commands carry reason + actor ------------------------------------------------------------------

def test_run_command_reason_and_actor_land_on_the_receipt(api) -> None:
    _seed_run(api, status=RunStatus.RELEASED)
    response = api.client.post(
        f"{BASE}/runs/run-1/commands/pause",
        json={"expected_version": 1, "idempotency_key": "p1", "reason": "fire drill"},
    )
    assert response.status_code == 200
    assert response.json()["receipt"]["actor_uid"] == "teacher-1" and response.json()["receipt"]["reason"] == "fire drill"


def test_a_stale_run_command_is_a_409_version_conflict(api) -> None:
    _seed_run(api, status=RunStatus.RELEASED, version=3)
    response = api.client.post(f"{BASE}/runs/run-1/commands/pause", json={"expected_version": 1, "idempotency_key": "p"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "version_conflict"


def test_an_illegal_run_transition_is_a_409(api) -> None:
    _seed_run(api, status=RunStatus.DRAFT)
    response = api.client.post(f"{BASE}/runs/run-1/commands/end", json={"expected_version": 1, "idempotency_key": "e"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "illegal_transition"


# -- finalize-expired ----------------------------------------------------------------------------------------

def test_finalize_expired_seals_only_attempts_past_their_own_deadline(api) -> None:
    _seed_run(api, enrolled=("late", "ontime", "acc"))
    _live_attempt(api, "late", admitted_minutes_ago=90)
    _live_attempt(api, "ontime", admitted_minutes_ago=10)
    _live_attempt(api, "acc", admitted_minutes_ago=90, minutes=120)  # accommodated: 30 minutes left
    response = api.client.post(f"{BASE}/runs/run-1/finalize-expired")
    assert response.status_code == 200 and response.json()["sealed"] == 1
    statuses = {u: api.db.get("exam_runs", "run-1", "attempts", u)["status"] for u in ("late", "ontime", "acc")}
    assert statuses == {"late": "expired", "ontime": "in_progress", "acc": "in_progress"}


# -- publish preview / publish -----------------------------------------------------------------------------------

def _blueprint(env, definition_id="def-1", negative=0.0):
    slots = [BlueprintSlot(slot_index=0, item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy",
                           marks=4, negative_marks=negative, target_minutes=5, requested_variant_count=1)]
    bp = ExamBlueprint(id="bp1", exam_definition_id=definition_id, title="t", slots=slots, created_by="t",
                       created_at=NOW, updated_at=NOW)
    env.db.put("exam_blueprints", "bp1", bp.model_dump(mode="json"))


def _candidate(env, cid="c1", status=CandidateStatus.APPROVED):
    c = GenerationCandidate(id=cid, request_id="r", blueprint_id="bp1", slot_index=0, family_id="f", local_candidate_id=cid,
                            item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy", marks=4,
                            estimated_minutes=5, uniqueness_signature=cid, public_prompt="p", status=status, created_at=NOW)
    env.db.put("generation_candidates", cid, c.model_dump(mode="json"))


def _definition(env):
    definition = env.service.create_definition(title="Midterm", owner_uid="teacher-1")
    return definition.id


def test_preview_shows_total_marks_and_resolved_counts_before_publishing(api) -> None:
    definition_id = _definition(api)
    _blueprint(api, definition_id)
    _candidate(api)
    response = api.client.get(f"{BASE}/definitions/{definition_id}/publish-preview",
                              params={"blueprint_id": "bp1", "duration_minutes": 60})
    assert response.status_code == 200
    data = response.json()
    assert data["total_marks"] == 4 and data["slot_count"] == 1 and data["publishable"] is True
    assert data["counts_by_type"] == {"single_choice": 1} and data["blockers"] == []
    assert audit_kinds(api.db) == []  # a preview writes nothing


def test_preview_errors_map_to_404_and_409(api) -> None:
    definition_id = _definition(api)
    assert api.client.get(f"{BASE}/definitions/{definition_id}/publish-preview", params={"blueprint_id": "missing"}).status_code == 404
    _blueprint(api, "another-definition")
    other = api.client.get(f"{BASE}/definitions/{definition_id}/publish-preview", params={"blueprint_id": "bp1"})
    assert other.status_code == 409 and other.json()["detail"]["reason"] == "blueprint_definition_mismatch"


def test_publish_needs_slot_count_or_blueprint(api) -> None:
    definition_id = _definition(api)
    assert api.client.post(f"{BASE}/definitions/{definition_id}/publish", json={}).status_code == 422


def test_legacy_publish_response_keeps_its_shape(api) -> None:
    definition_id = _definition(api)
    response = api.client.post(f"{BASE}/definitions/{definition_id}/publish", json={"slot_count": 3})
    assert response.status_code == 200
    assert set(response.json()) == {"id", "definition_id", "revision_number", "title", "published_at", "published_by", "slot_count"}


def test_blueprint_publish_blockers_are_a_422_listing_every_blocker(api) -> None:
    definition_id = _definition(api)
    _blueprint(api, definition_id)  # no approved candidate
    response = api.client.post(f"{BASE}/definitions/{definition_id}/publish", json={"blueprint_id": "bp1"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "publication_blocked"
    assert any("no approved candidate" in b for b in response.json()["detail"]["blockers"])


def test_blueprint_publish_succeeds_and_exposes_the_frozen_summary(api) -> None:
    definition_id = _definition(api)
    _blueprint(api, definition_id)
    _candidate(api)
    response = api.client.post(f"{BASE}/definitions/{definition_id}/publish",
                               json={"blueprint_id": "bp1", "reason": "first release", "idempotency_key": "pub"})
    assert response.status_code == 200
    data = response.json()
    assert data["total_marks"] == 4 and data["slot_count"] == 1 and data["publish_reason"] == "first release"
    assert data["slots"][0]["approved_candidate_ids"] == ["c1"]
    replay = api.client.post(f"{BASE}/definitions/{definition_id}/publish",
                             json={"blueprint_id": "bp1", "reason": "first release", "idempotency_key": "pub"})
    assert replay.json()["id"] == data["id"]


# -- run creation validates the new policy fields -----------------------------------------------------------------------

def test_creating_a_run_rejects_an_invalid_schedule_and_timezone(api, monkeypatch) -> None:
    definition_id = _definition(api)
    monkeypatch.setattr(faculty_routes, "authorize_definition", lambda _u, _d: api.service.definitions.get(definition_id))
    monkeypatch.setattr(faculty_routes, "validate_enrollment_scope", lambda *_: None)
    revision = api.client.post(f"{BASE}/definitions/{definition_id}/publish", json={"slot_count": 2}).json()
    base = {"definition_id": definition_id, "revision_id": revision["id"], "enrolled_uids": ["s1"]}
    start = NOW + timedelta(days=1)

    bad_zone = api.client.post(f"{BASE}/runs", json={**base, "policy": {"duration_minutes": 60, "timezone": "Mars/Base"}})
    naive = api.client.post(f"{BASE}/runs", json={**base, "policy": {"duration_minutes": 60, "scheduled_start": "2030-01-01T10:00:00"}})
    reversed_window = api.client.post(f"{BASE}/runs", json={**base, "policy": {
        "duration_minutes": 60, "scheduled_start": start.isoformat(), "scheduled_end": (start - MIN).isoformat()}})
    assert (bad_zone.status_code, naive.status_code, reversed_window.status_code) == (422, 422, 422)

    ok = api.client.post(f"{BASE}/runs", json={**base, "policy": {
        "duration_minutes": 60, "timezone": "Asia/Kolkata", "scheduled_start": start.isoformat(),
        "scheduled_end": (start + 2 * 60 * MIN).isoformat(), "variant_policy": "balanced_reuse"}})
    assert ok.status_code == 200
    assert ok.json()["policy"]["timezone"] == "Asia/Kolkata" and ok.json()["revision_id"] == revision["id"]
    assert "exam_run_created" in audit_kinds(api.db)


def test_a_run_created_the_old_way_has_neutral_new_fields(api, monkeypatch) -> None:
    definition_id = _definition(api)
    monkeypatch.setattr(faculty_routes, "authorize_definition", lambda _u, _d: api.service.definitions.get(definition_id))
    monkeypatch.setattr(faculty_routes, "validate_enrollment_scope", lambda *_: None)
    revision = api.client.post(f"{BASE}/definitions/{definition_id}/publish", json={"slot_count": 2}).json()
    run = api.client.post(f"{BASE}/runs", json={"definition_id": definition_id, "revision_id": revision["id"],
                                                "policy": {"duration_minutes": 45}, "enrolled_uids": ["s1"]}).json()
    assert run["policy"]["variant_policy"] == "unique" and run["policy"]["scheduled_start"] is None


# -- blueprint creation + variant policy -----------------------------------------------------------------------------------

def _blueprint_body(negative):
    return {"exam_definition_id": "def-1", "title": "t", "slots": [{
        "slot_index": 0, "item_type": "single_choice", "objective_ids": ["o1"], "difficulty": "easy", "marks": 2,
        "negative_marks": negative, "target_minutes": 5, "requested_variant_count": 1}]}


def test_negative_marks_are_rejected_until_the_deployment_enables_them(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_NEGATIVE_MARKING_ENABLED", False)
    blocked = api.client.post(f"{BASE}/blueprints", json=_blueprint_body(0.5))
    assert blocked.status_code == 422 and blocked.json()["detail"]["code"] == "negative_marking_not_enabled"
    assert api.client.post(f"{BASE}/blueprints", json=_blueprint_body(0)).status_code == 200
    monkeypatch.setattr(settings, "EXAM_NEGATIVE_MARKING_ENABLED", True)
    assert api.client.post(f"{BASE}/blueprints", json=_blueprint_body(0.5)).status_code == 200
    assert api.client.post(f"{BASE}/blueprints", json=_blueprint_body(2.5)).status_code == 422  # exceeds marks


def test_the_runs_variant_policy_pre_authorises_balanced_reuse(api) -> None:
    _blueprint(api)
    _candidate(api)  # one approved variant for two students
    for policy, expected in (("unique", 409), ("balanced_reuse", 200)):
        run_id = f"run-{policy}"
        seed_run(api.db, run_id=run_id, now=NOW, enrolled=("s1", "s2"),
                 policy=ExamPolicy(duration_minutes=60, variant_policy=policy))
        response = api.client.post(
            f"{BASE}/blueprints/bp1/slots/0/reserve-variants", json={"run_id": run_id, "uids": ["s1", "s2"]}
        )
        assert response.status_code == expected, (policy, response.text)
    assert response.json()["assignments"] == {"s1": "c1", "s2": "c1"}


# -- authorization wiring of the new routes (the general tripwires live in test_exam_authz_route_guards) --

def _flatten(dependant, acc):
    for sub in dependant.dependencies:
        if sub.call is not None:
            acc.add(getattr(sub.call, "__name__", type(sub.call).__name__))
        _flatten(sub, acc)
    return acc


def test_every_new_faculty_route_is_bound_to_its_resource_scope() -> None:
    from fastapi.routing import APIRoute

    expected = {
        ("POST", "/runs/{run_id}/attempts/{uid}/terminate"): "require_run_scope",
        ("POST", "/runs/{run_id}/attempts/{uid}/recover"): "require_run_scope",
        ("POST", "/runs/{run_id}/access/grant"): "require_run_scope",
        ("POST", "/runs/{run_id}/access/revoke"): "require_run_scope",
        ("POST", "/runs/{run_id}/finalize-expired"): "require_run_scope",
        ("GET", "/definitions/{definition_id}/publish-preview"): "require_definition_scope",
    }
    found = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(BASE):
            for method in route.methods:
                found[(method, route.path[len(BASE):])] = _flatten(route.dependant, set())
    for key, dependency in expected.items():
        assert key in found, f"{key} is not registered"
        assert dependency in found[key] and "require_exam_faculty" in found[key], key
