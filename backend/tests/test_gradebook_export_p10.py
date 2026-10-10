"""Gradebook export retention and CSV-injection tests (Prompt 10)."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest

from app.domain.gradebook.errors import ExportExpiredError
from app.domain.gradebook.export_service import GradebookExportService, build_csv_bytes
from app.domain.gradebook.models import ExportFormat, ExportJob
from test_gradebook_export_service import NOW, RUN_ID, _row, _service


@pytest.fixture(autouse=True)
def _patch_transactional(monkeypatch):
    import app.domain.gradebook.export_service as export_service_module

    monkeypatch.setattr(export_service_module.firestore, "transactional", lambda fn: fn)


def _succeeded_job(svc: GradebookExportService) -> ExportJob:
    job = svc.create_job(run_id=RUN_ID, requested_by="fac1", export_format=ExportFormat.CSV)
    return svc.run_job(job.id)


def test_a_finished_export_records_when_its_content_expires() -> None:
    svc = _service()
    job = _succeeded_job(svc)
    assert job.expires_at is not None
    assert timedelta(hours=23, minutes=59) < job.expires_at - job.completed_at <= timedelta(hours=24, seconds=1)


def test_sweep_deletes_expired_content_and_keeps_the_job_record() -> None:
    svc = _service()
    job = _succeeded_job(svc)
    ref = job.storage_ref
    assert svc._storage.exists(ref)

    assert svc.sweep_expired(now=job.expires_at - timedelta(minutes=1)) == 0  # not yet
    assert svc._storage.exists(ref)

    assert svc.sweep_expired(now=job.expires_at + timedelta(seconds=1)) == 1
    assert not svc._storage.exists(ref)
    swept = svc.jobs.get(job.id)
    assert swept.expired_at is not None and swept.storage_ref is None
    assert swept.requested_by == "fac1" and swept.row_count == 1  # who asked / how many rows stays on record
    assert svc.sweep_expired(now=job.expires_at + timedelta(days=1)) == 0  # idempotent


def test_no_ticket_is_issued_for_an_expired_export() -> None:
    svc = _service()
    job = _succeeded_job(svc)
    svc.jobs.update(job.id, {"expires_at": (NOW - timedelta(hours=1)).isoformat()})
    with pytest.raises(ExportExpiredError):
        svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)


def test_a_ticket_issued_before_expiry_cannot_download_after_the_content_expired() -> None:
    svc = _service()
    job = _succeeded_job(svc)
    ticket = svc.issue_download_ticket(job_id=job.id, issued_to="fac1", ttl_seconds=60)
    svc.jobs.update(job.id, {"expires_at": (NOW - timedelta(hours=1)).isoformat()})
    with pytest.raises(ExportExpiredError):
        svc.download(ticket_id=ticket.id, requested_by="fac1")
    assert svc.access_log.items == []  # no access was granted, so none is logged


def test_a_failed_delete_leaves_the_job_for_the_next_sweep() -> None:
    svc = _service()
    job = _succeeded_job(svc)

    def _boom(ref: str) -> None:
        raise OSError("bucket unavailable")

    real_delete = svc._storage.delete
    svc._storage.delete = _boom
    assert svc.sweep_expired(now=job.expires_at + timedelta(seconds=1)) == 0
    assert svc.jobs.get(job.id).expired_at is None
    svc._storage.delete = real_delete
    assert svc.sweep_expired(now=job.expires_at + timedelta(seconds=1)) == 1


HOSTILE = [
    "=1+1", "+1+1", "-1+1", "@SUM(A1)", "\t=1+1", "\r=1+1", "\n=1+1", "  =1+1", "\u00a0=1+1", "\u200b=1+1",
    "\uff1d1+1", "\uff0b1", "\uff0d1", "\uff20SUM(A1)", "=cmd|' /C calc'!A0",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_every_formula_prefix_is_neutralized_in_the_csv(hostile: str) -> None:
    content = build_csv_bytes([_row(hostile)]).decode("utf-8")
    cell = next(csv.DictReader(io.StringIO(content)))["uid"]
    assert cell == "'" + hostile  # visible text kept; a leading quote makes it inert in Excel/LibreOffice/Sheets


def test_ordinary_values_are_not_altered() -> None:
    content = build_csv_bytes([_row("student-42")]).decode("utf-8")
    cell = next(csv.DictReader(io.StringIO(content)))
    assert cell["uid"] == "student-42" and cell["raw_score"] == "10.0" and cell["outcome"] == "fully_solved"
