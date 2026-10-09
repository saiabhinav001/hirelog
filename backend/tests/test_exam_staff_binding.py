"""Staff bootstrap binding + authorization audit (Prompt 1, ADR 0017).

Uses a small in-memory Firestore stand-in. It proves the first-writer-wins
binding LOGIC (including the create-vs-existing race path); true multi-writer
atomicity of `DocumentReference.create()` is a Firestore guarantee that needs
an emulator/staging run to demonstrate (see docs/exams/requirements-traceability.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import AlreadyExists

from app.domain.authz import staff_binding
from app.domain.authz.staff_binding import (
    AUTHZ_AUDIT_COLLECTION,
    STAFF_BINDINGS_COLLECTION,
    StaffBinding,
    StaffBindingService,
    binding_doc_id,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Snap:
    def __init__(self, data: dict | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Ref:
    def __init__(self, store: dict, key: str) -> None:
        self._store, self._key = store, key

    def get(self):
        return _Snap(self._store.get(self._key))

    def create(self, data: dict) -> None:
        if self._key in self._store:
            raise AlreadyExists("document already exists")
        self._store[self._key] = data

    def set(self, data: dict) -> None:
        self._store[self._key] = data


class _Collection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def document(self, doc_id: str) -> _Ref:
        return _Ref(self._store, doc_id)


class _FakeDb:
    def __init__(self) -> None:
        self.stores: dict[str, dict] = {}

    def collection(self, name: str) -> _Collection:
        return _Collection(self.stores.setdefault(name, {}))


@pytest.fixture(autouse=True)
def _reset_conflict_throttle():
    staff_binding._conflict_audit_seen.clear()
    yield
    staff_binding._conflict_audit_seen.clear()


def _service(db: _FakeDb) -> StaffBindingService:
    return StaffBindingService(db, now_fn=lambda: NOW)


def _audit_kinds(db: _FakeDb) -> list[str]:
    return sorted(event["kind"] for event in db.stores.get(AUTHZ_AUDIT_COLLECTION, {}).values())


def test_binding_doc_id_is_a_hash_of_the_normalized_email_not_the_email() -> None:
    assert binding_doc_id("Prof@College.edu ") == binding_doc_id("prof@college.edu")
    assert "@" not in binding_doc_id("prof@college.edu")
    assert len(binding_doc_id("prof@college.edu")) == 64


def test_first_verified_sign_in_binds_the_email_to_the_uid_and_audits_it() -> None:
    db = _FakeDb()
    outcome = _service(db).ensure_bound(
        email="Prof@College.edu", uid="uid-1", roles=["teacher"], sign_in_provider="google.com"
    )

    assert outcome == "bound"
    stored = db.stores[STAFF_BINDINGS_COLLECTION][binding_doc_id("prof@college.edu")]
    binding = StaffBinding.model_validate(stored)
    assert binding.uid == "uid-1" and binding.email == "prof@college.edu"
    assert binding.roles_at_bind == ["teacher"] and binding.sign_in_provider == "google.com"
    assert _audit_kinds(db) == ["staff_bound"]
    event = next(iter(db.stores[AUTHZ_AUDIT_COLLECTION].values()))
    assert event["uid"] == "uid-1" and event["detail"]["roles"] == ["teacher"]


def test_same_uid_again_is_a_no_op_without_new_audit() -> None:
    db = _FakeDb()
    service = _service(db)
    service.ensure_bound(email="prof@college.edu", uid="uid-1", roles=["teacher"])

    assert service.ensure_bound(email="prof@college.edu", uid="uid-1", roles=["teacher"]) == "already_bound"
    assert _audit_kinds(db) == ["staff_bound"]


def test_a_different_uid_presenting_a_bound_email_is_a_conflict_and_is_audited() -> None:
    db = _FakeDb()
    service = _service(db)
    service.ensure_bound(email="prof@college.edu", uid="uid-1", roles=["teacher"])

    assert service.ensure_bound(email="prof@college.edu", uid="uid-2", roles=["teacher"]) == "conflict"

    assert _audit_kinds(db) == ["staff_binding_conflict", "staff_bound"]
    conflict = next(e for e in db.stores[AUTHZ_AUDIT_COLLECTION].values() if e["kind"] == "staff_binding_conflict")
    assert conflict["uid"] == "uid-2" and conflict["detail"]["bound_uid"] == "uid-1"
    # The original binding is never overwritten by the conflicting uid.
    stored = db.stores[STAFF_BINDINGS_COLLECTION][binding_doc_id("prof@college.edu")]
    assert stored["uid"] == "uid-1"


def test_repeated_conflicts_from_the_same_uid_do_not_flood_the_audit_log() -> None:
    db = _FakeDb()
    service = _service(db)
    service.ensure_bound(email="prof@college.edu", uid="uid-1", roles=["teacher"])
    for _ in range(5):
        assert service.ensure_bound(email="prof@college.edu", uid="uid-2", roles=["teacher"]) == "conflict"

    assert _audit_kinds(db).count("staff_binding_conflict") == 1


def test_two_different_staff_emails_bind_independently() -> None:
    db = _FakeDb()
    service = _service(db)
    assert service.ensure_bound(email="a@college.edu", uid="uid-1", roles=["super_admin"]) == "bound"
    assert service.ensure_bound(email="b@college.edu", uid="uid-2", roles=["teacher"]) == "bound"
    assert len(db.stores[STAFF_BINDINGS_COLLECTION]) == 2


def test_losing_the_create_race_resolves_against_the_winning_binding() -> None:
    """Another request bound the e-mail between our read and our create()."""
    db = _FakeDb()
    service = _service(db)
    winner = StaffBinding(email="prof@college.edu", uid="uid-winner", roles_at_bind=["teacher"], bound_at=NOW)

    reads = iter([None, winner])
    service._bindings.get = lambda _email: next(reads)  # type: ignore[method-assign]
    service._bindings.create_if_absent = lambda _binding: False  # type: ignore[method-assign]

    assert service.ensure_bound(email="prof@college.edu", uid="uid-loser", roles=["teacher"]) == "conflict"


def test_losing_the_create_race_to_the_same_uid_is_already_bound() -> None:
    db = _FakeDb()
    service = _service(db)
    same = StaffBinding(email="prof@college.edu", uid="uid-1", roles_at_bind=["teacher"], bound_at=NOW)

    reads = iter([None, same])
    service._bindings.get = lambda _email: next(reads)  # type: ignore[method-assign]
    service._bindings.create_if_absent = lambda _binding: False  # type: ignore[method-assign]

    assert service.ensure_bound(email="prof@college.edu", uid="uid-1", roles=["teacher"]) == "already_bound"
    assert _audit_kinds(db) == []
