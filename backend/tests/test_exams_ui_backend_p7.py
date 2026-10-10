"""Prompt 7 (ADR 0023) backend support for the workspace UI: time-warning thresholds, the server's clock reading
on state responses, server-stamped freshness for the control room, and short share links."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_faculty as faculty_routes
from app.api.routes import exams_student as student_routes
from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.exams.models import RunStatus
from app.domain.exams.policy import ExamPolicy
from app.domain.exams.short_links import (
    ALPHABET,
    CODE_LENGTH,
    ShortLinkError,
    ShortLinkService,
    format_code,
    new_code,
    normalize_code,
)
from app.exam_main import app
from exam_inmemory import T0, audit_kinds, make_env, seed_attempt, seed_run
from test_exams_student_attempts_p6 import BASE, RUN, api  # noqa: F401 - fixture reused

FACULTY = "/api/exams/v1/faculty"


# -- time warning thresholds -----------------------------------------------------------------------

def test_the_default_warning_is_ten_minutes_and_thresholds_are_normalised() -> None:
    assert ExamPolicy(duration_minutes=60).time_warning_minutes == [10]
    assert ExamPolicy(duration_minutes=60, time_warning_minutes=[5, 30, 5, 10]).time_warning_minutes == [30, 10, 5]
    assert ExamPolicy(duration_minutes=60, time_warning_minutes=[]).time_warning_minutes == []


@pytest.mark.parametrize("bad", [[0], [-5], [1441], list(range(1, 10))])
def test_invalid_warning_thresholds_are_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, time_warning_minutes=bad)


def test_a_policy_stored_before_this_change_reads_with_the_default() -> None:
    legacy = {"duration_minutes": 30, "warning_threshold": 3}
    assert ExamPolicy.model_validate(legacy).time_warning_minutes == [10]


# -- server time on student state ----------------------------------------------------------------------

def test_state_carries_the_servers_clock_and_the_runs_thresholds(api) -> None:  # noqa: F811
    body = api.client.get(f"{BASE}/runs/{RUN}/state").json()
    assert body["server_time"] == T0.isoformat() and body["time_warning_minutes"] == [10]
    assert datetime.fromisoformat(body["deadline"].replace("Z", "+00:00")) == T0 + timedelta(minutes=60)


def test_the_server_clock_not_the_browser_decides_what_time_it_is(api) -> None:  # noqa: F811
    first = api.client.get(f"{BASE}/runs/{RUN}/state", headers={"Date": "Mon, 01 Jan 1990 00:00:00 GMT"}).json()
    api.env.clock.advance(timedelta(minutes=45))
    later = api.client.get(f"{BASE}/runs/{RUN}/state").json()
    assert first["server_time"] == T0.isoformat() and later["server_time"] == (T0 + timedelta(minutes=45)).isoformat()


def test_a_delayed_finalizer_leaves_the_attempt_open_but_the_server_time_shows_the_deadline_has_passed(api) -> None:  # noqa: F811
    api.env.clock.advance(timedelta(minutes=75))
    state = api.client.get(f"{BASE}/runs/{RUN}/state").json()
    assert state["status"] == "in_progress" and state["server_time"] > state["deadline"]  # the UI shows "finalizing"
    assert api.client.get(f"{BASE}/runs/{RUN}/question").status_code == 403  # no content past the deadline
    api.env.sweeper.sweep_once(RUN, batch_size=10)
    assert api.client.get(f"{BASE}/runs/{RUN}/state").json()["status"] == "expired"


def test_the_navigator_data_also_carries_server_time(api) -> None:  # noqa: F811
    assert api.client.get(f"{BASE}/runs/{RUN}/slots").json()["server_time"] == T0.isoformat()


# -- faculty freshness -----------------------------------------------------------------------------------

@pytest.fixture
def fac(monkeypatch):
    env = make_env(monkeypatch)
    user = {"uid": "teacher-1"}
    for module in (faculty_routes, student_routes):
        monkeypatch.setattr(module, "db", env.db)
    monkeypatch.setattr(student_routes, "_service", lambda: __import__("app.domain.exams.student_service", fromlist=["x"]).ExamStudentService(env.db, clock=env.clock))
    app.dependency_overrides[deps.require_exams_enabled] = lambda: None
    app.dependency_overrides[deps.require_exam_faculty] = lambda: user
    app.dependency_overrides[scope.require_run_scope] = lambda run_id: user
    who = {"uid": "s1"}
    app.dependency_overrides[deps.get_current_exam_user] = lambda: {"uid": who["uid"]}
    app.dependency_overrides[deps.require_student_run_scope] = lambda: {"uid": who["uid"]}
    app.dependency_overrides[deps.require_seb_admission] = lambda: {"uid": who["uid"]}
    env.client, env.who = TestClient(app), who
    monkeypatch.setattr(settings, "EXAM_SHORT_LINKS_ENABLED", True)
    # every test gets fresh limiters so a shared in-process window cannot leak between tests
    monkeypatch.setattr(faculty_routes, "_short_link_issue_limiter", SlidingWindowLimiter(100, 60, namespace="p7-test-issue"))
    monkeypatch.setattr(student_routes, "_short_link_user_limiter", SlidingWindowLimiter(100, 60, namespace="p7-test-user"))
    monkeypatch.setattr(student_routes, "_short_link_ip_limiter", SlidingWindowLimiter(100, 60, namespace="p7-test-ip"))
    seed_run(env.db, run_id="run-1", status=RunStatus.RELEASED, enrolled=("s1", "s2"))
    yield env
    app.dependency_overrides.clear()


def test_the_control_room_data_is_stamped_by_the_server(fac) -> None:
    seed_attempt(fac.db, "run-1", "s1")
    run = fac.client.get(f"{FACULTY}/runs/run-1").json()
    attempts = fac.client.get(f"{FACULTY}/runs/run-1/attempts").json()
    assert "server_time" in run and "server_time" in attempts and run["id"] == "run-1"
    assert [a["uid"] for a in attempts["results"]] == ["s1"]


# -- short code primitives ---------------------------------------------------------------------------------

def test_codes_are_random_unambiguous_and_non_sequential() -> None:
    codes = [new_code() for _ in range(2000)]
    assert len(set(codes)) == 2000
    assert all(len(c) == CODE_LENGTH and set(c) <= set(ALPHABET) for c in codes)
    assert not set("ILOU") & set(ALPHABET)
    assert codes != sorted(codes) and codes != sorted(codes, reverse=True)
    assert len({c[0] for c in codes}) > 20  # not a counter: the first character varies across the whole alphabet


@pytest.mark.parametrize("raw, expected", [
    ("abcd-efgh", "ABCDEFGH"), (" ABCD EFGH ", "ABCDEFGH"), ("0123456O", "01234560"), ("1111iL11", "11111111"), ("ABCDEFG", None),
    ("ABCDEFGHJ", None), ("ABCD-EFG!", None), ("", None),
])
def test_codes_are_normalised_forgivingly_and_malformed_ones_refused(raw, expected) -> None:
    assert normalize_code(raw) == expected
    assert format_code("ABCDEFGH") == "ABCD-EFGH"


# -- the service ------------------------------------------------------------------------------------------

def _service(fac, codes=None) -> ShortLinkService:
    from app.domain.exams.clock import FixedClock

    iterator = iter(codes) if codes else None
    return ShortLinkService(fac.db, clock=fac.clock if hasattr(fac, "clock") else FixedClock(T0), code_factory=(lambda: next(iterator)) if iterator else new_code)


def test_issuing_is_idempotent_and_regenerating_replaces_the_old_code(fac) -> None:
    service = _service(fac)
    first, created = service.issue("run-1", actor_uid="t1")
    again, created_again = service.issue("run-1", actor_uid="t1")
    assert created and not created_again and again.code == first.code
    new, created_new = service.issue("run-1", actor_uid="t1", regenerate=True)
    assert created_new and new.code != first.code
    assert service.links.get(first.code).status == "revoked" and service.links.get(first.code).replaced_by == new.code
    assert [link.code for link in service.links.active_for_run("run-1")] == [new.code]
    assert service.resolve(first.code) is None and service.resolve(new.code).run_id == "run-1"
    assert audit_kinds(fac.db) == ["exam_short_link_issued", "exam_short_link_issued"]


def test_a_code_collision_draws_another_and_never_overwrites_the_taken_one(fac) -> None:
    service = _service(fac, ["AAAAAAAA", "BBBBBBBB", "CCCCCCCC"])
    taken, _ = service.issue("run-1", actor_uid="t1")
    assert taken.code == "AAAAAAAA"
    seed_run(fac.db, run_id="run-2", status=RunStatus.RELEASED)
    colliding = _service(fac, ["AAAAAAAA", "AAAAAAAA", "DDDDDDDD"])
    other, _ = colliding.issue("run-2", actor_uid="t1")
    assert other.code == "DDDDDDDD"
    assert colliding.links.get("AAAAAAAA").run_id == "run-1"  # the first owner kept its code


def test_running_out_of_free_codes_is_an_error_not_an_overwrite(fac) -> None:
    _service(fac, ["AAAAAAAA"]).issue("run-1", actor_uid="t1")
    seed_run(fac.db, run_id="run-2", status=RunStatus.RELEASED)
    with pytest.raises(ShortLinkError) as caught:
        _service(fac, ["AAAAAAAA"] * 10).issue("run-2", actor_uid="t1")
    assert caught.value.code == "code_space_exhausted"
    assert fac.db.get("exam_short_links", "AAAAAAAA")["run_id"] == "run-1"


def test_concurrent_regenerations_leave_exactly_one_active_code(fac) -> None:
    service = _service(fac)
    service.issue("run-1", actor_uid="t1")
    results: list = []

    def regenerate() -> None:
        results.append(service.issue("run-1", actor_uid="t1", regenerate=True)[0].code)

    threads = [threading.Thread(target=regenerate) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    active = service.links.active_for_run("run-1")
    assert len(active) == 1 and active[0].code in results


def test_revoking_stops_the_code_and_is_audited_once(fac) -> None:
    service = _service(fac)
    link, _ = service.issue("run-1", actor_uid="t1")
    assert service.revoke("run-1", actor_uid="t1") == 1 and service.revoke("run-1", actor_uid="t1") == 0
    assert service.resolve(link.code) is None and service.current("run-1") is None
    assert audit_kinds(fac.db).count("exam_short_link_revoked") == 1


# -- routes ---------------------------------------------------------------------------------------------------

def _issue(fac, **body):
    return fac.client.post(f"{FACULTY}/runs/run-1/short-link", json=body)


def test_short_links_are_off_unless_enabled(fac, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_SHORT_LINKS_ENABLED", False)
    assert fac.client.get(f"{FACULTY}/runs/run-1/short-link").json() == {"enabled": False, "link": None}
    refused = _issue(fac)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "short_links_not_enabled"
    assert fac.client.get(f"{BASE}/short-links/ABCDEFGH").status_code == 404


def test_a_teacher_creates_shares_regenerates_and_revokes_a_link(fac) -> None:
    created = _issue(fac).json()
    code = created["link"]["code"]
    assert created["created"] is True and created["link"]["display_code"] == format_code(code)
    assert _issue(fac).json()["link"]["code"] == code  # same link until told otherwise
    assert fac.client.get(f"{FACULTY}/runs/run-1/short-link").json()["link"]["code"] == code
    replaced = _issue(fac, regenerate=True).json()["link"]["code"]
    assert replaced != code
    assert fac.client.get(f"{BASE}/short-links/{code}").status_code == 404
    assert fac.client.get(f"{BASE}/short-links/{replaced}").json() == {"run_id": "run-1"}
    assert fac.client.post(f"{FACULTY}/runs/run-1/short-link/revoke").json()["revoked"] == 1
    assert fac.client.get(f"{BASE}/short-links/{replaced}").status_code == 404


def test_the_resolver_names_the_run_only_to_an_enrolled_student_and_looks_the_same_otherwise(fac) -> None:
    code = _issue(fac).json()["link"]["code"]
    assert fac.client.get(f"{BASE}/short-links/{code.lower()}").json() == {"run_id": "run-1"}
    fac.who["uid"] = "outsider"
    stranger = fac.client.get(f"{BASE}/short-links/{code}")
    unknown = fac.client.get(f"{BASE}/short-links/ZZZZZZZZ")
    malformed = fac.client.get(f"{BASE}/short-links/not-a-code")
    assert [r.status_code for r in (stranger, unknown, malformed)] == [404, 404, 404]
    assert stranger.json() == unknown.json() == malformed.json()


def test_a_short_link_is_a_locator_and_grants_no_access(fac) -> None:
    code = _issue(fac).json()["link"]["code"]
    run_id = fac.client.get(f"{BASE}/short-links/{code}").json()["run_id"]
    fac.who["uid"] = "s2"  # enrolled but never admitted: the run id from the link opens nothing
    assert fac.client.get(f"{BASE}/runs/{run_id}/question").status_code in (403, 404)
    assert fac.client.get(f"{BASE}/runs/{run_id}/state").json()["status"] == "not_started"


def test_the_resolver_and_issuing_are_rate_limited(fac, monkeypatch) -> None:
    monkeypatch.setattr(student_routes, "_short_link_user_limiter", SlidingWindowLimiter(3, 60, namespace="p7-test-user-small"))
    statuses = [fac.client.get(f"{BASE}/short-links/ZZZZZZZZ").status_code for _ in range(5)]
    assert statuses == [404, 404, 404, 429, 429]
    monkeypatch.setattr(faculty_routes, "_short_link_issue_limiter", SlidingWindowLimiter(2, 60, namespace="p7-test-issue-small"))
    assert [_issue(fac, regenerate=True).status_code for _ in range(3)] == [200, 200, 429]


def test_no_link_can_be_issued_for_a_run_that_no_longer_admits(fac) -> None:
    seed_run(fac.db, run_id="run-1", status=RunStatus.ENDED, enrolled=("s1",))
    refused = _issue(fac)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "run_not_admitting"


def test_the_short_link_routes_are_bound_to_the_run_scope() -> None:
    from fastapi.routing import APIRoute

    def names(dependant, acc):
        for sub in dependant.dependencies:
            acc.add(getattr(sub.call, "__name__", ""))
            names(sub, acc)
        return acc

    found = {
        (m, r.path): names(r.dependant, set())
        for r in app.routes if isinstance(r, APIRoute) and "/short-link" in r.path for m in r.methods
    }
    faculty = {k: v for k, v in found.items() if "/faculty/" in k[1]}
    assert len(faculty) == 3 and all("require_run_scope" in v and "require_exam_faculty" in v for v in faculty.values())
    assert any("/student/short-links/" in k[1] for k in found)
