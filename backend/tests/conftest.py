from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Keep backend/app importable no matter where pytest is invoked from.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _reset_exam_caches() -> None:
    from app.api import exams_scope
    from app.domain.exams import repository as exam_repository
    from app.domain.generation import repository as generation_repository
    from app.domain.institution.repository import membership_cache

    exams_scope.reset_caches()
    exam_repository.reset_read_caches()
    generation_repository.reset_read_caches()
    membership_cache.clear()


@pytest.fixture
def fake_db(monkeypatch):
    """In-memory Firestore wired in place of the real client for the whole app."""
    from firebase_admin import firestore as firestore_module

    import app.core.firebase as firebase_module
    from tests.support.fake_firestore import FakeFirestore, fake_transactional

    db = FakeFirestore()
    monkeypatch.setattr(firebase_module, "_db", db)
    monkeypatch.setattr(firestore_module, "transactional", fake_transactional)
    _reset_exam_caches()
    yield db
    _reset_exam_caches()


@pytest.fixture
def exam_world(fake_db, monkeypatch):
    """Exam API client + token issuer + scenario helpers, exams enabled and a
    verified platform operator configured."""
    import firebase_admin.auth as firebase_auth_module
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.exam_main import app
    from tests.support.exam_world import OPERATOR_EMAIL, TokenIssuer, World

    tokens = TokenIssuer()
    monkeypatch.setattr(firebase_auth_module, "verify_id_token", tokens.verify)
    monkeypatch.setattr(settings, "EXAMS_ENABLED", True)
    monkeypatch.setattr(settings, "EXAM_FACULTY_EMAILS", OPERATOR_EMAIL)
    monkeypatch.setattr(settings, "EXAM_MEMBERSHIP_CACHE_TTL_SECONDS", 30.0)
    return World(client=TestClient(app), db=fake_db, tokens=tokens)


@pytest.fixture
def exam(exam_world):
    """College A: faculty, admin, two students, one released run (S1 admitted)."""
    from tests.support.exam_scenarios import seed_exam_scenario

    return seed_exam_scenario(exam_world)
