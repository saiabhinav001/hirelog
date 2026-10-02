"""`ExamStudentService.admit()` tests — focused on the Prompt 11 rollback lever
(`settings.EXAM_NEW_ADMISSIONS_PAUSED`, docs/adr/0016).

Uses fake in-memory repos + `firestore.transactional` patched to a plain
passthrough (same pattern as `test_institution_auth_service.py`) — this
proves the admission LOGIC, not true multi-writer transactional concurrency
(see docs/adr/0008 "Known limitations").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from app.core.config import settings
from app.domain.exams import student_service as student_service_module
from app.domain.exams.clock import FixedClock
from app.domain.exams.models import Attempt, AttemptStatus, ExamRun, RunStatus
from app.domain.exams.policy import ExamPolicy
from app.domain.exams.student_service import ExamStudentService

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN_ID = "run-1"
UID = "student-1"


class _FakeRunRepo:
    def __init__(self, run: ExamRun) -> None:
        self._run = run

    def get_in_transaction(self, _transaction, run_id: str) -> Optional[ExamRun]:
        return self._run if run_id == self._run.id else None


class _FakeAttemptRepo:
    def __init__(self, attempt: Optional[Attempt] = None) -> None:
        self._by_key: dict[tuple[str, str], Attempt] = {}
        if attempt is not None:
            self._by_key[(attempt.run_id, attempt.uid)] = attempt

    def get_in_transaction(self, _transaction, run_id: str, uid: str) -> Optional[Attempt]:
        return self._by_key.get((run_id, uid))

    def create_in_transaction(self, _transaction, attempt: Attempt) -> None:
        self._by_key[(attempt.run_id, attempt.uid)] = attempt


class _FakeDb:
    """`.transaction()` only needs to return SOMETHING — `firestore.transactional`
    is patched to a passthrough below, so the returned value is never used
    for real transaction semantics."""

    def transaction(self):
        return object()


def _run(**overrides) -> ExamRun:
    defaults = dict(
        id=RUN_ID, definition_id="def-1", revision_id="rev-1", status=RunStatus.RELEASED,
        policy=ExamPolicy(duration_minutes=60), enrolled_uids=[UID], created_at=NOW, updated_at=NOW,
    )
    defaults.update(overrides)
    return ExamRun(**defaults)


def _service(monkeypatch, *, run: ExamRun, attempt: Optional[Attempt] = None) -> ExamStudentService:
    service = ExamStudentService.__new__(ExamStudentService)
    service._db = _FakeDb()
    service._clock = FixedClock(NOW)
    service.runs = _FakeRunRepo(run)
    service.attempts = _FakeAttemptRepo(attempt)
    monkeypatch.setattr(student_service_module.firestore, "transactional", lambda fn: fn)
    return service


def test_admit_succeeds_when_admissions_not_paused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_NEW_ADMISSIONS_PAUSED", False)
    service = _service(monkeypatch, run=_run())
    attempt = service.admit(RUN_ID, UID)
    assert attempt.status == AttemptStatus.IN_PROGRESS


def test_admit_blocks_brand_new_admission_when_paused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_NEW_ADMISSIONS_PAUSED", True)
    service = _service(monkeypatch, run=_run())
    with pytest.raises(PermissionError, match="new_admissions_paused"):
        service.admit(RUN_ID, UID)


def test_admit_is_idempotent_for_already_admitted_student_even_when_paused(monkeypatch) -> None:
    """The core guarantee (contract #12): a rollback must never strand a
    student who was already let in — only genuinely NEW admissions stop."""
    existing = Attempt(
        run_id=RUN_ID, uid=UID, status=AttemptStatus.IN_PROGRESS, version=1, epoch=1,
        admitted_at=NOW, updated_at=NOW,
    )
    monkeypatch.setattr(settings, "EXAM_NEW_ADMISSIONS_PAUSED", True)
    service = _service(monkeypatch, run=_run(), attempt=existing)
    attempt = service.admit(RUN_ID, UID)
    assert attempt.status == AttemptStatus.IN_PROGRESS
    assert attempt.uid == UID


def test_admit_checks_run_released_before_admissions_paused(monkeypatch) -> None:
    """Existing run-state checks still take priority — pausing admissions
    globally doesn't change or mask the per-run released-state contract."""
    monkeypatch.setattr(settings, "EXAM_NEW_ADMISSIONS_PAUSED", True)
    service = _service(monkeypatch, run=_run(status=RunStatus.DRAFT))
    with pytest.raises(PermissionError, match="run_not_released"):
        service.admit(RUN_ID, UID)
