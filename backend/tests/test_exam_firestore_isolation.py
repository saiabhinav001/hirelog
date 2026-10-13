"""Exam data can live in its own Firebase project so exams never use the live site's daily Firestore quota."""

from __future__ import annotations

import importlib
import pkgutil
from types import SimpleNamespace

import pytest

import app.api.dependencies as deps
import app.api.routes as routes_pkg
from app.core import firebase as fb


class _Sentinel:
    def __init__(self, name: str):
        self.name = name


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fb, "_exam_db", None)
    monkeypatch.setattr(fb.settings, "FIREBASE_PROJECT_ID", "main-project")
    monkeypatch.setattr(fb.settings, "EXAM_FIREBASE_PROJECT_ID", None)
    monkeypatch.setattr(fb.settings, "EXAM_FIREBASE_SERVICE_ACCOUNT_JSON", None)
    monkeypatch.setattr(fb.settings, "EXAM_FIREBASE_SERVICE_ACCOUNT_PATH", None)
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)


def test_exam_client_is_the_shared_one_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = _Sentinel("shared")
    monkeypatch.setattr(fb, "get_db", lambda: shared)
    assert fb.get_exam_db() is shared


def test_same_project_id_means_no_separation(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = _Sentinel("shared")
    monkeypatch.setattr(fb, "get_db", lambda: shared)
    monkeypatch.setattr(fb.settings, "EXAM_FIREBASE_PROJECT_ID", "main-project")
    assert fb.settings.exam_data_is_separate() is False
    assert fb.get_exam_db() is shared


def test_separate_project_gets_its_own_named_app_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = _Sentinel("shared")
    created: list[tuple] = []

    def fake_get_app(name: str):
        raise ValueError(name)

    def fake_initialize_app(credential, options, name):
        created.append((credential, options, name))
        return SimpleNamespace(name=name)

    monkeypatch.setattr(fb, "get_db", lambda: shared)
    monkeypatch.setattr(fb, "_build_exam_credential", lambda: "exam-credential")
    monkeypatch.setattr(fb.firebase_admin, "get_app", fake_get_app)
    monkeypatch.setattr(fb.firebase_admin, "initialize_app", fake_initialize_app)
    monkeypatch.setattr(fb.firestore, "client", lambda app: _Sentinel(f"firestore:{app.name}"))
    monkeypatch.setattr(fb.settings, "EXAM_FIREBASE_PROJECT_ID", "exam-data-project")

    first = fb.get_exam_db()
    second = fb.get_exam_db()

    assert first is not shared
    assert first is second
    assert first.name == "firestore:exams"
    assert created == [("exam-credential", {"projectId": "exam-data-project"}, "exams")]


def test_missing_exam_credentials_fail_loudly() -> None:
    with pytest.raises(ValueError, match="EXAM_FIREBASE_SERVICE_ACCOUNT_JSON"):
        fb._build_exam_credential()


def test_emulator_reuses_the_main_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    monkeypatch.setattr(fb, "_build_credential", lambda: "main-credential")
    assert fb._build_exam_credential() == "main-credential"


def _exam_module_names() -> list[str]:
    names = ["app.api.exams_dependencies", "app.api.exams_scope", "app.exam_main"]
    names += [m.name for m in pkgutil.iter_modules(routes_pkg.__path__, "app.api.routes.") if m.name.rsplit(".", 1)[-1].startswith("exams_")]
    return names


def test_every_exam_module_uses_the_exam_client() -> None:
    wrong = [name for name in _exam_module_names() if getattr(importlib.import_module(name), "db", fb.exam_db) is not fb.exam_db]
    assert not wrong, f"these exam modules still use the live site's Firestore client: {wrong}"


def test_legacy_modules_keep_the_shared_client() -> None:
    for name in ("users", "experiences", "search", "dashboard", "practice"):
        assert importlib.import_module(f"app.api.routes.{name}").db is fb.db


def _decoded_token() -> dict:
    return {"uid": "u1", "email": "Student@Example.edu", "name": "Student", "email_verified": True, "auth_time": 123,
            "firebase": {"sign_in_provider": "google.com"}, "exam_authz_version": 2, "exam_institution_id": "inst-1"}


def test_isolated_identity_never_touches_the_live_users_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("the live site's users collection must not be used")

    monkeypatch.setattr(deps, "_verify_firebase_token", lambda token: _decoded_token())
    monkeypatch.setattr(deps, "_get_or_create_user", forbidden)
    monkeypatch.setattr(deps.settings, "EXAM_FIREBASE_PROJECT_ID", "exam-data-project")

    user = deps.get_current_user_uncached(SimpleNamespace(credentials="token"))

    assert user["uid"] == "u1"
    assert user["token_email"] == "student@example.edu"
    assert user["email_verified"] is True
    assert user["sign_in_provider"] == "google.com"
    assert user["exam_authz_version"] == 2 and user["exam_institution_id"] == "inst-1"
    assert "role" not in user


def test_shared_project_identity_no_longer_reads_the_profile_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exam identity is token-only in every deployment: the per-request users/{uid} read was pure cost (no exam
    decision uses a stored profile field), so the shared-project path no longer makes it."""
    def forbidden(*args, **kwargs):
        raise AssertionError("an exam request must not read the legacy users collection")

    monkeypatch.setattr(deps, "_verify_firebase_token", lambda token: _decoded_token())
    monkeypatch.setattr(deps, "_get_or_create_user", forbidden)
    monkeypatch.setattr(deps.settings, "EXAM_FIREBASE_PROJECT_ID", None)

    user = deps.get_current_user_uncached(SimpleNamespace(credentials="token"))

    assert user["uid"] == "u1"
    assert user["email"] == "Student@Example.edu"
    assert user["token_email"] == "student@example.edu"
    assert "role" not in user
