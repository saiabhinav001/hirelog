"""Judge service layer: quota'd public runs, sealed final grading, MCQ
exact-match, and the short-answer human-review workflow. Thin wiring over
the pure `grading.py`/`worker.py` functions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.core.rate_limit import SlidingWindowLimiter
from app.domain.generation.repository import GenerationRepository
from app.domain.judge.converters import to_test_groups
from app.domain.judge.grading import create_regrade, grade_mcq_exact_match, new_grading_result
from app.domain.judge.models import (
    CodeSubmission,
    McqGradeResult,
    ShortAnswerReview,
    ShortAnswerReviewStatus,
    SubmissionPurpose,
    SubmissionStatus,
    TestGroupSpec,
)
from app.domain.judge.provider import JudgeProvider
from app.domain.judge.repository import (
    GradingResultRepository,
    McqResultRepository,
    ShortAnswerReviewRepository,
    SubmissionRepository,
)
from app.domain.judge.worker import JudgeWorker, new_submission

_public_run_limiter = SlidingWindowLimiter(10, 300, namespace="judge_public_run")  # 10 / 5 min per attempt+question


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JudgeService:
    def __init__(self, db, provider: JudgeProvider) -> None:
        self._db = db
        self._provider = provider
        self._submissions = SubmissionRepository(db)
        self._grading_results = GradingResultRepository(db)
        self._mcq_results = McqResultRepository(db)
        self._short_answer_reviews = ShortAnswerReviewRepository(db)
        self._worker = JudgeWorker(db, provider)

    # ── Coding: public run (quota'd, public tests only) ──────────────────────

    def run_public_test(
        self, *, candidate_id: str, run_id: str, uid: str, question_id: str, language_key: str,
        source_code: str, public_tests: list[TestGroupSpec], allowlist,
    ) -> CodeSubmission:
        _public_run_limiter.check(
            f"{run_id}:{uid}:{question_id}", detail="Too many test runs. Please wait before trying again."
        )
        now = _utc_now()
        submission = new_submission(
            candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
            purpose=SubmissionPurpose.PUBLIC_RUN, language_key=language_key, source_code=source_code,
            test_suite_version=1, now=now,
        )
        self._submissions.create(submission)
        self._worker.submit(submission, test_groups=public_tests, allowlist=allowlist)
        return self._submissions.get(submission.id)

    def poll_public_run(self, submission_id: str, *, public_tests: list[TestGroupSpec]) -> Optional[CodeSubmission]:
        results = self._worker.poll_one(submission_id, test_groups=public_tests)
        if results is None:
            return self._submissions.get(submission_id)
        self._submissions.update(
            submission_id, {"public_run_results": [r.model_dump(mode="json") for r in results]}
        )
        return self._submissions.get(submission_id)

    # ── Coding: sealed final grading (all groups, triggered by finalization) ─

    def submit_final_grade(
        self, *, candidate_id: str, run_id: str, uid: str, question_id: str, language_key: str,
        source_code: str, test_suite_version: int, all_test_groups: list[TestGroupSpec], allowlist,
    ) -> CodeSubmission:
        """No quota — this is server-triggered exactly once per finalized
        attempt, never student-initiated."""
        now = _utc_now()
        submission = new_submission(
            candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
            purpose=SubmissionPurpose.FINAL_GRADE, language_key=language_key, source_code=source_code,
            test_suite_version=test_suite_version, now=now,
        )
        self._submissions.create(submission)
        self._worker.submit(submission, test_groups=all_test_groups, allowlist=allowlist)
        return submission

    def poll_final_grade(self, submission_id: str, *, all_test_groups: list[TestGroupSpec]):
        submission = self._submissions.get(submission_id)
        if submission is None:
            raise LookupError(f"submission {submission_id} not found")

        # Idempotent: a grading result already exists for this submission —
        # duplicate provider results / a re-triggered poll must not re-grade.
        existing = self._grading_results.latest_for_submission(submission_id)
        if existing is not None:
            return existing

        results = self._worker.poll_one(submission_id, test_groups=all_test_groups)
        if results is None:
            return None

        grading = new_grading_result(
            submission_id=submission_id, candidate_id=submission.candidate_id, run_id=submission.run_id,
            uid=submission.uid, question_id=submission.question_id, test_results=results,
            submission_hash=submission.submission_hash, runtime_image=submission.runtime_image,
            test_suite_version=submission.test_suite_version, now=_utc_now(),
        )
        self._grading_results.create(grading)
        return grading

    def regrade(self, submission_id: str, *, all_test_groups: list[TestGroupSpec], allowlist, reviewer: str, reason: str):
        """Creates a NEW version — the original submitted code and every
        prior grading version are untouched."""
        previous = self._grading_results.latest_for_submission(submission_id)
        if previous is None:
            raise LookupError(f"no existing grading result for submission {submission_id}")
        submission = self._submissions.get(submission_id)
        if submission is None:
            raise LookupError(f"submission {submission_id} not found")

        self._submissions.update(
            submission_id, {"status": SubmissionStatus.PENDING.value, "provider_tokens": {}}
        )
        refreshed = self._submissions.get(submission_id)
        self._worker.submit(refreshed, test_groups=all_test_groups, allowlist=allowlist)
        results = self._worker.poll_one(submission_id, test_groups=all_test_groups)
        if results is None:
            return None  # still processing — caller polls again

        new_version = create_regrade(previous, test_results=results, now=_utc_now(), graded_by=reviewer, reason=reason)
        self._grading_results.create(new_version)
        return new_version

    # ── MCQ (exact match) ─────────────────────────────────────────────────────

    def grade_mcq(self, *, candidate_id: str, run_id: str, uid: str, question_id: str, student_answer: str, expected_answer: str) -> McqGradeResult:
        result = McqGradeResult(
            run_id=run_id, uid=uid, question_id=question_id, candidate_id=candidate_id,
            correct=grade_mcq_exact_match(student_answer, expected_answer), graded_at=_utc_now(),
        )
        self._mcq_results.save(result)
        return result

    # ── Short answer (human rubric review) ────────────────────────────────────

    def create_short_answer_review(
        self, *, candidate_id: str, run_id: str, uid: str, question_id: str, student_answer: str, max_score: float
    ) -> ShortAnswerReview:
        review = ShortAnswerReview(
            id=uuid4().hex, run_id=run_id, uid=uid, question_id=question_id, candidate_id=candidate_id,
            student_answer=student_answer, max_score=max_score, created_at=_utc_now(),
        )
        self._short_answer_reviews.create(review)
        return review

    def submit_short_answer_review(self, review_id: str, *, score: float, rubric_notes: str, reviewer: str) -> ShortAnswerReview:
        review = self._short_answer_reviews.get(review_id)
        if review is None:
            raise LookupError(f"short-answer review {review_id} not found")
        now = _utc_now()
        is_regrade = review.status == ShortAnswerReviewStatus.REVIEWED
        self._short_answer_reviews.update(
            review_id,
            {
                "status": ShortAnswerReviewStatus.REVIEWED.value,
                "score": score,
                "rubric_notes": rubric_notes,
                "reviewed_by": reviewer,
                "reviewed_at": now.isoformat(),
                "version": review.version + 1 if is_regrade else review.version,
            },
        )
        return self._short_answer_reviews.get(review_id)

    # ── Background sweep (bounded, idempotent, crash-safe) ────────────────────

    def sweep_pending_once(self, *, limit: int = 25) -> int:
        """Polls every submission still `submitted`/`polling`, regardless of
        whether any HTTP request is actively watching it — this is what
        actually completes FINAL_GRADE submissions the outbox handler
        created (nobody polls those via HTTP). Bounded per call and safe to
        call repeatedly/concurrently: `poll_final_grade` re-checks
        `latest_for_submission` before creating a result, and `poll_one`
        treats an already-terminal submission as a no-op — so a crash or a
        second worker picking up mid-sweep never double-grades or double-
        submits (contract #11: durable/restart-safe workers)."""
        generation = GenerationRepository(self._db)
        processed = 0
        for submission in self._submissions.list_pending_or_polling(limit=limit):
            candidate = generation.get_candidate(submission.candidate_id)
            if candidate is None:
                continue  # orphaned submission — nothing sane to grade against
            public_groups = to_test_groups(candidate.public_tests)
            if submission.purpose == SubmissionPurpose.PUBLIC_RUN:
                self.poll_public_run(submission.id, public_tests=public_groups)
            else:
                all_groups = public_groups + to_test_groups(candidate.hidden_test_groups)
                self.poll_final_grade(submission.id, all_test_groups=all_groups)
            processed += 1
        return processed
