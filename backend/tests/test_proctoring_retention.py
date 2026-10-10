"""`RetentionSweeper` tests: deletes evidence (storage + record together)
once past the configured retention window; leaves recent evidence alone."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app.domain.proctoring.evidence_service import EvidenceService
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage
from app.domain.proctoring.models import EvidenceSnapshot, EvidenceSnapshotKind
from app.domain.proctoring.retention import RetentionSweeper

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _FakeSnapshotRepo:
    def __init__(self) -> None:
        self.store: dict[str, EvidenceSnapshot] = {}

    def create(self, snapshot):
        self.store[snapshot.id] = snapshot

    def get(self, snapshot_id):
        return self.store.get(snapshot_id)

    def update(self, snapshot_id, update):
        self.store[snapshot_id] = self.store[snapshot_id].model_copy(update=update)

    def list_all(self, *, limit):
        return list(self.store.values())[:limit]

    def list_oldest(self, *, limit, after_requested_at=None):
        return sorted(self.store.values(), key=lambda s: s.requested_at)[:limit]

    def delete(self, snapshot_id):
        self.store.pop(snapshot_id, None)


class _FakeUploadTicketRepo:
    def create(self, ticket) -> None: ...
    def get_in_transaction(self, _txn, ticket_id): return None
    def mark_consumed_in_transaction(self, _txn, ticket_id, *, consumed_at) -> None: ...


class _FakeViewTicketRepo:
    def create(self, ticket) -> None: ...
    def get_in_transaction(self, _txn, ticket_id): return None
    def mark_consumed_in_transaction(self, _txn, ticket_id, *, consumed_at) -> None: ...


class _FakeAccessLogRepo:
    def create(self, entry) -> None: ...


class _FakeDb:
    def transaction(self):
        return object()


def _service_with_snapshot(*, uploaded_at) -> tuple[EvidenceService, EvidenceSnapshot]:
    service = EvidenceService.__new__(EvidenceService)
    service._db = _FakeDb()
    storage = InMemoryEvidenceStorage()
    service._storage = storage
    service.snapshots = _FakeSnapshotRepo()
    service.upload_tickets = _FakeUploadTicketRepo()
    service.view_tickets = _FakeViewTicketRepo()
    service.access_log = _FakeAccessLogRepo()

    content = b"jpeg-bytes"
    storage_ref = "exam-evidence/run-1/u1/snap-1"
    storage.put(storage_ref, content, content_type="image/jpeg")
    snapshot = EvidenceSnapshot(
        id="snap-1", run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, status="verified",
        requested_at=uploaded_at, uploaded_at=uploaded_at, storage_ref=storage_ref,
        sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content), content_type="image/jpeg",
    )
    service.snapshots.create(snapshot)
    return service, snapshot


def test_sweep_deletes_snapshot_past_retention() -> None:
    service, snapshot = _service_with_snapshot(uploaded_at=NOW - timedelta(days=91))
    sweeper = RetentionSweeper(service)

    deleted = sweeper.sweep_once(retention_days=90, now=NOW)

    assert deleted == 1
    assert service.snapshots.get(snapshot.id) is None
    assert service._storage.get(snapshot.storage_ref) is None


def test_sweep_leaves_recent_snapshot_alone() -> None:
    service, snapshot = _service_with_snapshot(uploaded_at=NOW - timedelta(days=5))
    sweeper = RetentionSweeper(service)

    deleted = sweeper.sweep_once(retention_days=90, now=NOW)

    assert deleted == 0
    assert service.snapshots.get(snapshot.id) is not None
    assert service._storage.get(snapshot.storage_ref) is not None
