"""Release versioning, defective-question resolution, and grade-appeal
workflow. Thin imperative shell — all branching logic lives in
`decisions.py`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.models import AttemptStatus, RunStatus
from app.domain.gradebook.aggregation_service import GradebookAggregationService
from app.domain.gradebook.decisions import decide_next_release_version, decide_resolve_grade_appeal, student_row_view
from app.domain.gradebook.errors import (
    GradebookDomainError,
    GradeAppealNotAllowedError,
    ReleaseIncompleteError,
    ReleaseReasonRequiredError,
    ResultsNotReleasedError,
)
from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    GradeAppeal,
    GradeAppealStatus,
    QuestionOutcome,
    ReleaseRecord,
    StudentGradebookRow,
)
from app.domain.gradebook.repository import (
    DefectiveQuestionResolutionRepository,
    GradeAppealRepository,
    ReleaseRecordRepository,
    ReleaseSnapshotRepository,
)

# A student may keep several disputes open across different questions, but not an unbounded number.
MAX_GRADE_APPEALS_PER_STUDENT = 10

_LIVE_ATTEMPT_STATUSES = frozenset({AttemptStatus.IN_PROGRESS.value, AttemptStatus.BLOCKED.value})
_FINISHED_RUN_STATUSES = frozenset({RunStatus.ENDED, RunStatus.CLOSED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GradebookReleaseService:
    """Owns release versions, defective-question resolutions, and grade
    appeals — the three "auditable lineage" write paths in the gradebook
    domain. Never touches the underlying `GradingResult`/`McqGradeResult`/
    `ShortAnswerReview` records (contract: 'never overwriting original
    submissions')."""

    def __init__(self, db) -> None:
        self._db = db
        self.releases = ReleaseRecordRepository(db)
        self.snapshots = ReleaseSnapshotRepository(db)
        self.defective_resolutions = DefectiveQuestionResolutionRepository(db)
        self.grade_appeals = GradeAppealRepository(db)
        self.aggregation = GradebookAggregationService(db)
        self._audit = AuthzAuditRepository(db)

    # ── release versioning ───────────────────────────────────────────────

    def release_results(
        self, *, run_id: str, released_by: str, note: str = "", acknowledge_incomplete: bool = False
    ) -> ReleaseRecord:
        """Always creates a NEW version — v1 the first time, v2+ for every
        subsequent re-release (a correction). A release freezes what the
        students will see: every enrolled student's row is copied into a
        snapshot first, then the release record that points at it is
        created. Students are shown that snapshot, never the live
        gradebook, so a regrade or a defective-question resolution changes
        nothing a student sees until a new version is released.

        A correction (v2+) must say why. Releasing while the run is still
        open, attempts are still live or answers are still ungraded needs
        `acknowledge_incomplete`, and the record keeps those counts."""
        note = note.strip()
        previous = self.releases.list_for_run(run_id)
        if previous and not note:
            raise ReleaseReasonRequiredError("a corrected re-release must state the reason for the correction")

        run, _revision = self.aggregation.get_run_and_revision(run_id)
        rows = self.aggregation.list_all_rows(run_id)
        live = sum(1 for row in rows if row.attempt_status in _LIVE_ATTEMPT_STATUSES)
        pending = sum(1 for row in rows for q in row.question_results if q.outcome == QuestionOutcome.PENDING)
        run_open = run.status not in _FINISHED_RUN_STATUSES
        incomplete = bool(live or pending or run_open)
        if incomplete and not acknowledge_incomplete:
            raise ReleaseIncompleteError(
                f"{live} attempt(s) still in progress, {pending} answer(s) not yet graded"
                f"{', and the exam run has not ended' if run_open else ''}; confirm to release anyway"
            )

        snapshot_id = uuid4().hex
        for row in rows:
            self.snapshots.put(snapshot_id, run_id, row)

        for _ in range(3):  # another release may take the version number between the read and the create
            previous = self.releases.list_for_run(run_id)
            if previous and not note:
                raise ReleaseReasonRequiredError("a corrected re-release must state the reason for the correction")
            version = decide_next_release_version(previous)
            release = ReleaseRecord(
                id=f"{run_id}-v{version}", run_id=run_id, version=version, released_by=released_by,
                released_at=_utc_now(), note=note, snapshot_id=snapshot_id, row_count=len(rows),
                pending_question_count=pending, live_attempt_count=live, acknowledged_incomplete=incomplete,
            )
            if self.releases.create_if_absent(release):
                self._audit.append(build_audit_event(
                    "exam_results_released", actor_uid=released_by,
                    detail={
                        "run_id": run_id, "version": version, "reason": note[:500], "row_count": len(rows),
                        "pending_question_count": pending, "live_attempt_count": live, "acknowledged_incomplete": incomplete,
                    },
                ))
                return release
        raise GradebookDomainError("another release was being created at the same time; try again")

    def latest_release(self, run_id: str):
        return self.releases.latest_for_run(run_id)

    def _released_row(self, release: ReleaseRecord, uid: str) -> StudentGradebookRow | None:
        """The row a student was shown for this release: the frozen snapshot, or — for a release made before
        snapshots existed — the live row, as before."""
        if release.snapshot_id:
            return self.snapshots.get(release.snapshot_id, uid)
        run, revision = self.aggregation.get_run_and_revision(release.run_id)
        if uid not in run.enrolled_uids:
            return None
        return self.aggregation.build_student_row(run, revision, uid)

    def student_result(self, run_id: str, uid: str) -> tuple[StudentGradebookRow, ReleaseRecord]:
        release = self.latest_release(run_id)
        if release is None:
            raise ResultsNotReleasedError(f"run {run_id} results have not been released yet")
        row = self._released_row(release, uid)
        if row is None:
            raise ResultsNotReleasedError(f"no released result for this student in version {release.version}")
        return student_row_view(row), release

    # ── defective-question resolution ───────────────────────────────────

    def resolve_defective_question(
        self, *, run_id: str, question_id: str, resolution: str, reason: str, resolved_by: str
    ) -> DefectiveQuestionResolution:
        """A single, uniform, auditable action across every assigned
        student for this question — never N separate per-student edits.
        Applied at gradebook READ time by `apply_defective_question_resolution`
        (aggregation_service.py). It changes the live gradebook at once but
        what students see only changes when a new version is released."""
        record = DefectiveQuestionResolution(
            id=uuid4().hex, run_id=run_id, question_id=question_id, resolution=resolution,
            reason=reason, resolved_by=resolved_by, resolved_at=_utc_now(),
        )
        self.defective_resolutions.create(record)
        self._audit.append(build_audit_event(
            "exam_question_resolved_defective", actor_uid=resolved_by,
            detail={"run_id": run_id, "question_id": question_id, "resolution": resolution, "reason": reason[:500]},
        ))
        return record

    def list_defective_resolutions(self, run_id: str) -> list[DefectiveQuestionResolution]:
        return self.defective_resolutions.list_for_run(run_id)

    # ── grade appeals ────────────────────────────────────────────────────

    def submit_grade_appeal(self, *, run_id: str, uid: str, question_id: str, student_statement: str) -> GradeAppeal:
        """Only against a released result, only for a question that is in the student's released row, one open
        appeal per question, and a bounded number per student. The appeal id is derived from how many appeals the
        student already has for that question, so two simultaneous submissions collide on one id and only one is
        stored."""
        release = self.latest_release(run_id)
        if release is None:
            raise GradeAppealNotAllowedError("results have not been released yet")
        row = self._released_row(release, uid)
        if row is None or question_id not in {q.question_id for q in row.question_results}:
            raise GradeAppealNotAllowedError("that question is not part of your released results")

        existing = self.grade_appeals.list_for_student(run_id, uid)
        for_question = [a for a in existing if a.question_id == question_id]
        if any(a.status == GradeAppealStatus.PENDING for a in for_question):
            raise GradeAppealNotAllowedError("you already have an open appeal for this question")
        if len(existing) >= MAX_GRADE_APPEALS_PER_STUDENT:
            raise GradeAppealNotAllowedError("the limit on grade appeals for this exam has been reached")

        appeal_id = hashlib.sha256(f"{run_id}|{uid}|{question_id}|{len(for_question)}".encode("utf-8")).hexdigest()[:32]
        appeal = GradeAppeal(
            id=appeal_id, run_id=run_id, uid=uid, question_id=question_id,
            student_statement=student_statement, created_at=_utc_now(), release_version=release.version,
        )
        if not self.grade_appeals.create_if_absent(appeal):
            raise GradeAppealNotAllowedError("you already have an open appeal for this question")
        return appeal

    def resolve_grade_appeal(self, *, appeal_id: str, uphold: bool, resolved_by: str, resolution_notes: str) -> GradeAppeal:
        """Bookkeeping only — mirrors `AppealsService.resolve_appeal`
        (Prompt 8): this call alone never changes a grade. If the appeal is
        granted (`uphold=False`, i.e. the original grade was wrong), faculty
        separately triggers the appropriate regrade (`JudgeService.regrade`
        for coding, `.regrade_mcq` for MCQ, `.submit_short_answer_review`
        for short answer) and then releases a new version — the same
        explicit-and-separate pattern proctoring appeals already use for
        `resolve_block`."""
        appeal = self.grade_appeals.get(appeal_id)
        if appeal is None:
            raise LookupError(f"grade appeal {appeal_id} not found")
        resolved = decide_resolve_grade_appeal(appeal, uphold=uphold, resolved_by=resolved_by, resolution_notes=resolution_notes, now=_utc_now())
        self.grade_appeals.update(
            appeal_id,
            {
                "status": resolved.status.value, "resolved_by": resolved.resolved_by,
                "resolved_at": resolved.resolved_at.isoformat(), "resolution_notes": resolved.resolution_notes,
                "version": resolved.version,
            },
        )
        self._audit.append(build_audit_event(
            "exam_grade_appeal_resolved", uid=appeal.uid, actor_uid=resolved_by,
            detail={"run_id": appeal.run_id, "question_id": appeal.question_id, "uphold": uphold, "version": resolved.version},
        ))
        return self.grade_appeals.get(appeal_id)

    def list_grade_appeals_for_run(self, run_id: str) -> list[GradeAppeal]:
        return self.grade_appeals.list_for_run(run_id)

    def list_grade_appeals_for_student(self, run_id: str, uid: str) -> list[GradeAppeal]:
        return self.grade_appeals.list_for_student(run_id, uid)
