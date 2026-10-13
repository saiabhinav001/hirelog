from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api import dependencies as deps


def test_require_contributor_allows_placement_cell() -> None:
    user = {"role": "placement_cell"}
    assert deps.require_contributor(user) == user


def test_require_contributor_blocks_viewer() -> None:
    with pytest.raises(HTTPException) as exc:
        deps.require_contributor({"role": "viewer"})
    assert exc.value.status_code == 403


def test_require_placement_cell_blocks_non_admin() -> None:
    with pytest.raises(HTTPException) as exc:
        deps.require_placement_cell({"role": "contributor"})
    assert exc.value.status_code == 403


def test_resolve_role_promotes_placement_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps.settings, "PLACEMENT_CELL_EMAILS", "pcell@example.edu")
    assert deps._resolve_role("viewer", "pcell@example.edu") == "placement_cell"


def test_get_current_user_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"verify": 0}

    def fake_verify(_: str) -> dict:
        calls["verify"] += 1
        return {
            "uid": "u-1",
            "email": "demo@example.edu",
            "name": "Demo",
            "exp": int(time.time()) + 1800,
        }

    def fake_get_or_create(uid: str, email: str | None, name: str | None = None) -> dict:
        return {"uid": uid, "email": email or "", "name": name or "", "role": "viewer"}

    monkeypatch.setattr(deps.firebase_auth, "verify_id_token", fake_verify)
    monkeypatch.setattr(deps, "_get_or_create_user", fake_get_or_create)

    deps._auth_cache.clear()
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token-1")

    first = deps.get_current_user(creds)
    second = deps.get_current_user(creds)

    assert first["uid"] == "u-1"
    assert second["uid"] == "u-1"
    assert calls["verify"] == 1


def test_get_current_user_rejects_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify(_: str) -> dict:
        raise RuntimeError("bad token")

    monkeypatch.setattr(deps.firebase_auth, "verify_id_token", fake_verify)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid")

    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(creds)

    assert exc.value.status_code == 401


def test_get_current_user_uncached_verifies_every_call_and_never_reads_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exam routes take identity from the verified token alone: no caching layer to serve a stale claim, and no read
    (or creation) of the legacy `users/{uid}` document, which cost a billable read on every exam request."""
    calls = {"verify": 0, "lookup": 0}

    def fake_verify(_: str) -> dict:
        calls["verify"] += 1
        return {
            "uid": "u-2", "email": "Demo2@Example.edu", "name": "Demo2", "exp": int(time.time()) + 1800,
            "email_verified": True, "auth_time": 1700000000, "exam_authz_version": 3, "exam_institution_id": "inst-1",
            "firebase": {"sign_in_provider": "custom"},
        }

    def fake_get_or_create(uid: str, email: str | None, name: str | None = None) -> dict:
        calls["lookup"] += 1
        return {"uid": uid, "role": "placement_cell"}

    monkeypatch.setattr(deps.firebase_auth, "verify_id_token", fake_verify)
    monkeypatch.setattr(deps, "_get_or_create_user", fake_get_or_create)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token-2")

    first = deps.get_current_user_uncached(creds)
    second = deps.get_current_user_uncached(creds)

    assert calls["verify"] == 2
    assert calls["lookup"] == 0
    assert first == second
    assert first["uid"] == "u-2"
    assert first["token_email"] == "demo2@example.edu"
    assert first["email_verified"] is True
    assert first["exam_authz_version"] == 3
    assert first["exam_institution_id"] == "inst-1"
    assert first["sign_in_provider"] == "custom"
    assert "role" not in first  # authority never comes from a stored profile field


def test_legacy_get_current_user_still_verifies_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compatibility check: the legacy cached dependency keeps working exactly
    as before for existing (non-exam) routes."""
    calls = {"verify": 0}

    def fake_verify(_: str) -> dict:
        calls["verify"] += 1
        return {"uid": "u-3", "email": "legacy@example.edu", "name": "Legacy", "exp": int(time.time()) + 1800}

    def fake_get_or_create(uid: str, email: str | None, name: str | None = None) -> dict:
        return {"uid": uid, "email": email or "", "name": name or "", "role": "viewer"}

    monkeypatch.setattr(deps.firebase_auth, "verify_id_token", fake_verify)
    monkeypatch.setattr(deps, "_get_or_create_user", fake_get_or_create)
    deps._auth_cache.clear()

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token-3")
    first = deps.get_current_user(creds)
    second = deps.get_current_user(creds)

    assert first["role"] == second["role"] == "viewer"
    assert calls["verify"] == 1
