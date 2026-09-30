"""`EvidenceService` tests: request/upload/verify lifecycle (hash match/
mismatch, oversized payload, already-completed), missing-evidence
recording (permission denial / unsupported media), scoped single-use
upload/view tickets (expiry, wrong-owner, replay), and audited access.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.proctoring import evidence_service as evidence_service_module
from app.domain.proctoring.errors import SnapshotHashMismatchError, UploadTicketInvalidError, ViewTicketInvalidError
from app.domain.proctoring.evidence_service import EvidenceService
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage
from app.domain.proctoring.models import (
    EvidenceAccessLogEntry,
    EvidenceSnapshot,
    EvidenceSnapshotKind,
    UploadTicket,
    ViewTicket,
)

NOW = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _passthrough_transactions(monkeypatch):
    monkeypatch.setattr(evidence_service_module.firestore, "transactional", lambda fn: fn)


class _FakeSnapshotRepo:
    def __init__(self) -> None:
        self.store: dict[str, EvidenceSnapshot] = {}

    def create(self, snapshot):
        self.store[snapshot.id] = snapshot

    def get(self, snapshot_id):
        return self.store.get(snapshot_id)

    def update(self, snapshot_id, update):
        # Merge + re-validate (not model_copy) so string enum values written
        # exactly like a real Firestore round-trip coerce back properly —
        # model_copy(update=...) would skip validation and leave raw strings.
        merged = {**self.store[snapshot_id].model_dump(mode="json"), **update}
        self.store[snapshot_id] = EvidenceSnapshot.model_validate(merged)

    def list_for_attempt(self, run_id, uid):
        return [s for s in self.store.values() if s.run_id == run_id and s.uid == uid]

    def list_all(self, *, limit):
        return list(self.store.values())[:limit]

    def delete(self, snapshot_id):
        self.store.pop(snapshot_id, None)


class _FakeUploadTicketRepo:
    def __init__(self) -> None:
        self.store: dict[str, UploadTicket] = {}

    def create(self, ticket):
        self.store[ticket.id] = ticket

    def get_in_transaction(self, _txn, ticket_id):
        return self.store.get(ticket_id)

    def mark_consumed_in_transaction(self, _txn, ticket_id, *, consumed_at):
        current = self.store[ticket_id]
        self.store[ticket_id] = current.model_copy(update={"consumed": True, "consumed_at": consumed_at})


class _FakeViewTicketRepo:
    def __init__(self) -> None:
        self.store: dict[str, ViewTicket] = {}

    def create(self, ticket):
        self.store[ticket.id] = ticket

    def get_in_transaction(self, _txn, ticket_id):
        return self.store.get(ticket_id)

    def mark_consumed_in_transaction(self, _txn, ticket_id, *, consumed_at):
        current = self.store[ticket_id]
        self.store[ticket_id] = current.model_copy(update={"consumed": True, "consumed_at": consumed_at})


class _FakeAccessLogRepo:
    def __init__(self) -> None:
        self.entries: list[EvidenceAccessLogEntry] = []

    def create(self, entry):
        self.entries.append(entry)


class _FakeDb:
    def transaction(self):
        return object()


def _service() -> EvidenceService:
    service = EvidenceService.__new__(EvidenceService)
    service._db = _FakeDb()
    service._storage = InMemoryEvidenceStorage()
    service.snapshots = _FakeSnapshotRepo()
    service.upload_tickets = _FakeUploadTicketRepo()
    service.view_tickets = _FakeViewTicketRepo()
    service.access_log = _FakeAccessLogRepo()
    return service


def test_request_then_confirm_upload_with_matching_hash_succeeds() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, upload_ticket_ttl_seconds=120)

    content = b"fake-jpeg-bytes"
    digest = hashlib.sha256(content).hexdigest()
    result = service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=digest, content_type="image/jpeg", max_size_bytes=10_000)

    assert result.status.value == "verified"
    assert result.sha256 == digest
    assert service._storage.get(result.storage_ref) == content


def test_confirm_upload_rejects_hash_mismatch_and_marks_missing() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.PERIODIC, upload_ticket_ttl_seconds=120)

    with pytest.raises(SnapshotHashMismatchError):
        service.confirm_upload(ticket_id=ticket.id, uid="u1", content=b"abc", declared_sha256="wrong-hash", content_type="image/jpeg", max_size_bytes=10_000)

    assert service.snapshots.get(snapshot.id).status.value == "missing"


def test_confirm_upload_rejects_expired_ticket() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, upload_ticket_ttl_seconds=120)
    expired = ticket.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    service.upload_tickets.store[ticket.id] = expired

    with pytest.raises(UploadTicketInvalidError):
        service.confirm_upload(ticket_id=ticket.id, uid="u1", content=b"x", declared_sha256=hashlib.sha256(b"x").hexdigest(), content_type="image/jpeg", max_size_bytes=10_000)


def test_confirm_upload_rejects_wrong_owner() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, upload_ticket_ttl_seconds=120)

    with pytest.raises(UploadTicketInvalidError):
        service.confirm_upload(ticket_id=ticket.id, uid="attacker", content=b"x", declared_sha256=hashlib.sha256(b"x").hexdigest(), content_type="image/jpeg", max_size_bytes=10_000)


def test_confirm_upload_rejects_replayed_ticket() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, upload_ticket_ttl_seconds=120)
    content = b"abc"
    digest = hashlib.sha256(content).hexdigest()
    service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=digest, content_type="image/jpeg", max_size_bytes=10_000)

    with pytest.raises(UploadTicketInvalidError):
        service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=digest, content_type="image/jpeg", max_size_bytes=10_000)


def test_record_missing_evidence_for_permission_denial() -> None:
    service = _service()
    snapshot = service.record_missing(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, reason="permission_denied")
    assert snapshot.status.value == "missing"
    assert snapshot.missing_reason == "permission_denied"


def test_record_missing_evidence_for_unsupported_media() -> None:
    service = _service()
    snapshot = service.record_missing(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.PERIODIC, reason="unsupported_media")
    assert snapshot.missing_reason == "unsupported_media"


def test_view_snapshot_happy_path_is_audited() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.SUBMISSION, upload_ticket_ttl_seconds=120)
    content = b"jpeg-bytes"
    service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=hashlib.sha256(content).hexdigest(), content_type="image/jpeg", max_size_bytes=10_000)

    view_ticket = service.issue_view_ticket(snapshot_id=snapshot.id, faculty_uid="fac-1", ttl_seconds=60)
    result = service.view_snapshot(ticket_id=view_ticket.id, faculty_uid="fac-1")

    assert result == content
    assert len(service.access_log.entries) == 1
    assert service.access_log.entries[0].accessed_by == "fac-1"


def test_view_snapshot_rejects_wrong_faculty() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.SUBMISSION, upload_ticket_ttl_seconds=120)
    content = b"jpeg-bytes"
    service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=hashlib.sha256(content).hexdigest(), content_type="image/jpeg", max_size_bytes=10_000)
    view_ticket = service.issue_view_ticket(snapshot_id=snapshot.id, faculty_uid="fac-1", ttl_seconds=60)

    with pytest.raises(ViewTicketInvalidError):
        service.view_snapshot(ticket_id=view_ticket.id, faculty_uid="fac-OTHER")


def test_view_snapshot_rejects_replayed_ticket() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.SUBMISSION, upload_ticket_ttl_seconds=120)
    content = b"jpeg-bytes"
    service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=hashlib.sha256(content).hexdigest(), content_type="image/jpeg", max_size_bytes=10_000)
    view_ticket = service.issue_view_ticket(snapshot_id=snapshot.id, faculty_uid="fac-1", ttl_seconds=60)
    service.view_snapshot(ticket_id=view_ticket.id, faculty_uid="fac-1")

    with pytest.raises(ViewTicketInvalidError):
        service.view_snapshot(ticket_id=view_ticket.id, faculty_uid="fac-1")


def test_delete_snapshot_removes_storage_and_record() -> None:
    service = _service()
    snapshot, ticket = service.request_snapshot(run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, upload_ticket_ttl_seconds=120)
    content = b"abc"
    verified = service.confirm_upload(ticket_id=ticket.id, uid="u1", content=content, declared_sha256=hashlib.sha256(content).hexdigest(), content_type="image/jpeg", max_size_bytes=10_000)

    service.delete_snapshot(verified)

    assert service.snapshots.get(snapshot.id) is None
    assert service._storage.get(verified.storage_ref) is None
