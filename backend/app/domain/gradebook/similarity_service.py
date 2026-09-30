"""Similarity review service — groups final-grade coding submissions by
(question, language), excludes instructor starter code, delegates pairwise
comparison to a `SimilarityProvider`, and persists matches for authorized
(faculty) reviewers only. Never auto-flags misconduct or adjusts a score —
matches are surfaced for human judgement, never acted on automatically
(contract: never auto-penalize/auto-flag misconduct from similarity)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.domain.generation.models import ItemType
from app.domain.generation.repository import GenerationRepository, VariantAssignmentRepository
from app.domain.gradebook.decisions import strip_starter_code
from app.domain.gradebook.models import SimilarityMatch
from app.domain.gradebook.repository import SimilarityMatchRepository
from app.domain.gradebook.similarity_provider import SimilarityProvider, SimilaritySubmissionInput
from app.domain.judge.repository import SubmissionRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SimilarityService:
    def __init__(self, db, provider: SimilarityProvider) -> None:
        self._db = db
        self._provider = provider
        self.matches = SimilarityMatchRepository(db)
        self._submissions = SubmissionRepository(db)
        self._generation = GenerationRepository(db)
        self._assignments = VariantAssignmentRepository(db)

    def run_for_question(self, *, run_id: str, question_id: str, uids: list[str]) -> list[SimilarityMatch]:
        """`uids` is the assigned cohort for this run (from
        `run.enrolled_uids`) — supplied by the caller so this service has no
        direct dependency on `ExamRunRepository`. Only CODING questions are
        comparable; MCQ/short-answer are skipped (nothing to run JPlag on)."""
        slot_index = int(question_id.rsplit(":slot:", 1)[1])
        inputs_by_language: dict[str, list[SimilaritySubmissionInput]] = {}

        for uid in uids:
            candidate_id = self._assignments.get_assignment(run_id=run_id, uid=uid, slot_index=slot_index)
            if not candidate_id:
                continue
            candidate = self._generation.get_candidate(candidate_id)
            if candidate is None or candidate.item_type != ItemType.CODING:
                continue
            submission = self._submissions.get_latest_final_grade(run_id, uid, question_id)
            if submission is None:
                continue
            starter = candidate.starter_code.get(submission.language_key, "")
            cleaned = strip_starter_code(submission.source_code, starter)
            if not cleaned.strip():
                continue  # nothing left after removing boilerplate — not a real submission
            inputs_by_language.setdefault(submission.language_key, []).append(
                SimilaritySubmissionInput(
                    submission_id=submission.id, uid=uid, language=submission.language_key, source_code=cleaned
                )
            )

        provider_name = type(self._provider).__name__
        now = _utc_now()
        all_matches: list[SimilarityMatch] = []
        for language, inputs in inputs_by_language.items():
            if len(inputs) < 2:
                continue  # nothing to compare a lone submission against
            for result in self._provider.compare(inputs):
                all_matches.append(
                    SimilarityMatch(
                        id=uuid4().hex, run_id=run_id, question_id=question_id, language=language,
                        uid_a=result.uid_a, uid_b=result.uid_b, submission_id_a=result.submission_id_a,
                        submission_id_b=result.submission_id_b, similarity_percent=result.similarity_percent,
                        matched_spans=result.matched_spans, provider=provider_name, computed_at=now,
                    )
                )

        self.matches.replace_for_question(run_id, question_id, all_matches)
        return all_matches

    def list_matches(self, run_id: str, question_id: str) -> list[SimilarityMatch]:
        return self.matches.list_for_question(run_id, question_id)

    def list_matches_for_run(self, run_id: str) -> list[SimilarityMatch]:
        return self.matches.list_for_run(run_id)
