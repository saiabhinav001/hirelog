"""Evidence snapshot service — request/upload/verify lifecycle, scoped
upload/view tickets, and access audit. Submission acceptance never depends
on any of this succeeding (contract: enforced by construction — nothing in
`app.domain.exams.student_service` calls into this module at all; the
finalization/submit path is completely independent of evidence capture).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from firebase_admin import firestore

from app.domain.proctoring.decisions import decide_confirm_snapshot_upload, detect_image_type
from app.domain.proctoring.errors import (
    EvidenceStorageUnavailableError,
    SnapshotHashMismatchError,
    UploadTicketInvalidError,
    ViewTicketInvalidError,
)
from app.domain.proctoring.evidence_storage import EvidenceStorage
from app.domain.proctoring.models import (
    EvidenceAccessLogEntry,
    EvidenceSnapshot,
    EvidenceSnapshotKind,
    EvidenceSnapshotStatus,
    UploadTicket,
    ViewTicket,
)
from app.domain.proctoring.repository import (
    EvidenceAccessLogRepository,
    EvidenceSnapshotRepository,
    UploadTicketRepository,
    ViewTicketRepository,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceService:
    def __init__(self, db, storage: EvidenceStorage) -> None:
        self._db = db
        self._storage = storage
        self.snapshots = EvidenceSnapshotRepository(db)
        self.upload_tickets = UploadTicketRepository(db)
        self.view_tickets = ViewTicketRepository(db)
        self.access_log = EvidenceAccessLogRepository(db)

    def request_snapshot(
        self, *, run_id: str, uid: str, kind: EvidenceSnapshotKind, upload_ticket_ttl_seconds: int,
    ) -> tuple[EvidenceSnapshot, UploadTicket]:
        now = _utc_now()
        snapshot = EvidenceSnapshot(id=uuid4().hex, run_id=run_id, uid=uid, kind=kind, requested_at=now)
        self.snapshots.create(snapshot)
        ticket = UploadTicket(
            id=uuid4().hex, run_id=run_id, uid=uid, snapshot_id=snapshot.id,
            expires_at=now + timedelta(seconds=upload_ticket_ttl_seconds),
        )
        self.upload_tickets.create(ticket)
        return snapshot, ticket

    def confirm_upload(
        self, *, ticket_id: str, uid: str, content: bytes, declared_sha256: str, content_type: str, max_size_bytes: int,
    ) -> EvidenceSnapshot:
        transaction = self._db.transaction()

        @firestore.transactional
        def _consume_ticket(transaction) -> UploadTicket:
            ticket = self.upload_tickets.get_in_transaction(transaction, ticket_id)
            now = _utc_now()
            if ticket is None or ticket.consumed or ticket.uid != uid or now > ticket.expires_at:
                raise UploadTicketInvalidError(f"upload ticket {ticket_id!r} is not valid for this caller")
            self.upload_tickets.mark_consumed_in_transaction(transaction, ticket_id, consumed_at=now)
            return ticket

        ticket = _consume_ticket(transaction)
        snapshot = self.snapshots.get(ticket.snapshot_id)
        if snapshot is None:
            raise LookupError(f"snapshot {ticket.snapshot_id} not found")

        actual_sha256 = hashlib.sha256(content).hexdigest()
        detected_type = detect_image_type(content)
        outcome = decide_confirm_snapshot_upload(
            snapshot=snapshot, declared_sha256=declared_sha256, actual_sha256=actual_sha256,
            size_bytes=len(content), max_size_bytes=max_size_bytes, image_ok=detected_type is not None,
        )
        now = _utc_now()
        if not outcome.accepted:
            if snapshot.status == EvidenceSnapshotStatus.REQUESTED:  # never downgrade evidence that already verified
                self.snapshots.update(
                    snapshot.id,
                    {"status": EvidenceSnapshotStatus.MISSING.value, "missing_reason": outcome.conflict_reason, "uploaded_at": now.isoformat()},
                )
            raise SnapshotHashMismatchError(outcome.conflict_reason)

        storage_ref = f"exam-evidence/{snapshot.run_id}/{snapshot.uid}/{snapshot.id}"
        try:
            self._storage.put(storage_ref, content, content_type=detected_type or content_type)
        except Exception as exc:  # noqa: BLE001 - any storage failure leaves the evidence honestly missing
            self.snapshots.update(
                snapshot.id,
                {"status": EvidenceSnapshotStatus.MISSING.value, "missing_reason": "storage_unavailable", "uploaded_at": now.isoformat()},
            )
            raise EvidenceStorageUnavailableError("evidence storage could not be written") from exc
        self.snapshots.update(
            snapshot.id,
            {
                "status": EvidenceSnapshotStatus.VERIFIED.value, "uploaded_at": now.isoformat(), "verified_at": now.isoformat(),
                "storage_ref": storage_ref, "sha256": actual_sha256, "size_bytes": len(content), "content_type": detected_type,
            },
        )
        return self.snapshots.get(snapshot.id)

    def record_missing(self, *, run_id: str, uid: str, kind: EvidenceSnapshotKind, reason: str) -> EvidenceSnapshot:
        """Submission acceptance never depends on evidence — an explicit
        MISSING record with a reason is a complete, honest outcome, not an
        error (contract: 'Submission acceptance cannot depend on a
        successful image upload.')."""
        now = _utc_now()
        snapshot = EvidenceSnapshot(
            id=uuid4().hex, run_id=run_id, uid=uid, kind=kind, status=EvidenceSnapshotStatus.MISSING,
            requested_at=now, missing_reason=reason,
        )
        self.snapshots.create(snapshot)
        return snapshot

    def issue_view_ticket(self, *, snapshot_id: str, faculty_uid: str, ttl_seconds: int) -> ViewTicket:
        """Only for evidence that was actually stored and verified. Issuing is itself audited."""
        snapshot = self.snapshots.get(snapshot_id)
        if snapshot is None or snapshot.status != EvidenceSnapshotStatus.VERIFIED or not snapshot.storage_ref:
            raise LookupError("there is no stored evidence for this snapshot")
        now = _utc_now()
        ticket = ViewTicket(
            id=uuid4().hex, snapshot_id=snapshot_id, issued_to=faculty_uid,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self.view_tickets.create(ticket)
        self.access_log.create(EvidenceAccessLogEntry(
            id=uuid4().hex, snapshot_id=snapshot_id, accessed_by=faculty_uid, accessed_at=now, outcome="issued",
        ))
        return ticket

    def view_snapshot(self, *, ticket_id: str, faculty_uid: str) -> bytes:
        return self.view_snapshot_with_type(ticket_id=ticket_id, faculty_uid=faculty_uid)[0]

    def view_snapshot_with_type(self, *, ticket_id: str, faculty_uid: str) -> tuple[bytes, str]:
        """Every access is audited: a refused attempt on a real ticket is recorded as `denied`, a served one as
        `viewed`. Camera pictures are never treated as automatic identity/liveness proof; this is a manual review
        aid, nothing more."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _consume_ticket(transaction) -> ViewTicket:
            ticket = self.view_tickets.get_in_transaction(transaction, ticket_id)
            now = _utc_now()
            if ticket is None or ticket.consumed or ticket.issued_to != faculty_uid or now > ticket.expires_at:
                raise ViewTicketInvalidError(f"view ticket {ticket_id!r} is not valid for this caller")
            self.view_tickets.mark_consumed_in_transaction(transaction, ticket_id, consumed_at=now)
            return ticket

        try:
            ticket = _consume_ticket(transaction)
        except ViewTicketInvalidError:
            known = self.view_tickets.get(ticket_id)
            if known is not None:  # an unknown id names no snapshot; only a real ticket can be refused on record
                self.access_log.create(EvidenceAccessLogEntry(
                    id=uuid4().hex, snapshot_id=known.snapshot_id, accessed_by=faculty_uid, accessed_at=_utc_now(), outcome="denied",
                ))
            raise
        snapshot = self.snapshots.get(ticket.snapshot_id)
        if snapshot is None or snapshot.storage_ref is None:
            raise LookupError("snapshot has no stored content")
        content = self._storage.get(snapshot.storage_ref)
        if content is None:
            raise LookupError("stored content missing")

        self.access_log.create(
            EvidenceAccessLogEntry(id=uuid4().hex, snapshot_id=snapshot.id, accessed_by=faculty_uid, accessed_at=_utc_now(), outcome="viewed")
        )
        return content, snapshot.content_type or "application/octet-stream"

    def expire_stale_requests(self, *, now: datetime, older_than_seconds: float, limit: int = 200) -> int:
        """A snapshot still `requested` after its upload ticket has lapsed will never arrive (the capture failed,
        the page closed, the network dropped). Mark it missing so a reviewer sees an honest state, not an eternal
        "requested"."""
        marked = 0
        for snapshot in self.snapshots.list_requested(limit=limit):
            if (now - snapshot.requested_at).total_seconds() > older_than_seconds:
                self.snapshots.update(
                    snapshot.id, {"status": EvidenceSnapshotStatus.MISSING.value, "missing_reason": "upload_not_completed"},
                )
                marked += 1
        return marked

    def delete_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        """Retention/deletion — removes the stored bytes AND the Firestore
        record together (covers 'derivatives/exports': there are none besides
        the one stored object in this phase, so deleting it is complete)."""
        if snapshot.storage_ref:
            self._storage.delete(snapshot.storage_ref)
        self.snapshots.delete(snapshot.id)
