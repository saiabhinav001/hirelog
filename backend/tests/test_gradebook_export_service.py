"""Gradebook export tests: CSV/JSON byte-building (including the CSV
formula-injection-safe path end-to-end through `csv.DictWriter`), job
lifecycle (PENDING -> RUNNING -> SUCCEEDED/FAILED), ticket-gated single-use
downloads, and the access audit log — via fake in-memory repos and the
`InMemoryEvidenceStorage` real-but-in-memory blob store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.domain.gradebook.export_service import GradebookExportService, build_csv_bytes, build_json_bytes
from app.domain.gradebook.errors import ExportJobNotReadyError, ExportTicketInvalidError
from app.domain.gradebook.models import (
    ExportAccessLogEntry,
    ExportDownloadTicket,
    ExportFormat,
    ExportJob,
    ExportJobStatus,
    FailureCategory,
    IntegrityDisposition,
    QuestionOutcome,
    StudentGradebookRow,
    StudentQuestionResult,
)
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
RUN_ID = "run-1"


def _row(uid: str, **overrides) -> StudentGradebookRow:
    defaults = dict(
        uid=uid, attempt_status="finalized", finalized=True, raw_score=10.0, raw_max_score=10.0,
        integrity_disposition=IntegrityDisposition.CLEAN, warning_count=0,
        question_results=[
            StudentQuestionResult(
                uid=uid, question_id="rev-1:slot:0", slot_index=0, item_type="coding", presented=True,
                outcome=QuestionOutcome.FULLY_SOLVED, weighted_score=10.0, max_score=10.0,
                tests_passed=2, tests_total=2, failure_category=FailureCategory.NONE,
            )
        ],
    )
    defaults.update(overrides)
    return StudentGradebookRow(**defaults)


# ── build_csv_bytes / build_json_bytes ──────────────────────────────────────

def test_build_csv_bytes_contains_expected_header_and_row() -> None:
    content = build_csv_bytes([_row("u1")]).decode("utf-8")
    lines = content.splitlines()
    assert lines[0].split(",")[0] == "uid"
    assert "u1" in lines[1]
    assert "fully_solved" in lines[1]


def test_build_csv_bytes_neutralizes_formula_injection_in_uid() -> None:
    # A malicious/mistaken uid or free-text field starting with a formula
    # trigger character must never reach the raw cell unescaped.
    content = build_csv_bytes([_row("=cmd|'/bin/bash'!A1")]).decode("utf-8")
    assert "'=cmd" in content
    assert content.count("\n=cmd") == 0  # never appears as a bare leading formula


def test_build_csv_bytes_student_with_no_questions_still_gets_one_row() -> None:
    row = _row("u2", question_results=[])
    content = build_csv_bytes([row]).decode("utf-8")
    lines = [line for line in content.splitlines() if line.startswith("u2")]
    assert len(lines) == 1


def test_build_json_bytes_round_trips_structure() -> None:
    import json

    content = build_json_bytes([_row("u1")])
    payload = json.loads(content)
    assert payload[0]["uid"] == "u1"
    assert payload[0]["question_results"][0]["outcome"] == "fully_solved"


# ── GradebookExportService ──────────────────────────────────────────────────

class _FakeAggregationService:
    def __init__(self, rows: list[StudentGradebookRow]) -> None:
        self._rows = rows

    def list_all_rows(self, run_id: str) -> list[StudentGradebookRow]:
        return self._rows


class _FakeJobRepo:
    def __init__(self) -> None:
        self.store: dict[str, ExportJob] = {}

    def create(self, job: ExportJob) -> None:
        self.store[job.id] = job

    def get(self, job_id: str) -> Optional[ExportJob]:
        return self.store.get(job_id)

    def update(self, job_id: str, update: dict) -> None:
        # Round-trip through the model, as the real repository does through Firestore (ISO strings -> datetimes).
        self.store[job_id] = ExportJob.model_validate({**self.store[job_id].model_dump(mode="json"), **update})

    def list_expired(self, now) -> list[ExportJob]:
        return [j for j in self.store.values() if j.expires_at is not None and j.expires_at <= now and j.expired_at is None]


class _FakeTicketRepo:
    def __init__(self) -> None:
        self.store: dict[str, ExportDownloadTicket] = {}

    def create(self, ticket: ExportDownloadTicket) -> None:
        self.store[ticket.id] = ticket

    def get_in_transaction(self, transaction, ticket_id: str) -> Optional[ExportDownloadTicket]:
        return self.store.get(ticket_id)

    def mark_consumed_in_transaction(self, transaction, ticket_id: str, *, consumed_at) -> None:
        self.store[ticket_id] = self.store[ticket_id].model_copy(update={"consumed": True, "consumed_at": consumed_at})


class _FakeAccessLogRepo:
    def __init__(self) -> None:
        self.items: list[ExportAccessLogEntry] = []

    def create(self, entry: ExportAccessLogEntry) -> None:
        self.items.append(entry)


class _PassthroughTransaction:
    pass


def _service(*, rows=None) -> GradebookExportService:
    svc = GradebookExportService.__new__(GradebookExportService)
    svc._db = _FakeDb()
    svc._storage = InMemoryEvidenceStorage()
    svc._retention = timedelta(hours=24)
    svc.jobs = _FakeJobRepo()
    svc.tickets = _FakeTicketRepo()
    svc.access_log = _FakeAccessLogRepo()
    svc._aggregation = _FakeAggregationService(rows if rows is not None else [_row("u1")])
    return svc


class _FakeDb:
    def transaction(self):
        return _PassthroughTransaction()


@pytest.fixture(autouse=True)
def _patch_transactional(monkeypatch):
    import app.domain.gradebook.export_service as export_service_module

    monkeypatch.setattr(export_service_module.firestore, "transactional", lambda fn: fn)


def test_create_and_run_job_succeeds() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.CSV)
    assert job.status == ExportJobStatus.PENDING

    completed = svc.run_job(job.id)
    assert completed.status == ExportJobStatus.SUCCEEDED
    assert completed.storage_ref is not None
    assert completed.row_count == 1
    assert svc._storage.exists(completed.storage_ref)


def test_run_job_failure_marks_job_failed_and_reraises() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.CSV)

    def _boom(run_id: str):
        raise RuntimeError("aggregation exploded")

    svc._aggregation.list_all_rows = _boom
    with pytest.raises(RuntimeError):
        svc.run_job(job.id)
    assert svc.jobs.get(job.id).status == ExportJobStatus.FAILED
    assert "exploded" in svc.jobs.get(job.id).error


def test_issue_download_ticket_requires_succeeded_job() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.JSON)
    with pytest.raises(ExportJobNotReadyError):
        svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)


def test_download_with_valid_ticket_returns_content_and_audits_access() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.JSON)
    svc.run_job(job.id)
    ticket = svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)

    content, downloaded_job = svc.download(ticket_id=ticket.id, requested_by="fac1")
    assert b"u1" in content
    assert downloaded_job.id == job.id
    assert len(svc.access_log.items) == 1
    assert svc.access_log.items[0].accessed_by == "fac1"


def test_download_ticket_is_single_use() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.JSON)
    svc.run_job(job.id)
    ticket = svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)

    svc.download(ticket_id=ticket.id, requested_by="fac1")
    with pytest.raises(ExportTicketInvalidError):
        svc.download(ticket_id=ticket.id, requested_by="fac1")


def test_download_ticket_rejects_wrong_caller() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.JSON)
    svc.run_job(job.id)
    ticket = svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)
    with pytest.raises(ExportTicketInvalidError):
        svc.download(ticket_id=ticket.id, requested_by="someone-else")


def test_download_ticket_rejects_expired() -> None:
    svc = _service()
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.JSON)
    svc.run_job(job.id)
    ticket = svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)
    svc.tickets.store[ticket.id] = ticket.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    with pytest.raises(ExportTicketInvalidError):
        svc.download(ticket_id=ticket.id, requested_by="fac1")
