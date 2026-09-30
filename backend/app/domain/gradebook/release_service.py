"""Release versioning, defective-question resolution, and grade-appeal
workflow. Thin imperative shell — all branching logic lives in
`decisions.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.domain.gradebook.decisions import decide_next_release_version, decide_resolve_grade_appeal
from app.domain.gradebook.models import DefectiveQuestionResolution, GradeAppeal, ReleaseRecord
from app.domain.gradebook.repository import (
    DefectiveQuestionResolutionRepository,
    GradeAppealRepository,
    ReleaseRecordRepository,
)


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
        self.defective_resolutions = DefectiveQuestionResolutionRepository(db)
        self.grade_appeals = GradeAppealRepository(db)

    # ── release versioning ───────────────────────────────────────────────

    def release_results(self, *, run_id: str, released_by: str, note: str = "") -> ReleaseRecord:
        """Always creates a NEW version — v1 the first time, v2+ for every
        subsequent re-release (e.g. after a regrade). Students only ever see
        results once at least one release exists (enforced by the route
        layer's `ResultsNotReleasedError`)."""
        previous = self.releases.list_for_run(run_id)
        version = decide_next_release_version(previous)
        release = ReleaseRecord(id=uuid4().hex, run_id=run_id, version=version, released_by=released_by, released_at=_utc_now(), note=note)
        self.releases.create(release)
        return release

    def latest_release(self, run_id: str):
        return self.releases.latest_for_run(run_id)

    # ── defective-question resolution ───────────────────────────────────

    def resolve_defective_question(
        self, *, run_id: str, question_id: str, resolution: str, reason: str, resolved_by: str
    ) -> DefectiveQuestionResolution:
        """A single, uniform, auditable action across every assigned
        student for this question — never N separate per-student edits.
        Applied at gradebook READ time by `apply_defective_question_resolution`
        (aggregation_service.py), so this call alone does not change any
        already-computed row until the gradebook is next read."""
        record = DefectiveQuestionResolution(
            id=uuid4().hex, run_id=run_id, question_id=question_id, resolution=resolution,
            reason=reason, resolved_by=resolved_by, resolved_at=_utc_now(),
        )
        self.defective_resolutions.create(record)
        return record

    def list_defective_resolutions(self, run_id: str) -> list[DefectiveQuestionResolution]:
        return self.defective_resolutions.list_for_run(run_id)

    # ── grade appeals ────────────────────────────────────────────────────

    def submit_grade_appeal(self, *, run_id: str, uid: str, question_id: str, student_statement: str) -> GradeAppeal:
        appeal = GradeAppeal(
            id=uuid4().hex, run_id=run_id, uid=uid, question_id=question_id,
            student_statement=student_statement, created_at=_utc_now(),
        )
        self.grade_appeals.create(appeal)
        return appeal

    def resolve_grade_appeal(self, *, appeal_id: str, uphold: bool, resolved_by: str, resolution_notes: str) -> GradeAppeal:
        """Bookkeeping only — mirrors `AppealsService.resolve_appeal`
        (Prompt 8): this call alone never changes a grade. If the appeal is
        granted (`uphold=False`, i.e. the original grade was wrong), faculty
        separately triggers the appropriate regrade (`JudgeService.regrade`
        for coding, `.regrade_mcq` for MCQ, `.submit_short_answer_review`
        for short answer) — the same explicit-and-separate pattern proctoring
        appeals already use for `resolve_block`."""
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
        return self.grade_appeals.get(appeal_id)

    def list_grade_appeals_for_run(self, run_id: str) -> list[GradeAppeal]:
        return self.grade_appeals.list_for_run(run_id)

    def list_grade_appeals_for_student(self, run_id: str, uid: str) -> list[GradeAppeal]:
        return self.grade_appeals.list_for_student(run_id, uid)
