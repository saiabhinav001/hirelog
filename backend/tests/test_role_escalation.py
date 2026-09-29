"""Regression tests for the Prompt 1 role-escalation fix.

See docs/adr/0007-role-escalation-fix.md for the confirmed baseline
vulnerability this guards against: `POST /api/users` used to accept a
client-supplied `role` and keep the higher of (client role, stored role),
letting any authenticated viewer self-escalate to `placement_cell`.
"""

from __future__ import annotations

import pytest

from app.api.routes import users as users_route
from app.models.schemas import UserCreate


class _FakeSnapshot:
    def __init__(self, data: dict | None, doc_id: str = "fake-uid"):
        self._data = data
        self.exists = data is not None
        self.id = doc_id

    def to_dict(self):
        return dict(self._data) if self._data else {}


class _FakeDocRef:
    def __init__(self, initial: dict | None):
        self._data = dict(initial) if initial else None
        self.set_calls: list[dict] = []

    def get(self):
        return _FakeSnapshot(self._data)

    def set(self, data: dict, merge: bool = False):
        self.set_calls.append(data)
        self._data = {**(self._data or {}), **data}


class _FakeUsersCollection:
    def __init__(self, doc_ref: _FakeDocRef):
        self._doc_ref = doc_ref

    def document(self, uid: str):
        return self._doc_ref


class _FakeDB:
    def __init__(self, doc_ref: _FakeDocRef):
        self._doc_ref = doc_ref

    def collection(self, name: str):
        assert name == "users"
        return _FakeUsersCollection(self._doc_ref)


def test_user_create_schema_has_no_role_field() -> None:
    """DTO allowlist contract: role/authority can never be supplied by a client."""
    assert "role" not in UserCreate.model_fields


def test_new_viewer_cannot_grant_placement_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    doc_ref = _FakeDocRef(initial=None)
    monkeypatch.setattr(users_route, "db", _FakeDB(doc_ref))

    # get_current_user (mocked here) resolved this caller as a plain viewer —
    # this is the only source of truth the route is allowed to read.
    server_user = {"uid": "victim-1", "email": "victim@example.edu", "role": "viewer"}

    # Even if a crafted request smuggles role="placement_cell" as an extra
    # field, UserCreate has no such field to bind it to.
    payload = UserCreate.model_validate({"name": "Victim", "role": "placement_cell"})

    result = users_route.create_or_update_user(payload, server_user)

    assert result["role"] == "viewer"
    assert doc_ref.set_calls[-1]["role"] == "viewer"


def test_arbitrary_authority_fields_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    doc_ref = _FakeDocRef(initial=None)
    monkeypatch.setattr(users_route, "db", _FakeDB(doc_ref))
    server_user = {"uid": "victim-2", "email": "victim2@example.edu", "role": "viewer"}

    payload = UserCreate.model_validate(
        {
            "name": "Victim Two",
            "is_admin": True,
            "authority": "faculty",
            "institution_role": "proctor",
        }
    )
    result = users_route.create_or_update_user(payload, server_user)

    assert result["role"] == "viewer"
    persisted = doc_ref.set_calls[-1]
    assert "is_admin" not in persisted
    assert "authority" not in persisted
    assert "institution_role" not in persisted


def test_legitimate_privileged_user_keeps_role_on_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user genuinely promoted server-side (email allowlist) must not be
    regressed to viewer by simply re-calling this endpoint."""
    doc_ref = _FakeDocRef(initial={"uid": "staff-1", "role": "placement_cell", "name": "Staff One"})
    monkeypatch.setattr(users_route, "db", _FakeDB(doc_ref))
    server_user = {"uid": "staff-1", "email": "staff@example.edu", "role": "placement_cell"}

    payload = UserCreate.model_validate({"name": "Staff One"})
    result = users_route.create_or_update_user(payload, server_user)

    assert result["role"] == "placement_cell"


def test_viewer_role_unaffected_even_if_stored_doc_was_tampered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a crafted direct Firestore write that got a doc into a bad
    state (rules should already block this — see firestore.rules — but the
    API must be independently safe since the Admin SDK bypasses rules)."""
    doc_ref = _FakeDocRef(initial={"uid": "victim-3", "role": "viewer", "name": "Victim Three"})
    monkeypatch.setattr(users_route, "db", _FakeDB(doc_ref))
    server_user = {"uid": "victim-3", "email": "victim3@example.edu", "role": "viewer"}

    payload = UserCreate.model_validate({"name": "Victim Three", "role": "placement_cell"})
    result = users_route.create_or_update_user(payload, server_user)

    assert result["role"] == "viewer"
