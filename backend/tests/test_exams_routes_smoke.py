"""HTTP-level smoke test: the exams module is safe-by-default when disabled.

This does NOT touch Firestore (the 503 feature-flag check runs before any
route handler would need `db`), so it is safe to run without emulator/creds.
Full request/transaction wiring against a real Firestore is explicitly out of
scope here — see docs/adr/0008 "Known limitations".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import settings
from app.exam_main import app

client = TestClient(app)


def test_faculty_route_is_503_when_exams_disabled() -> None:
    assert settings.EXAMS_ENABLED is False  # default, contract #12
    response = client.post(
        "/api/exams/v1/faculty/runs/does-not-matter/commands/release",
        json={"expected_version": 1, "idempotency_key": "k1"},
    )
    assert response.status_code == 503


def test_student_accommodations_route_is_503_when_exams_disabled() -> None:
    assert client.get("/api/exams/v1/student/runs/r1/accommodations").status_code == 503


def test_me_route_is_503_when_exams_disabled_even_without_auth() -> None:
    """Regression test: `exams_student.py` and `exams_me.py` previously had
    no ROUTER-level `require_exams_enabled` dependency, relying solely on
    `get_current_exam_user`'s internal ordering — which FastAPI's
    `HTTPBearer` security dependency can pre-empt with a 403 'Not
    authenticated' for a caller with no Authorization header at all,
    before the feature-flag check ever runs. Both routers now declare it
    at the router level, exactly like every other exam router."""
    assert client.get("/api/exams/v1/me").status_code == 503


def test_faculty_create_definition_is_503_when_exams_disabled() -> None:
    response = client.post("/api/exams/v1/faculty/definitions", json={"title": "Midterm"})
    assert response.status_code == 503


def test_kt_and_generation_routes_are_503_when_exams_disabled() -> None:
    assert client.post("/api/exams/v1/faculty/kt/sources/from-url", json={"url": "https://example.com/x"}).status_code == 503
    assert client.post("/api/exams/v1/faculty/blueprints", json={"exam_definition_id": "d1", "title": "t", "slots": []}).status_code == 503


def test_judge_routes_are_503_when_exams_disabled() -> None:
    assert client.post(
        "/api/exams/v1/student/judge/public-run",
        json={"run_id": "r1", "question_id": "q1", "language_key": "python3.11", "source_code": "x"},
    ).status_code == 503


def test_proctoring_routes_are_503_when_exams_disabled() -> None:
    assert client.post(
        "/api/exams/v1/student/proctoring/runs/r1/observations",
        json={"kind": "page_hidden", "client_event_id": "e1", "client_timestamp": "2026-06-01T10:00:00Z"},
    ).status_code == 503
    assert client.get("/api/exams/v1/faculty/proctoring/runs/r1/uids/u1/incidents").status_code == 503


def test_live_ticket_route_is_503_when_exams_disabled() -> None:
    """The WebSocket route itself can't be exercised without a real
    Firestore-backed ticket (see docs/adr/0012 'Known limitations') — this
    only proves the feature flag gate, matching every other route here."""
    assert client.post("/api/exams/v1/live/ticket", json={"run_id": "r1"}).status_code == 503


def test_gradebook_routes_are_503_when_exams_disabled() -> None:
    assert client.get("/api/exams/v1/student/gradebook/runs/r1/results").status_code == 503
    assert client.get("/api/exams/v1/student/gradebook/runs/r1/feedback").status_code == 503
    assert client.post(
        "/api/exams/v1/student/gradebook/runs/r1/questions/q1/appeals", json={"student_statement": "x"}
    ).status_code == 503
    assert client.get("/api/exams/v1/faculty/gradebook/runs/r1/rows").status_code == 503
    assert client.get("/api/exams/v1/faculty/gradebook/runs/r1/counts").status_code == 503
    assert client.post("/api/exams/v1/faculty/gradebook/runs/r1/release", json={}).status_code == 503
    assert client.post(
        "/api/exams/v1/faculty/gradebook/runs/r1/questions/q1/defective",
        json={"resolution": "full_credit_all", "reason": "x"},
    ).status_code == 503
    assert client.post(
        "/api/exams/v1/faculty/gradebook/runs/r1/questions/q1/similarity", json={}
    ).status_code == 503
    assert client.post("/api/exams/v1/faculty/gradebook/runs/r1/export", json={"format": "csv"}).status_code == 503


def test_lockdown_routes_are_503_when_exams_disabled() -> None:
    assert client.get("/api/exams/v1/faculty/lockdown/runs/r1/profile").status_code == 503
    assert client.post("/api/exams/v1/faculty/lockdown/runs/r1/profile", json={"enabled": True}).status_code == 503
    assert client.get("/api/exams/v1/faculty/lockdown/runs/r1/admission-log").status_code == 503


def test_root_and_health_endpoints_work_even_when_disabled() -> None:
    """The API itself must stay up (only mutating routes are gated) so
    operators can still see health/readiness while the module is off."""
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["exams_enabled"] is False

    live = client.get("/health/live")
    assert live.status_code == 200
