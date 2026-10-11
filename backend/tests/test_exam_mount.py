"""Serving the exam API from the main HireLog backend (`app.main`): the mount must leave every existing route,
middleware and error handler alone, keep exam responses no-store, and keep the exam workers single-start."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import exam_main
from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter


def _parent() -> FastAPI:
    parent = FastAPI()

    @parent.get("/api/legacy")
    def legacy() -> dict:
        return {"legacy": True}

    @parent.get("/health/live")
    def live() -> dict:
        return {"status": "alive", "service": "api"}

    parent.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:3000"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )
    exam_main.mount_exam_api(parent)
    return parent


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "EXAMS_ENABLED", True)
    return TestClient(_parent())


def test_existing_routes_still_win_over_the_mounted_exam_app(client) -> None:
    assert client.get("/api/legacy").json() == {"legacy": True}
    assert client.get("/health/live").json()["service"] == "api"
    assert client.get("/no/such/route").status_code == 404


def test_exam_routes_are_served_behind_authentication_and_are_never_cached(client) -> None:
    response = client.get("/api/exams/v1/me")
    assert response.status_code in (401, 403)
    assert response.headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/api/legacy").headers


def test_the_exam_websocket_route_is_reachable_through_the_mount(client) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/api/exams/v1/live/ws?ticket=x&run_id=r"):
            pass
    assert closed.value.code == 4403  # refused for its missing origin: the exam handler answered


def test_cors_answers_once_even_though_both_apps_have_it(client) -> None:
    headers = {"Origin": "http://localhost:3000"}
    actual = client.get("/api/exams/v1/me", headers=headers)
    assert actual.headers.get_list("access-control-allow-origin") == ["http://localhost:3000"]
    preflight = client.options(
        "/api/exams/v1/student/runs", headers={**headers, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
    )
    assert preflight.status_code == 200
    assert preflight.headers.get_list("access-control-allow-origin") == ["http://localhost:3000"]


def test_the_workers_start_once_and_only_when_the_exam_api_is_enabled(monkeypatch) -> None:
    started: list = []

    class _Thread:
        def __init__(self, target=None, daemon=None, **_kw) -> None:
            self.target = target

        def start(self) -> None:
            started.append(self.target.__name__)

    monkeypatch.setattr(exam_main, "threading", SimpleNamespace(Thread=_Thread))
    monkeypatch.setattr(exam_main, "_workers_started", False)
    monkeypatch.setattr(settings, "EXAMS_ENABLED", False)
    exam_main.start_exam_workers()
    assert started == []

    monkeypatch.setattr(settings, "EXAMS_ENABLED", True)
    monkeypatch.setattr(settings, "EXAM_FINALIZATION_SWEEP_ENABLED", True)
    exam_main.start_exam_workers()
    exam_main.start_exam_workers()
    assert len(started) == 7 and "_outbox_drain_loop" in started and "_finalization_sweep_loop" in started

    exam_main.stop_exam_workers()
    assert exam_main._workers_started is False
    exam_main._stop_event.clear()


def test_the_legacy_write_limiter_skips_exam_routes_but_still_limits_everything_else(monkeypatch) -> None:
    import app.main as main

    monkeypatch.setattr(settings, "EXAMS_ENABLED", True)
    mount_count = len(main.app.router.routes)
    exam_main.mount_exam_api(main.app)
    try:
        monkeypatch.setattr(main, "_mutation_limiter", SlidingWindowLimiter(2, 60, namespace="mount-test"))
        client = TestClient(main.app)
        exam_statuses = [client.patch("/api/exams/v1/student/runs/r/drafts/q", json={}).status_code for _ in range(6)]
        assert 429 not in exam_statuses  # autosave must never be throttled by the legacy limit

        legacy_write = next(
            r.path for r in main.app.routes
            if getattr(r, "methods", None) and "POST" in r.methods and r.path.startswith("/api/") and not r.path.startswith("/api/exams/")
        )
        legacy_statuses = [client.post(legacy_write, json={}).status_code for _ in range(6)]
        assert 429 in legacy_statuses
    finally:
        del main.app.router.routes[mount_count:]
