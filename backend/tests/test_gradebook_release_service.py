"""Gradebook release service tests: versioned releases that freeze what students
see, correction reasons, incomplete-release acknowledgement, defective-question
resolution bookkeeping, and grade-appeal guards/resolution — using fake
in-memory repos via the `Service.__new__` bypass pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from app.domain.exams.models import RunStatus
from app.domain.gradebook.errors import (
    GradeAppealNotAllowedError,
    ReleaseIncompleteError,
    ReleaseReasonRequiredError,
    ResultsNotReleasedError,
)
from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    GradeAppeal,
    GradeAppealStatus,
    IntegrityDisposition,
    QuestionOutcome,
    ReleaseRecord,
    StudentGradebookRow,
    StudentQuestionResult,
)
from app.domain.gradebook.release_service import MAX_GRADE_APPEALS_PER_STUDENT, GradebookReleaseService

RUN_ID = "run-1"


class _FakeReleaseRepo:
    def __init__(self) -> None:
        self.items: list[ReleaseRecord] = []
        self.lose_next_creates = 0  # simulate another release taking the version number first

    def create(self, release: ReleaseRecord) -> None:
        self.items.append(release)

    def create_if_absent(self, release: ReleaseRecord) -> bool:
        if self.lose_next_creates:
            self.lose_next_creates -= 1
            self.items.append(release.model_copy(update={"id": release.id + "-other", "snapshot_id": "someone-elses"}))
            return False
        if any(r.id == release.id for r in self.items):
            return False
        self.items.append(release)
        return True

    def list_for_run(self, run_id: str) -> list[ReleaseRecord]:
        return [r for r in self.items if r.run_id == run_id]

    def latest_for_run(self, run_id: str) -> Optional[ReleaseRecord]:
        matches = self.list_for_run(run_id)
        return max(matches, key=lambda r: r.version) if matches else None


class _FakeSnapshotRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], StudentGradebookRow] = {}

    def put(self, snapshot_id: str, run_id: str, row: StudentGradebookRow) -> None:
        # Stored as plain data, like Firestore: later edits to the live row object can never reach a snapshot.
        self.store[(snapshot_id, row.uid)] = StudentGradebookRow.model_validate(row.model_dump(mode="json"))

    def get(self, snapshot_id: str, uid: str) -> Optional[StudentGradebookRow]:
        return self.store.get((snapshot_id, uid))


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

    def create_if_absent(self, appeal: GradeAppeal) -> bool:
        if appeal.id in self.store:
            return False
        self.store[appeal.id] = appeal
        return True

    def get(self, appeal_id: str) -> Optional[GradeAppeal]:
        return self.store.get(appeal_id)

    def update(self, appeal_id: str, update: dict) -> None:
        current = self.store[appeal_id]
        self.store[appeal_id] = GradeAppeal.model_validate({**current.model_dump(mode="json"), **update})

    def list_for_run(self, run_id: str) -> list[GradeAppeal]:
        return [a for a in self.store.values() if a.run_id == run_id]

    def list_for_student(self, run_id: str, uid: str) -> list[GradeAppeal]:
        return [a for a in self.store.values() if a.run_id == run_id and a.uid == uid]


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list = []

    def append(self, event) -> None:
        self.events.append(event)


class _FakeAggregation:
    """The live gradebook: tests change `rows` after a release to simulate a regrade or a defective-question fix."""

    def __init__(self, rows: list[StudentGradebookRow], status: RunStatus) -> None:
        self.rows = {r.uid: r for r in rows}
        self.status = status

    def get_run_and_revision(self, run_id: str):
        return SimpleNamespace(status=self.status, enrolled_uids=list(self.rows)), SimpleNamespace(id="rev-1", slot_count=1)

    def list_all_rows(self, run_id: str) -> list[StudentGradebookRow]:
        return list(self.rows.values())

    def build_student_row(self, run, revision, uid: str) -> StudentGradebookRow:
        return self.rows[uid]


def _question(uid: str, *, score: float = 5.0, outcome=QuestionOutcome.PARTIALLY_SOLVED, question_id: str = "q1") -> StudentQuestionResult:
    return StudentQuestionResult(
        uid=uid, question_id=question_id, slot_index=0, item_type="coding", presented=True, outcome=outcome,
        weighted_score=score, max_score=10.0, tests_passed=1, tests_total=2,
        submission_hash="HASH-INTERNAL", runtime_image="IMAGE-INTERNAL", test_suite_version=7,
    )


def _row(uid: str, *, score: float = 5.0, status: str = "finalized", outcome=QuestionOutcome.PARTIALLY_SOLVED) -> StudentGradebookRow:
    return StudentGradebookRow(
        uid=uid, attempt_status=status, finalized=status == "finalized", raw_score=score, raw_max_score=10.0,
        integrity_disposition=IntegrityDisposition.CLEAN, warning_count=0,
        question_results=[_question(uid, score=score, outcome=outcome)],
    )


def _service(rows=None, *, status: RunStatus = RunStatus.ENDED) -> GradebookReleaseService:
    svc = GradebookReleaseService.__new__(GradebookReleaseService)
    svc.releases = _FakeReleaseRepo()
    svc.snapshots = _FakeSnapshotRepo()
    svc.defective_resolutions = _FakeDefectiveRepo()
    svc.grade_appeals = _FakeGradeAppealRepo()
    svc.aggregation = _FakeAggregation(rows if rows is not None else [_row("u1"), _row("u2")], status)
    svc._audit = _FakeAudit()
    return svc


# ── release versioning ───────────────────────────────────────────────────────

def test_first_release_starts_at_version_one_and_is_audited() -> None:
    svc = _service()
    release = svc.release_results(run_id=RUN_ID, released_by="fac1", note="Initial grades.")
    assert release.version == 1 and release.row_count == 2 and release.snapshot_id
    assert not release.acknowledged_incomplete
    assert svc.latest_release(RUN_ID).id == release.id
    [event] = svc._audit.events
    assert event.kind == "exam_results_released" and event.actor_uid == "fac1"
    assert event.detail["version"] == 1 and event.detail["reason"] == "Initial grades."


def test_a_first_release_needs_no_reason() -> None:
    assert _service().release_results(run_id=RUN_ID, released_by="fac1").version == 1


def test_a_corrected_re_release_must_state_the_reason_and_nothing_is_written_without_it() -> None:
    svc = _service()
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    for blank in ("", "   "):
        with pytest.raises(ReleaseReasonRequiredError):
            svc.release_results(run_id=RUN_ID, released_by="fac1", note=blank)
    assert len(svc.releases.list_for_run(RUN_ID)) == 1
    assert len(svc.snapshots.store) == 2  # only the first release's two rows
    assert len(svc._audit.events) == 1


def test_second_release_increments_version_and_keeps_history() -> None:
    svc = _service()
    first = svc.release_results(run_id=RUN_ID, released_by="fac1")
    second = svc.release_results(run_id=RUN_ID, released_by="fac1", note="Corrected after regrade.")
    assert second.version == 2 and second.note == "Corrected after regrade."
    assert svc.latest_release(RUN_ID).id == second.id
    assert [r.id for r in svc.releases.list_for_run(RUN_ID)] == [first.id, second.id]
    assert first.snapshot_id != second.snapshot_id  # a correction never edits the earlier snapshot


def test_a_release_that_loses_the_version_race_retries_with_the_next_number() -> None:
    svc = _service()
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    svc.releases.lose_next_creates = 1
    release = svc.release_results(run_id=RUN_ID, released_by="fac1", note="Corrected.")
    assert release.version == 3  # v2 was taken by the other releaser; this one is never silently dropped or duplicated
    assert sorted(r.version for r in svc.releases.list_for_run(RUN_ID)) == [1, 2, 3]


def test_no_release_yet_returns_none() -> None:
    svc = _service()
    assert svc.latest_release(RUN_ID) is None


# ── incomplete releases ──────────────────────────────────────────────────────

@pytest.mark.parametrize("rows, status, live, pending", [
    ([_row("u1", status="in_progress")], RunStatus.ENDED, 1, 0),
    ([_row("u1", status="blocked")], RunStatus.ENDED, 1, 0),
    ([_row("u1", outcome=QuestionOutcome.PENDING)], RunStatus.ENDED, 0, 1),
    ([_row("u1")], RunStatus.RELEASED, 0, 0),  # the exam is still running
    ([_row("u1")], RunStatus.PAUSED, 0, 0),
])
def test_releasing_an_incomplete_gradebook_needs_an_explicit_acknowledgement(rows, status, live, pending) -> None:
    svc = _service(rows, status=status)
    with pytest.raises(ReleaseIncompleteError):
        svc.release_results(run_id=RUN_ID, released_by="fac1")
    assert svc.releases.list_for_run(RUN_ID) == [] and svc.snapshots.store == {} and svc._audit.events == []
    with pytest.raises(ResultsNotReleasedError):
        svc.student_result(RUN_ID, "u1")

    release = svc.release_results(run_id=RUN_ID, released_by="fac1", acknowledge_incomplete=True)
    assert release.acknowledged_incomplete
    assert (release.live_attempt_count, release.pending_question_count) == (live, pending)
    assert svc._audit.events[0].detail["acknowledged_incomplete"] is True


def test_a_complete_gradebook_releases_without_acknowledgement() -> None:
    svc = _service([_row("u1"), _row("u2", status="submitted")], status=RunStatus.CLOSED)
    assert svc.release_results(run_id=RUN_ID, released_by="fac1").acknowledged_incomplete is False


# ── what a student is shown ─────────────────────────────────────────────────

def test_nothing_is_shown_before_a_release() -> None:
    svc = _service()
    with pytest.raises(ResultsNotReleasedError):
        svc.student_result(RUN_ID, "u1")


def test_students_see_the_frozen_release_not_the_live_gradebook_until_a_new_version_is_released() -> None:
    svc = _service()
    svc.release_results(run_id=RUN_ID, released_by="fac1")

    # A regrade / defective-question resolution changes the LIVE gradebook after the release...
    svc.aggregation.rows["u1"] = _row("u1", score=10.0, outcome=QuestionOutcome.FULLY_SOLVED)
    assert svc.aggregation.rows["u1"].raw_score == 10.0

    # ...but the student still sees exactly what was released.
    row, release = svc.student_result(RUN_ID, "u1")
    assert (row.raw_score, release.version) == (5.0, 1)

    svc.release_results(run_id=RUN_ID, released_by="fac1", note="Hidden test fixed; regraded.")
    row, release = svc.student_result(RUN_ID, "u1")
    assert (row.raw_score, release.version) == (10.0, 2)


def test_a_student_only_ever_gets_their_own_row() -> None:
    svc = _service([_row("u1", score=1.0), _row("u2", score=9.0)])
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    assert svc.student_result(RUN_ID, "u1")[0].uid == "u1"
    assert svc.student_result(RUN_ID, "u2")[0].raw_score == 9.0
    with pytest.raises(ResultsNotReleasedError):
        svc.student_result(RUN_ID, "late-joiner")  # enrolled after the release: no row in it


def test_the_student_view_hides_internal_grading_provenance() -> None:
    svc = _service()
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    question = svc.student_result(RUN_ID, "u1")[0].question_results[0]
    assert (question.submission_hash, question.runtime_image, question.test_suite_version) == (None, None, None)
    assert (question.tests_passed, question.tests_total, question.weighted_score) == (1, 2, 5.0)
    # The stored snapshot itself is untouched (faculty/audit still have it).
    assert svc.snapshots.store[(svc.latest_release(RUN_ID).snapshot_id, "u1")].question_results[0].submission_hash == "HASH-INTERNAL"


def test_a_release_made_before_snapshots_existed_is_still_served_live() -> None:
    svc = _service()
    legacy = ReleaseRecord(id="legacy", run_id=RUN_ID, version=1, released_by="fac1", released_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    svc.releases.create(legacy)
    row, release = svc.student_result(RUN_ID, "u1")
    assert row.raw_score == 5.0 and release.snapshot_id is None
    with pytest.raises(ResultsNotReleasedError):
        svc.student_result(RUN_ID, "not-enrolled")


# ── defective-question resolution ───────────────────────────────────────────

def test_resolve_defective_question_is_recorded_and_audited() -> None:
    svc = _service()
    record = svc.resolve_defective_question(
        run_id=RUN_ID, question_id="q1", resolution="full_credit_all", reason="Bad test case.", resolved_by="fac1"
    )
    assert record.resolution == "full_credit_all"
    assert svc.list_defective_resolutions(RUN_ID) == [record]
    [event] = svc._audit.events
    assert event.kind == "exam_question_resolved_defective" and event.detail["reason"] == "Bad test case."


def test_a_defective_question_resolution_does_not_change_what_students_already_see() -> None:
    svc = _service()
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    svc.resolve_defective_question(run_id=RUN_ID, question_id="q1", resolution="full_credit_all", reason="Bad key.", resolved_by="fac1")
    assert svc.student_result(RUN_ID, "u1")[0].raw_score == 5.0  # until a corrected version is released


# ── grade appeals ────────────────────────────────────────────────────────────

def _released() -> GradebookReleaseService:
    svc = _service()
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    return svc


def test_submit_and_resolve_grade_appeal_upheld() -> None:
    svc = _released()
    appeal = svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="I deserve more credit.")
    assert appeal.status == GradeAppealStatus.PENDING and appeal.release_version == 1

    resolved = svc.resolve_grade_appeal(appeal_id=appeal.id, uphold=True, resolved_by="fac1", resolution_notes="Correctly graded.")
    assert resolved.status == GradeAppealStatus.UPHELD
    assert resolved.version == 1
    assert svc._audit.events[-1].kind == "exam_grade_appeal_resolved"


def test_resolve_grade_appeal_corrected_then_reversed_bumps_version() -> None:
    svc = _released()
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
    svc = _released()
    svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="a")
    svc.submit_grade_appeal(run_id=RUN_ID, uid="u2", question_id="q1", student_statement="b")
    assert len(svc.list_grade_appeals_for_student(RUN_ID, "u1")) == 1
    assert len(svc.list_grade_appeals_for_run(RUN_ID)) == 2


def test_an_appeal_needs_a_released_result() -> None:
    svc = _service()
    with pytest.raises(GradeAppealNotAllowedError):
        svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="early")
    assert svc.grade_appeals.store == {}


@pytest.mark.parametrize("uid, question_id", [("u1", "q-not-mine"), ("u1", ""), ("u1", "rev-1:slot:99"), ("stranger", "q1")])
def test_an_appeal_must_name_a_question_in_the_students_own_released_row(uid, question_id) -> None:
    svc = _released()
    with pytest.raises(GradeAppealNotAllowedError):
        svc.submit_grade_appeal(run_id=RUN_ID, uid=uid, question_id=question_id, student_statement="x")
    assert svc.grade_appeals.store == {}


def test_only_one_open_appeal_per_question_but_a_new_one_after_it_is_resolved() -> None:
    svc = _released()
    first = svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="a")
    with pytest.raises(GradeAppealNotAllowedError):
        svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="again")
    svc.resolve_grade_appeal(appeal_id=first.id, uphold=True, resolved_by="fac1", resolution_notes="no change")
    second = svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="new evidence")
    assert second.id != first.id and len(svc.grade_appeals.list_for_student(RUN_ID, "u1")) == 2


def test_two_simultaneous_submissions_for_the_same_question_store_only_one() -> None:
    """Both pass the 'no open appeal' check, then collide on the id derived from the same prior-appeal count."""
    svc = _released()
    original = svc.grade_appeals.list_for_student
    svc.grade_appeals.list_for_student = lambda run_id, uid: []  # both racers read an empty list
    first = svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="a")
    with pytest.raises(GradeAppealNotAllowedError):
        svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id="q1", student_statement="b")
    svc.grade_appeals.list_for_student = original
    assert list(svc.grade_appeals.store) == [first.id]


def test_a_student_cannot_file_an_unbounded_number_of_appeals() -> None:
    questions = [_question("u1", question_id=f"q{i}") for i in range(MAX_GRADE_APPEALS_PER_STUDENT + 1)]
    row = _row("u1").model_copy(update={"question_results": questions})
    svc = _service([row])
    svc.release_results(run_id=RUN_ID, released_by="fac1")
    for i in range(MAX_GRADE_APPEALS_PER_STUDENT):
        svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id=f"q{i}", student_statement="x")
    with pytest.raises(GradeAppealNotAllowedError):
        svc.submit_grade_appeal(run_id=RUN_ID, uid="u1", question_id=f"q{MAX_GRADE_APPEALS_PER_STUDENT}", student_statement="x")
