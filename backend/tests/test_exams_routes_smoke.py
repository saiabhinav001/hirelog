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


def test_faculty_create_definition_is_503_when_exams_disabled() -> None:
    response = client.post("/api/exams/v1/faculty/definitions", json={"title": "Midterm"})
    assert response.status_code == 503


def test_root_and_health_endpoints_work_even_when_disabled() -> None:
    """The API itself must stay up (only mutating routes are gated) so
    operators can still see health/readiness while the module is off."""
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["exams_enabled"] is False

    live = client.get("/health/live")
    assert live.status_code == 200
