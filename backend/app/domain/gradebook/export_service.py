"""Gradebook CSV/JSON export — job status tracking, ticket-gated downloads,
and an access audit log.

"Async" here means the `ExportJob` STATUS machine (PENDING -> RUNNING ->
SUCCEEDED/FAILED); the actual work runs inline when `run_job` is invoked —
there is no separate task-queue infrastructure anywhere in this codebase
(mirrors the existing finalization-sweeper/outbox-drain "background work"
style). A route/worker calls `create_job` then `run_job` back-to-back today;
swapping in a real queue later only changes WHO calls `run_job` and WHEN,
never this module's contract.

Every CSV cell passes through `sanitize_csv_cell` (contract: CSV-injection
safe). Blob storage reuses the proctoring domain's generic `EvidenceStorage`
Protocol/adapters — the interface (`put`/`get`/`delete`/`exists` over a ref
+ bytes) has nothing evidence-specific about it.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from firebase_admin import firestore

from app.domain.gradebook.aggregation_service import GradebookAggregationService
from app.domain.gradebook.decisions import sanitize_csv_cell
from app.domain.gradebook.errors import ExportExpiredError, ExportJobNotReadyError, ExportTicketInvalidError
from app.domain.gradebook.models import (
    ExportAccessLogEntry,
    ExportDownloadTicket,
    ExportFormat,
    ExportJob,
    ExportJobStatus,
    StudentGradebookRow,
)
from app.domain.gradebook.repository import ExportAccessLogRepository, ExportDownloadTicketRepository, ExportJobRepository
from app.domain.proctoring.evidence_storage import EvidenceStorage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_CSV_COLUMNS = [
    "uid", "attempt_status", "finalized", "raw_score", "raw_max_score", "integrity_disposition", "warning_count",
    "question_id", "slot_index", "item_type", "presented", "outcome", "weighted_score", "max_score",
    "tests_passed", "tests_total", "failure_category", "grading_version",
]


def _row_to_csv_records(row: StudentGradebookRow) -> list[dict[str, Any]]:
    header = {
        "uid": row.uid, "attempt_status": row.attempt_status, "finalized": row.finalized,
        "raw_score": row.raw_score, "raw_max_score": row.raw_max_score,
        "integrity_disposition": row.integrity_disposition.value, "warning_count": row.warning_count,
    }
    if not row.question_results:
        # A student with zero resolvable questions (e.g. never started)
        # still gets one summary-only row — never silently dropped from
        # the export.
        return [{**header, "question_id": "", "slot_index": "", "item_type": "", "presented": "",
                 "outcome": "", "weighted_score": "", "max_score": "", "tests_passed": "",
                 "tests_total": "", "failure_category": "", "grading_version": ""}]
    records = []
    for qr in row.question_results:
        records.append({
            **header,
            "question_id": qr.question_id, "slot_index": qr.slot_index, "item_type": qr.item_type,
            "presented": qr.presented, "outcome": qr.outcome.value, "weighted_score": qr.weighted_score,
            "max_score": qr.max_score, "tests_passed": qr.tests_passed, "tests_total": qr.tests_total,
            "failure_category": qr.failure_category.value if qr.failure_category else "",
            "grading_version": qr.grading_version,
        })
    return records


def build_csv_bytes(rows: list[StudentGradebookRow]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        for record in _row_to_csv_records(row):
            sanitized = {key: sanitize_csv_cell(str(value)) if value != "" else "" for key, value in record.items()}
            writer.writerow(sanitized)
    return buffer.getvalue().encode("utf-8")


def build_json_bytes(rows: list[StudentGradebookRow]) -> bytes:
    payload = [row.model_dump(mode="json") for row in rows]
    return json.dumps(payload, indent=2).encode("utf-8")


class GradebookExportService:
    def __init__(self, db, storage: EvidenceStorage, *, retention_hours: int = 24) -> None:
        self._db = db
        self._storage = storage
        self._retention = timedelta(hours=max(1, retention_hours))
        self.jobs = ExportJobRepository(db)
        self.tickets = ExportDownloadTicketRepository(db)
        self.access_log = ExportAccessLogRepository(db)
        self._aggregation = GradebookAggregationService(db)

    def create_job(self, *, run_id: str, requested_by: str, export_format: ExportFormat) -> ExportJob:
        job = ExportJob(id=uuid4().hex, run_id=run_id, requested_by=requested_by, format=export_format, created_at=_utc_now())
        self.jobs.create(job)
        return job

    def run_job(self, job_id: str) -> ExportJob:
        """Runs a PENDING job to completion. Idempotent-ish in the sense
        that a SUCCEEDED/FAILED job is simply re-run from scratch (the
        gradebook is re-read fresh) — never partially appends to a prior
        export file. The content is only kept for the retention window
        (`expires_at`); `sweep_expired` deletes it afterwards."""
        job = self.jobs.get(job_id)
        if job is None:
            raise LookupError(f"export job {job_id} not found")
        self.jobs.update(job.id, {"status": ExportJobStatus.RUNNING.value})
        try:
            rows = self._aggregation.list_all_rows(job.run_id)
            content = build_csv_bytes(rows) if job.format == ExportFormat.CSV else build_json_bytes(rows)
            content_type = "text/csv" if job.format == ExportFormat.CSV else "application/json"
            storage_ref = f"exam-exports/{job.run_id}/{job.id}.{job.format.value}"
            self._storage.put(storage_ref, content, content_type=content_type)
            now = _utc_now()
            self.jobs.update(
                job.id,
                {
                    "status": ExportJobStatus.SUCCEEDED.value, "completed_at": now.isoformat(),
                    "storage_ref": storage_ref, "row_count": len(rows),
                    "expires_at": (now + self._retention).isoformat(), "expired_at": None,
                },
            )
        except Exception as exc:
            self.jobs.update(
                job.id, {"status": ExportJobStatus.FAILED.value, "error": str(exc), "completed_at": _utc_now().isoformat()}
            )
            raise
        return self.jobs.get(job.id)

    @staticmethod
    def _is_expired(job: ExportJob, now: datetime) -> bool:
        return job.expired_at is not None or (job.expires_at is not None and now >= job.expires_at)

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Deletes the stored content of every export past its retention window and records that on the job.
        Bytes first, record second: a failed delete leaves the job listed so the next sweep retries it."""
        now = now or _utc_now()
        deleted = 0
        for job in self.jobs.list_expired(now):
            try:
                if job.storage_ref:
                    self._storage.delete(job.storage_ref)
                self.jobs.update(job.id, {"storage_ref": None, "expired_at": now.isoformat()})
                deleted += 1
            except Exception:
                continue
        return deleted

    def issue_download_ticket(self, *, job_id: str, issued_to: str, ttl_seconds: int) -> ExportDownloadTicket:
        job = self.jobs.get(job_id)
        if job is None:
            raise LookupError(f"export job {job_id} not found")
        if job.status != ExportJobStatus.SUCCEEDED:
            raise ExportJobNotReadyError(f"export job {job_id} is {job.status.value}, not ready to download")
        if self._is_expired(job, _utc_now()):
            raise ExportExpiredError(f"export job {job_id} has expired and its content was deleted; create a new export")
        ticket = ExportDownloadTicket(
            id=uuid4().hex, export_job_id=job_id, issued_to=issued_to,
            expires_at=_utc_now() + timedelta(seconds=ttl_seconds),
        )
        self.tickets.create(ticket)
        return ticket

    def download(self, *, ticket_id: str, requested_by: str) -> tuple[bytes, ExportJob]:
        """Single-use, short-lived ticket, consumed inside a transaction —
        the same pattern as evidence view tickets (ADR 0013). Every
        successful access is audited. An export past its retention window
        is refused even with a still-valid ticket."""
        transaction = self._db.transaction()

        @firestore.transactional
        def _consume_ticket(transaction) -> ExportDownloadTicket:
            ticket = self.tickets.get_in_transaction(transaction, ticket_id)
            now = _utc_now()
            if ticket is None or ticket.consumed or ticket.issued_to != requested_by or now > ticket.expires_at:
                raise ExportTicketInvalidError(f"export download ticket {ticket_id!r} is not valid for this caller")
            self.tickets.mark_consumed_in_transaction(transaction, ticket_id, consumed_at=now)
            return ticket

        ticket = _consume_ticket(transaction)
        job = self.jobs.get(ticket.export_job_id)
        if job is None or job.storage_ref is None:
            raise LookupError("export job has no stored content")
        if self._is_expired(job, _utc_now()):
            raise ExportExpiredError(f"export job {job.id} has expired and its content was deleted; create a new export")
        content = self._storage.get(job.storage_ref)
        if content is None:
            raise LookupError("stored export content missing")

        self.access_log.create(
            ExportAccessLogEntry(id=uuid4().hex, export_job_id=job.id, accessed_by=requested_by, accessed_at=_utc_now())
        )
        return content, job
