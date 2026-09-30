"""Gradebook release service tests: release versioning, defective-question
resolution bookkeeping, and grade-appeal submit/resolve/reversal — using
fake in-memory repos via the `Service.__new__` bypass pattern.
"""

from __future__ import annotations

from typing import Optional

import pytest

from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    GradeAppeal,
    GradeAppealStatus,
    ReleaseRecord,
)
from app.domain.gradebook.release_service import GradebookReleaseService

RUN_ID = "run-1"


class _FakeReleaseRepo:
    def __init__(self) -> None:
        self.items: list[ReleaseRecord] = []

    def create(self, release: ReleaseRecord) -> None:
        self.items.append(release)

    def list_for_run(self, run_id: str) -> list[ReleaseRecord]:
        return [r for r in self.items if r.run_id == run_id]

    def latest_for_run(self, run_id: str) -> Optional[ReleaseRecord]:
        matches = self.list_for_run(run_id)
        return max(matches, key=lambda r: r.version) if matches else None


class _FakeDefectiveRepo:
    def __init__(self) -> None:
        self.items: list[DefectiveQuestionResolution] = []

    def create(self, resolution: DefectiveQuestionResolution) -> None:
        self.items.append(resolution)

    def list_for_run(self, run_id: str) -> list[DefectiveQuestionResolution]:
        return [r for r in self.items if r.run_id == run_id]


class _FakeGradeAppealRepo:
    def __init__(self) -> None:
        self.store: dict[str, GradeAppeal] = {}

    def create(self, appeal: GradeAppeal) -> None:
        self.store[appeal.id] = appeal

    def get(self, appeal_id: str) -> Optional[GradeAppeal]:
        return self.store.get(appeal_id)

    def update(self, appeal_id: str, update: dict) -> None:
        current = self.store[appeal_id]
        self.store[appeal_id] = current.model_copy(update=update)

    def list_for_run(self, run_id: str) -> list[GradeAppeal]:
        return [a for a in self.store.values() if a.run_id == run_id]

    def list_for_student(self, run_id: str, uid: str) -> list[GradeAppeal]:
        return [a for a in self.store.values() if a.run_id == run_id and a.uid == uid]


def _service() -> GradebookReleaseService:
    svc = GradebookReleaseService.__new__(GradebookReleaseService)
    svc.releases = _FakeReleaseRepo()
    svc.defective_resolutions = _FakeDefectiveRepo()
    svc.grade_appeals = _FakeGradeAppealRepo()
    return svc


# ── release versioning ───────────────────────────────────────────────────────

def test_first_release_starts_at_version_one() -> None:
    svc = _service()
    release = svc.release_results(run_id=RUN_ID, released_by="fac1", note="Initial grades.")
    assert release.version == 1
    assert svc.latest_release(RUN_ID).id == release.id


def test_second_release_increments_version_and_keeps_history() -> None:
    svc = _service()
    first = svc.release_results(run_id=RUN_ID, released_by="fac1")
    second = svc.release_results(run_id=RUN_ID, released_by="fac1", note="Corrected after regrade.")
    assert second.version == 2
    assert svc.latest_release(RUN_ID).id == second.id
    assert len(svc.releases.list_for_run(RUN_ID)) == 2
    # The first release record is never deleted or edited.
    assert svc.releases.list_for_run(RUN_ID)[0].id == first.id


def test_no_release_yet_returns_none() -> None:
    svc = _service()
    assert svc.latest_release(RUN_ID) is None


# ── defective-question resolution ───────────────────────────────────────────

def test_resolve_defective_question_is_recorded() -> None:
    svc = _service()
    record = svc.resolve_defective_question(
        run_id=RUN_ID, question_id="q1", resolution="full_credit_all", reason="Bad test case.", resolved_by="fac1"
    )
    assert record.resolution == "full_credit_all"
    assert svc.list_defective_resolutions(RUN_ID) == [record]


# ── grade appeals ────────────────────────────────────────────────────────────

def test_submit_and_resolve_grade_appeal_upheld() -> None:
    svc = _service()
    appeal = svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="I deserve more credit.")
    assert appeal.status == GradeAppealStatus.PENDING

    resolved = svc.resolve_grade_appeal(appeal_id=appeal.id, uphold=True, resolved_by="fac1", resolution_notes="Correctly graded.")
    assert resolved.status == GradeAppealStatus.UPHELD
    assert resolved.version == 1


def test_resolve_grade_appeal_corrected_then_reversed_bumps_version() -> None:
    svc = _service()
    appeal = svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="Please recheck.")
    first = svc.resolve_grade_appeal(appeal_id=appeal.id, uphold=True, resolved_by="fac1", resolution_notes="Looks right.")
    assert first.version == 1

    reversed_ = svc.resolve_grade_appeal(appeal_id=appeal.id, uphold=False, resolved_by="fac2", resolution_notes="On review, an error was found.")
    assert reversed_.status == GradeAppealStatus.CORRECTED
    assert reversed_.version == 2


def test_resolve_unknown_appeal_raises_lookup_error() -> None:
    svc = _service()
    with pytest.raises(LookupError):
        svc.resolve_grade_appeal(appeal_id="missing", uphold=True, resolved_by="fac1", resolution_notes="n/a")


def test_list_grade_appeals_for_student_filters_by_uid() -> None:
    svc = _service()
    svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="a")
    svc.submit_grade_appeal(run_id=RUN_ID, uid="u2", question_id="q1", student_statement="b")
    assert len(svc.list_grade_appeals_for_student(RUN_ID, "u1")) == 1
    assert len(svc.list_grade_appeals_for_run(RUN_ID)) == 2
