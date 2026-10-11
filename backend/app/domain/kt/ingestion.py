"""Durable KT ingestion pipeline (Prompt 4, ADR 0020).

    accept (route)  ->  private quarantine storage + upload record + outbox job   [one transaction]
    process (worker) -> scan -> parse in a killable child process -> immutable extracted version

Everything after "accept" runs in the existing outbox worker (`exam_main` drain loop), so it is
restart-safe: the job survives a crash, a redelivered job is idempotent (statuses are checked, the
extracted version is create-only with a deterministic id) and a stale job for an older attempt is
ignored. Nothing in an uploaded document or fetched page is executed, rendered or followed.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable, Optional
from uuid import uuid4

from firebase_admin import firestore

from app.core import background
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.clock import Clock, system_clock
from app.domain.exams.errors import CommandRejectedError
from app.domain.exams.models import OutboxEntry
from app.domain.exams.repository import OutboxRepository, new_outbox_entry
from app.domain.kt.limits import PARSE_TIMEOUT_SECONDS
from app.domain.kt.malware_scan import MalwareScanner
from app.domain.kt.models import (
    ExtractedAnchor,
    ExtractedVersion,
    SourceReviewStatus,
    UploadedSource,
    UploadStatus,
)
from app.domain.kt.parse_isolation import IngestionParseError, parse_in_subprocess
from app.domain.kt.parsers import PARSER_LABELS, ParseResult
from app.domain.kt.repository import ApprovedSourceRepository, UploadedSourceRepository
from app.domain.kt.source_registry import decide_reject_source
from app.domain.kt.storage import new_object_key

logger = logging.getLogger("hirelog.exams.kt.ingestion")

KT_INGEST_KIND = "kt_ingest"
INGESTION_ACTOR = "system:kt-ingestion"

# Terminal for the current attempt: a redelivered job does nothing. `retry` starts a new attempt.
SETTLED_STATUSES = frozenset({
    UploadStatus.PARSED, UploadStatus.INFECTED, UploadStatus.REJECTED, UploadStatus.DELETED,
    UploadStatus.PARSE_FAILED, UploadStatus.SCAN_UNAVAILABLE,
})
RETRYABLE_STATUSES = frozenset({UploadStatus.PARSE_FAILED, UploadStatus.SCAN_UNAVAILABLE})
# Bytes are gone or untrustworthy: only a fresh upload (replace) can fix these.
NON_RETRYABLE_CODES = frozenset({"storage_missing", "integrity_mismatch"})
COUNTED_STATUSES_EXCLUDED = frozenset({UploadStatus.DELETED, UploadStatus.REJECTED})


class UploadLimitError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"you already have {limit} active uploads; delete some before adding more")
        self.limit = limit


class DuplicateUploadError(Exception):
    def __init__(self, existing_upload_id: str) -> None:
        super().__init__("this exact file is already uploaded")
        self.existing_upload_id = existing_upload_id


class TransientIngestionError(Exception):
    """Infrastructure trouble (scanner unreachable, parser killed once): the outbox redelivers the job."""


def _content_hash(anchors: list[ExtractedAnchor]) -> str:
    return hashlib.sha256("\n".join(a.text for a in anchors).encode("utf-8")).hexdigest()


def _stable_anchors(file_sha256: str, anchors: list[ExtractedAnchor]) -> list[ExtractedAnchor]:
    """Anchor ids derived from the file hash + position, so re-parsing identical bytes cites the same anchors."""
    return [
        a.model_copy(update={"anchor_id": hashlib.sha256(f"{file_sha256}:{i}".encode("utf-8")).hexdigest()[:32]})
        for i, a in enumerate(anchors)
    ]


class KtIngestionService:
    def __init__(
        self,
        db,
        storage,
        scanner: MalwareScanner,
        *,
        parse_fn: Optional[Callable[[str, bytes], ParseResult]] = None,
        clock: Clock = system_clock,
        max_active_uploads: int = 200,
        parse_memory_mb: int = 1024,
    ) -> None:
        self._db = db
        self._storage = storage
        self._scanner = scanner
        self._clock = clock
        self._max_active = max_active_uploads
        self._parse = parse_fn or (
            lambda kind, content: parse_in_subprocess(kind, content, timeout=PARSE_TIMEOUT_SECONDS, memory_mb=parse_memory_mb)
        )
        self.uploads = UploadedSourceRepository(db)
        self.sources = ApprovedSourceRepository(db)
        self._outbox = OutboxRepository(db)
        self._audit = AuthzAuditRepository(db)

    # -- intake ----------------------------------------------------------------------

    def _audit_event(self, kind: str, upload: UploadedSource, actor: str, **detail):
        return build_audit_event(
            kind, uid=upload.uploaded_by, actor_uid=actor,
            detail={"upload_id": upload.id, **{k: v for k, v in detail.items() if v is not None}}, now=self._clock.now(),
        )

    def record_rejected(
        self, *, owner_uid: str, filename: str, declared_type: str, content: bytes, reason: str
    ) -> UploadedSource:
        """Keep a history row for a file that failed validation. No bytes are stored."""
        now = self._clock.now()
        upload = UploadedSource(
            id=uuid4().hex,
            uploaded_by=owner_uid, original_filename=filename[:260], declared_content_type=declared_type[:200],
            size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), status=UploadStatus.REJECTED,
            rejection_reason=reason, failure_code="invalid_file", storage_ref="", created_at=now, updated_at=now,
        )
        self.uploads.create(upload)
        self._audit.append(self._audit_event("kt_upload_rejected", upload, owner_uid, reason=reason))
        return upload

    def accept(
        self,
        *,
        owner_uid: str,
        filename: str,
        declared_type: str,
        content: bytes,
        detected_kind: str,
        source_kind: str = "file",
        origin_url: Optional[str] = None,
        final_url: Optional[str] = None,
        fetched_at=None,
        replaces_upload_id: Optional[str] = None,
    ) -> UploadedSource:
        """Validated bytes -> quarantine. The caller has already validated the bytes and authorised the owner."""
        sha256 = hashlib.sha256(content).hexdigest()
        owned = self.uploads.list_by_owner(owner_uid)
        if sum(1 for u in owned if u.status not in COUNTED_STATUSES_EXCLUDED) >= self._max_active:
            raise UploadLimitError(self._max_active)
        for existing in owned:
            if existing.sha256 == sha256 and existing.status not in (
                UploadStatus.DELETED, UploadStatus.REJECTED, UploadStatus.INFECTED
            ) and existing.id != replaces_upload_id:
                raise DuplicateUploadError(existing.id)

        storage_ref = new_object_key()
        # Always opaque bytes: nothing stored here can ever be served as an active content type.
        self._storage.put(storage_ref, content, content_type="application/octet-stream")

        now = self._clock.now()
        upload = UploadedSource(
            id=hashlib.sha256(f"{storage_ref}".encode()).hexdigest()[:32], uploaded_by=owner_uid,
            original_filename=filename[:260], declared_content_type=declared_type[:200], detected_kind=detected_kind,
            size_bytes=len(content), sha256=sha256, status=UploadStatus.QUARANTINED, storage_ref=storage_ref,
            created_at=now, updated_at=now, source_kind=source_kind, origin_url=origin_url, final_url=final_url,
            fetched_at=fetched_at, ingest_attempt=1, replaces_upload_id=replaces_upload_id,
        )
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> None:
            if replaces_upload_id:
                old = self.uploads.get_in_transaction(transaction, replaces_upload_id)
                if old is None or old.status == UploadStatus.DELETED:
                    raise CommandRejectedError("replace", "upload_not_replaceable")
                self.uploads.update_in_transaction(
                    transaction, replaces_upload_id,
                    {"superseded_by_upload_id": upload.id, "updated_at": now.isoformat()},
                )
            self.uploads.create_in_transaction(transaction, upload)
            self._outbox.add_in_transaction(transaction, self._job(upload, now))
            self._audit.append_in_transaction(
                transaction,
                self._audit_event(
                    "kt_upload_replaced" if replaces_upload_id else "kt_upload_accepted", upload, owner_uid,
                    file_kind=detected_kind, size_bytes=len(content), source_kind=source_kind, replaces=replaces_upload_id,
                ),
            )

        try:
            _txn(transaction)
        except Exception:
            self._storage.delete(storage_ref)  # nothing references the bytes
            raise
        background.wake("kt")
        return upload

    def _job(self, upload: UploadedSource, now) -> OutboxEntry:
        return new_outbox_entry(
            kind=KT_INGEST_KIND, run_id="",
            payload={"upload_id": upload.id, "ingest_attempt": upload.ingest_attempt}, now=now,
        )

    # -- teacher commands ----------------------------------------------------------------

    def retry(self, upload_id: str, *, actor_uid: str) -> UploadedSource:
        """Start a new processing attempt for a failed upload. History is kept: attempts only count up."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> UploadedSource:
            now = self._clock.now()
            upload = self.uploads.get_in_transaction(transaction, upload_id)
            if upload is None:
                raise LookupError(f"upload {upload_id} not found")
            if upload.status not in RETRYABLE_STATUSES:
                raise CommandRejectedError("retry", f"status_{upload.status.value}_cannot_be_retried")
            if upload.failure_code in NON_RETRYABLE_CODES:
                raise CommandRejectedError("retry", "source_bytes_unavailable_replace_the_file")
            updated = upload.model_copy(update={
                "status": UploadStatus.QUARANTINED, "ingest_attempt": upload.ingest_attempt + 1,
                "failure_code": None, "rejection_reason": None, "updated_at": now,
            })
            self.uploads.update_in_transaction(transaction, upload_id, {
                "status": updated.status.value, "ingest_attempt": updated.ingest_attempt,
                "failure_code": None, "rejection_reason": None, "updated_at": now.isoformat(),
            })
            self._outbox.add_in_transaction(transaction, self._job(updated, now))
            self._audit.append_in_transaction(
                transaction,
                self._audit_event("kt_upload_retried", upload, actor_uid, attempt=updated.ingest_attempt,
                                  previous_failure=upload.failure_code),
            )
            return updated

        retried = _txn(transaction)
        background.wake("kt")
        return retried

    def delete(self, upload_id: str, *, actor_uid: str) -> UploadedSource:
        """Remove the bytes and extracted text; keep the metadata row and audit trail.

        Refused while an APPROVED source cites the upload (its anchors back published items).
        Pending sources built on it are rejected with a reason. Re-running is safe: cleanup is idempotent."""
        upload = self.uploads.get(upload_id)
        if upload is None:
            raise LookupError(f"upload {upload_id} not found")
        sources = self.sources.list_by_upload(upload_id)
        if upload.status != UploadStatus.DELETED and any(s.review_status == SourceReviewStatus.APPROVED for s in sources):
            raise CommandRejectedError("delete", "source_is_approved_and_may_back_published_items")

        now = self._clock.now()
        if upload.status != UploadStatus.DELETED:
            transaction = self._db.transaction()

            @firestore.transactional
            def _txn(transaction) -> None:
                current = self.uploads.get_in_transaction(transaction, upload_id)
                if current is None:
                    raise LookupError(f"upload {upload_id} not found")
                self.uploads.update_in_transaction(transaction, upload_id, {
                    "status": UploadStatus.DELETED.value, "deleted_at": now.isoformat(), "deleted_by": actor_uid,
                    "updated_at": now.isoformat(), "latest_extracted_version_id": None,
                })
                self._audit.append_in_transaction(
                    transaction,
                    self._audit_event("kt_upload_deleted", current, actor_uid, previous_status=current.status.value),
                )

            _txn(transaction)
            for source in sources:
                if source.review_status == SourceReviewStatus.PENDING:
                    self.sources.save(decide_reject_source(source, reviewer=actor_uid, reason="the uploaded file was deleted", now=now))

        if upload.storage_ref:
            self._storage.delete(upload.storage_ref)
        self.uploads.delete_extracted_versions(upload_id)
        return self.uploads.get(upload_id) or upload

    # -- worker ---------------------------------------------------------------------------

    def _set_status(self, upload_id: str, status: UploadStatus, **extra) -> None:
        self.uploads.update(upload_id, {"status": status.value, "updated_at": self._clock.now().isoformat(), **extra})

    def _settle(self, upload: UploadedSource, status: UploadStatus, code: str, message: str, *, audit_kind: str) -> str:
        self._set_status(upload.id, status, failure_code=code, rejection_reason=message)
        self._audit.append(self._audit_event(audit_kind, upload, INGESTION_ACTOR, code=code, attempt=upload.ingest_attempt))
        return status.value

    def process(self, upload_id: str, ingest_attempt: int, *, delivery_attempt: int = 0, final_delivery: bool = True) -> str:
        """One worker pass. Returns a short outcome label (used by tests and logs).

        Raises `TransientIngestionError` for trouble the outbox should redeliver; on the
        `final_delivery` that trouble is settled as a failure instead of retried forever."""
        upload = self.uploads.get(upload_id)
        if upload is None or upload.ingest_attempt != ingest_attempt:
            return "stale"
        if upload.status in SETTLED_STATUSES:
            return "already_settled"

        content = self._storage.get(upload.storage_ref) if upload.storage_ref else None
        if content is None:
            return self._settle(upload, UploadStatus.PARSE_FAILED, "storage_missing",
                                "The stored file is no longer available. Upload it again.", audit_kind="kt_upload_failed")
        if hashlib.sha256(content).hexdigest() != upload.sha256:
            return self._settle(upload, UploadStatus.PARSE_FAILED, "integrity_mismatch",
                                "The stored file does not match what was uploaded. Upload it again.", audit_kind="kt_upload_failed")

        self._set_status(upload_id, UploadStatus.SCANNING)
        scan = self._scanner.scan(content)
        if scan.infected:
            self._storage.delete(upload.storage_ref)
            return self._settle(upload, UploadStatus.INFECTED, "infected",
                                "The malware scan flagged this file. It was removed and will not be read.", audit_kind="kt_upload_infected")
        if not scan.clean:
            if self._scanner.configured and not final_delivery:
                raise TransientIngestionError(scan.detail or "scanner unavailable")
            message = (
                "No malware scanner is configured, so the file was not read. Ask an administrator to enable scanning, then retry."
                if not self._scanner.configured
                else "The malware scanner could not be reached. Try again in a few minutes."
            )
            return self._settle(upload, UploadStatus.SCAN_UNAVAILABLE, "scanner_unavailable", message,
                                audit_kind="kt_upload_scan_unavailable")

        self._set_status(upload_id, UploadStatus.CLEAN)
        self._set_status(upload_id, UploadStatus.PARSING)
        try:
            result = self._parse(upload.detected_kind or "", content)
        except IngestionParseError as exc:
            if exc.retryable and delivery_attempt < 1 and not final_delivery:
                raise TransientIngestionError(exc.code) from exc  # one automatic retry, then the teacher decides
            return self._settle(upload, UploadStatus.PARSE_FAILED, exc.code, exc.message, audit_kind="kt_upload_failed")

        anchors = _stable_anchors(upload.sha256, result.anchors)
        version_id = f"{upload.id}-a{ingest_attempt}"
        existing = self.uploads.list_extracted_versions(upload_id)
        number = max((v.version_number for v in existing if v.id != version_id), default=0) + 1
        version = ExtractedVersion(
            id=version_id, source_id=upload.id, version_number=number, created_at=self._clock.now(), anchors=anchors,
            total_pages=result.total_pages, truncated=result.truncated, content_sha256=_content_hash(anchors),
            parser=PARSER_LABELS.get(upload.detected_kind or "", None), warnings=result.warnings, stats=result.stats,
            ingest_attempt=ingest_attempt,
        )
        self.uploads.create_extracted_version_once(version)
        self._set_status(upload_id, UploadStatus.PARSED, latest_extracted_version_id=version_id, failure_code=None,
                         rejection_reason=None)
        self._audit.append(self._audit_event(
            "kt_upload_parsed", upload, INGESTION_ACTOR, version_id=version_id, anchors=len(anchors),
            warnings=[w.code for w in result.warnings] or None, attempt=ingest_attempt,
        ))
        return "parsed"


class KtIngestionOutboxHandler:
    """Plugs the pipeline into the existing `CompositeOutboxHandler` (no second worker)."""

    def __init__(self, service: KtIngestionService, *, max_attempts: int = 5) -> None:
        self._service = service
        self._max_attempts = max_attempts

    def handle(self, entry: OutboxEntry) -> None:
        if entry.kind != KT_INGEST_KIND:
            return
        upload_id = str(entry.payload.get("upload_id", ""))
        attempt = int(entry.payload.get("ingest_attempt", 0))
        if not upload_id:
            return
        final = entry.attempts + 1 >= self._max_attempts
        try:
            self._service.process(upload_id, attempt, delivery_attempt=entry.attempts, final_delivery=final)
        except TransientIngestionError:
            raise  # the drain loop records the failure and redelivers after the lease
        except Exception as exc:  # noqa: BLE001
            if not final:
                raise
            logger.exception("kt ingestion gave up on upload %s", upload_id)
            upload = self._service.uploads.get(upload_id)
            if upload is not None and upload.status not in SETTLED_STATUSES:
                self._service._settle(  # noqa: SLF001 - same module family, one place that settles failures
                    upload, UploadStatus.PARSE_FAILED, "processing_error",
                    f"Processing failed repeatedly ({type(exc).__name__}). Retry later or contact an administrator.",
                    audit_kind="kt_upload_failed",
                )
