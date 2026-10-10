"""Deterministic, rule-based learning feedback.

Never a live LLM call by default (contract: "Optional AI signals remain
disabled until locally evaluated."). Assembled ONLY from a released run's
own gradebook rows and a question's OWN `supporting_anchors` (the same
faculty-approved KT citations already attached to that candidate at
generation time, Prompt 5) — never hidden tests, never another student's
submissions, and never anything from a run that hasn't been released yet.

`FeedbackService` is provider-pluggable (see `feedback_provider.py`,
"Backend model prompt C") but defaults to `TemplateFeedbackProvider` below,
preserving this exact behavior for every institution that never configures
a real provider.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.gradebook.feedback_provider import (
    MAX_GAPS,
    MAX_MISSING_MATERIAL,
    MAX_PRACTICE_STEPS,
    MAX_STRENGTHS,
    FeedbackProvider,
    FeedbackProviderResult,
    sanitize_feedback_result,
)
from app.domain.gradebook.models import (
    FailureCategory,
    LearningFeedback,
    PracticeStep,
    QuestionOutcome,
    StudentGradebookRow,
    StudentQuestionResult,
)
from app.domain.gradebook.aggregation_service import GradebookAggregationService
from app.domain.gradebook.release_service import GradebookReleaseService

_FAILURE_HINTS: dict[FailureCategory, str] = {
    FailureCategory.COMPILE_ERROR: "Your submission didn't compile/parse — re-check syntax before submitting.",
    FailureCategory.RUNTIME_ERROR: "Your program raised a runtime error on at least one case — check edge-case inputs (empty, zero, negative).",
    FailureCategory.TIME_LIMIT_EXCEEDED: "Your solution was too slow on at least one case — consider a more efficient algorithm or data structure.",
    FailureCategory.MEMORY_LIMIT_EXCEEDED: "Your solution used too much memory on at least one case — look for unnecessarily large data structures.",
    FailureCategory.OUTPUT_LIMIT_EXCEEDED: "Your program printed far more output than expected — check for an unintended loop or leftover debug output.",
    FailureCategory.WRONG_ANSWER: "Your output didn't match the expected result on at least one case — re-check your logic against the examples.",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_learning_feedback(
    row: StudentGradebookRow, *, run_id: str, supporting_anchors_by_question: dict[str, list[str]],
    objective_ids_by_question: dict[str, list[str]] | None = None,
) -> LearningFeedback:
    """Pure — takes an already-built gradebook row (itself already
    release-gated by the caller) and produces template-based feedback.
    PENDING questions are silently excluded: grading isn't final, so there
    is nothing honest to say about them yet. At most `MAX_GAPS` gaps are
    reported, the ones that cost the most marks first, and the practice
    steps, anchors and missing-material notes follow those gaps."""
    objectives = objective_ids_by_question or {}
    strengths: list[str] = []
    gaps: list[tuple[float, str, str, StudentQuestionResult]] = []

    for qr in row.question_results:
        label = f"Question {qr.slot_index + 1}"
        if qr.outcome == QuestionOutcome.FULLY_SOLVED:
            strengths.append(f"{label}: fully solved ({qr.weighted_score:g}/{qr.max_score:g}).")
        elif qr.outcome == QuestionOutcome.PARTIALLY_SOLVED:
            gaps.append((qr.max_score - qr.weighted_score, label, f"{label}: partially solved ({qr.weighted_score:g}/{qr.max_score:g}).", qr))
        elif qr.outcome == QuestionOutcome.BLANK:
            gaps.append((qr.max_score - qr.weighted_score, label, f"{label}: not attempted.", qr))

    gap_lines: list[str] = []
    practice_steps: list[str] = []
    practice_sequence: list[PracticeStep] = []
    source_anchors: list[str] = []
    missing_material: list[str] = []
    for _lost, label, line, qr in sorted(gaps, key=lambda g: -g[0])[:MAX_GAPS]:
        gap_lines.append(line)
        question_objectives = objectives.get(qr.question_id, [])
        hint = None
        if qr.outcome == QuestionOutcome.PARTIALLY_SOLVED and qr.failure_category and qr.failure_category != FailureCategory.NONE:
            hint = _FAILURE_HINTS[qr.failure_category]
        if question_objectives:
            step = hint or "Review the supporting material for this objective, then retry a similar question."
            for objective_id in question_objectives:
                practice_sequence.append(PracticeStep(objective_id=objective_id, step=step))
                practice_steps.append(f"Objective {objective_id}: {step}")
        elif hint:
            practice_steps.append(hint)

        anchors = supporting_anchors_by_question.get(qr.question_id, [])
        if anchors:
            source_anchors.extend(anchors)
        else:
            where = f" (objective {', '.join(question_objectives)})" if question_objectives else ""
            missing_material.append(f"{label}{where}: no supporting material on file to recommend.")

    return LearningFeedback(
        run_id=run_id, uid=row.uid, strengths=strengths[:MAX_STRENGTHS], gaps=gap_lines,
        practice_steps=practice_steps[:MAX_PRACTICE_STEPS], practice_sequence=practice_sequence[:MAX_PRACTICE_STEPS],
        source_anchors=sorted(set(source_anchors)), missing_material=missing_material[:MAX_MISSING_MATERIAL],
        generated_at=_utc_now(),
    )


class TemplateFeedbackProvider:
    """The real, permanent default — wraps `build_learning_feedback` above.
    Not a placeholder/fake; this is what every institution gets unless they
    explicitly configure a real provider (see `feedback_provider.py`)."""

    def generate_feedback(
        self,
        row: StudentGradebookRow,
        *,
        run_id: str,
        anchors_by_question: dict[str, list[str]],
        objective_ids_by_question: dict[str, list[str]],
    ) -> FeedbackProviderResult:
        feedback = build_learning_feedback(
            row, run_id=run_id, supporting_anchors_by_question=anchors_by_question,
            objective_ids_by_question=objective_ids_by_question,
        )
        return FeedbackProviderResult(
            strengths=feedback.strengths,
            gaps=feedback.gaps,
            practice_steps=feedback.practice_steps,
            practice_sequence=feedback.practice_sequence,
            source_anchors=feedback.source_anchors,
            missing_material=feedback.missing_material,
        )


class FeedbackService:
    def __init__(self, db, provider: FeedbackProvider | None = None) -> None:
        self._db = db
        self._release_service = GradebookReleaseService(db)
        self._aggregation = GradebookAggregationService(db)
        self._provider = provider or TemplateFeedbackProvider()

    def build_feedback_for_student(self, run_id: str, uid: str) -> LearningFeedback:
        # The frozen row of the latest release, exactly what /results shows - never the live gradebook,
        # so a later regrade or defective-question resolution cannot leak into feedback before it is released.
        row, _release = self._release_service.student_result(run_id, uid)

        anchors_by_question: dict[str, list[str]] = {}
        objective_ids_by_question: dict[str, list[str]] = {}
        for qr in row.question_results:
            candidate_id = self._aggregation.assignments.get_assignment(run_id=run_id, uid=uid, slot_index=qr.slot_index)
            candidate = self._aggregation.generation.get_candidate(candidate_id) if candidate_id else None
            if candidate is not None:
                anchors_by_question[qr.question_id] = [f"{a.source_id}:{a.anchor_id}" for a in candidate.supporting_anchors]
                objective_ids_by_question[qr.question_id] = candidate.objective_ids

        known_anchor_ids = {anchor for anchors in anchors_by_question.values() for anchor in anchors}
        known_objective_ids = {o for objectives in objective_ids_by_question.values() for o in objectives}
        from_model = not isinstance(self._provider, TemplateFeedbackProvider)
        try:
            result = self._provider.generate_feedback(
                row, run_id=run_id, anchors_by_question=anchors_by_question,
                objective_ids_by_question=objective_ids_by_question,
            )
        except Exception:
            # A configured-but-unavailable real provider must never break this student-facing page:
            # fall back to the deterministic template (supplementary enhancement, never a hard dependency).
            from_model = False
            result = TemplateFeedbackProvider().generate_feedback(
                row, run_id=run_id, anchors_by_question=anchors_by_question,
                objective_ids_by_question=objective_ids_by_question,
            )

        sanitized, _warnings = sanitize_feedback_result(
            result, known_anchor_ids=known_anchor_ids, known_objective_ids=known_objective_ids if from_model else None,
        )
        return LearningFeedback(
            run_id=run_id, uid=uid, strengths=sanitized.strengths, gaps=sanitized.gaps,
            practice_steps=sanitized.practice_steps, practice_sequence=sanitized.practice_sequence,
            source_anchors=sanitized.source_anchors, missing_material=sanitized.missing_material, generated_at=_utc_now(),
        )
