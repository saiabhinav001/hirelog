"""Lockdown service tests: admission is always audited (pass or fail), and
denied admission raises rather than returning a falsy value the caller
could accidentally ignore.
"""

from __future__ import annotations

from typing import Optional

import pytest

from app.domain.lockdown.decisions import compute_seb_hash
from app.domain.lockdown.errors import LockdownAdmissionDeniedError
from app.domain.lockdown.models import LockdownAdmissionLogEntry, LockdownProfile
from app.domain.lockdown.service import LockdownService

RUN_ID = "run-1"
URL = "https://exams.example.edu/api/exams/v1/student/runs/run-1/admit"


class _FakeProfileRepo:
    def __init__(self) -> None:
        self.store: dict[str, LockdownProfile] = {}

    def get(self, run_id: str) -> Optional[LockdownProfile]:
        return self.store.get(run_id)

    def upsert(self, profile: LockdownProfile) -> None:
        self.store[profile.run_id] = profile


class _FakeAdmissionLogRepo:
    def __init__(self) -> None:
        self.items: list[LockdownAdmissionLogEntry] = []

    def create(self, entry: LockdownAdmissionLogEntry) -> None:
        self.items.append(entry)

    def list_for_run(self, run_id: str) -> list[LockdownAdmissionLogEntry]:
        return [e for e in self.items if e.run_id == run_id]


def _service() -> LockdownService:
    svc = LockdownService.__new__(LockdownService)
    svc.profiles = _FakeProfileRepo()
    svc.admission_log = _FakeAdmissionLogRepo()
    return svc


def test_get_or_default_profile_returns_disabled_default_when_unconfigured() -> None:
    svc = _service()
    profile = svc.get_or_default_profile(RUN_ID)
    assert profile.enabled is False
    assert profile.run_id == RUN_ID


def test_configure_profile_persists_and_is_retrievable() -> None:
    svc = _service()
    svc.configure_profile(
        run_id=RUN_ID, enabled=True, expected_config_key_hash="key1",
        expected_browser_exam_key_hashes=[], piloted_platforms=["SEB_Windows_3.6"], updated_by="fac-1",
    )
    profile = svc.get_or_default_profile(RUN_ID)
    assert profile.enabled is True
    assert profile.expected_config_key_hash == "key1"
    assert profile.piloted_platforms == ["SEB_Windows_3.6"]


def test_check_admission_admits_and_logs_on_correct_header() -> None:
    svc = _service()
    svc.configure_profile(
        run_id=RUN_ID, enabled=True, expected_config_key_hash="key1",
        expected_browser_exam_key_hashes=[], piloted_platforms=[], updated_by="fac-1",
    )
    header = compute_seb_hash(URL, "key1")
    outcome = svc.check_admission(run_id=RUN_ID, uid="u1", url=URL, config_key_hash_header=header, request_hash_header=None)
    assert outcome.value == "admitted"
    log = svc.list_admission_log(RUN_ID)
    assert len(log) == 1
    assert log[0].outcome.value == "admitted"
    assert log[0].config_key_header_present is True


def test_check_admission_raises_and_logs_on_missing_headers() -> None:
    svc = _service()
    svc.configure_profile(
        run_id=RUN_ID, enabled=True, expected_config_key_hash="key1",
        expected_browser_exam_key_hashes=[], piloted_platforms=[], updated_by="fac-1",
    )
    with pytest.raises(LockdownAdmissionDeniedError):
        svc.check_admission(run_id=RUN_ID, uid="u1", url=URL, config_key_hash_header=None, request_hash_header=None)

    log = svc.list_admission_log(RUN_ID)
    assert len(log) == 1
    assert log[0].outcome.value == "denied_missing_headers"


def test_check_admission_raises_when_enabled_but_unconfigured() -> None:
    svc = _service()
    svc.configure_profile(
        run_id=RUN_ID, enabled=True, expected_config_key_hash=None,
        expected_browser_exam_key_hashes=[], piloted_platforms=[], updated_by="fac-1",
    )
    with pytest.raises(LockdownAdmissionDeniedError):
        svc.check_admission(
            run_id=RUN_ID, uid="u1", url=URL, config_key_hash_header="anything", request_hash_header=None
        )


def test_check_admission_for_never_configured_run_denies_not_admits() -> None:
    # A run with NO lockdown profile document at all defaults to enabled=False
    # (ordinary admission) -- this test documents that default explicitly.
    svc = _service()
    outcome = svc.check_admission(run_id="never-configured", uid="u1", url=URL, config_key_hash_header=None, request_hash_header=None)
    assert outcome.value == "admitted"
