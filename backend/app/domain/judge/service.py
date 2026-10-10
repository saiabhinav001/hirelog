"""Judge service layer: quota'd public runs, sealed final grading, MCQ
exact-match, and the short-answer human-review workflow. Thin wiring over
the pure `grading.py`/`worker.py` functions.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.generation.repository import GenerationRepository
from app.domain.judge.converters import to_test_groups
from app.domain.judge.errors import JudgeBusyError, RuntimeNotAllowedError
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
from app.domain.judge.runtime_allowlist import configured_allowlist
from app.domain.judge.worker import JudgeWorker, new_submission

logger = logging.getLogger("hirelog.judge.service")

_public_run_limiter = SlidingWindowLimiter(10, 300, namespace="judge_public_run")  # 10 / 5 min per attempt+question

_RETRY_BACKOFF_BASE_SECONDS = 15
_RETRY_BACKOFF_MAX_SECONDS = 600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def final_submission_id(*, candidate_id: str, run_id: str, uid: str, question_id: str) -> str:
    """One sealed submission per student, question and variant: a repeated trigger resumes it instead of adding another."""
    digest = hashlib.sha256(f"{run_id}|{uid}|{question_id}|{candidate_id}".encode()).hexdigest()
    return f"final-{digest[:32]}"


def _first_result_id(prefix: str, run_id: str, uid: str, question_id: str, candidate_id: str) -> str:
    digest = hashlib.sha256(f"{prefix}|{run_id}|{uid}|{question_id}|{candidate_id}".encode()).hexdigest()
    return f"{prefix}-{digest[:32]}"


def retry_backoff_seconds(attempts: int) -> int:
    return min(_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts)), _RETRY_BACKOFF_MAX_SECONDS)


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
        source_code: str, public_tests: list[TestGroupSpec], allowlist, test_suite_version: int = 1,
    ) -> CodeSubmission:
        _public_run_limiter.check(
            f"{run_id}:{uid}:{question_id}", detail="Too many test runs. Please wait before trying again."
        )
        # Bound the queue: a student-initiated run is refused while the judge is saturated; sealed grading never is.
        cap = settings.EXAM_JUDGE_MAX_IN_FLIGHT
        if self._submissions.count_in_flight(limit=cap) >= cap:
            raise JudgeBusyError("The code runner is busy. Try again in a moment.")
        now = _utc_now()
        submission = new_submission(
            candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
            purpose=SubmissionPurpose.PUBLIC_RUN, language_key=language_key, source_code=source_code,
            test_suite_version=test_suite_version, now=now,
        )
        self._submissions.create(submission)
        self._worker.submit(submission, test_groups=public_tests, allowlist=allowlist)
        return self._submissions.get(submission.id)

    def poll_public_run(self, submission_id: str, *, public_tests: list[TestGroupSpec]) -> Optional[CodeSubmission]:
        def commit(results) -> dict:
            return {"public_run_results": [r.model_dump(mode="json") for r in results]}

        self._worker.poll_one(submission_id, test_groups=public_tests, commit=commit)
        return self._submissions.get(submission_id)

    def get_submission(self, submission_id: str) -> Optional[CodeSubmission]:
        return self._submissions.get(submission_id)

    # ── Coding: sealed final grading (all groups, triggered by finalization) ─

    def submit_final_grade(
        self, *, candidate_id: str, run_id: str, uid: str, question_id: str, language_key: str,
        source_code: str, test_suite_version: int, all_test_groups: list[TestGroupSpec], allowlist,
    ) -> CodeSubmission:
        """No quota - server-triggered, never student-initiated. Idempotent: the submission id is derived from
        (run, student, question, variant), so a retried trigger resumes the same submission, and one that was
        created but never reached the judge is sent again here instead of being duplicated."""
        submission_id = final_submission_id(candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id)
        submission = new_submission(
            candidate_id=candidate_id, run_id=run_id, uid=uid, question_id=question_id,
            purpose=SubmissionPurpose.FINAL_GRADE, language_key=language_key, source_code=source_code,
            test_suite_version=test_suite_version, now=_utc_now(), submission_id=submission_id,
        )
        created = self._submissions.create_if_absent(submission)
        current = submission if created else (self._submissions.get(submission_id) or submission)
        if current.status == SubmissionStatus.PENDING:
            try:
                self._worker.submit(current, test_groups=all_test_groups, allowlist=allowlist)
            except RuntimeNotAllowedError:
                # Not an infrastructure fault and not a score: a person decides (e.g. an unlisted language key).
                self._exhaust(current, f"language {current.language_key!r} is not usable")
        return self._submissions.get(submission_id) or current

    def poll_final_grade(self, submission_id: str, *, all_test_groups: list[TestGroupSpec]):
        submission = self._submissions.get(submission_id)
        if submission is None:
            raise LookupError(f"submission {submission_id} not found")

        # Idempotent: a grading result already exists for this submission - duplicate provider results or a
        # re-triggered poll must not re-grade (unless a regrade was requested and is still outstanding).
        existing = self._grading_results.latest_for_submission(submission_id)
        regrading = submission.regrade_reason is not None
        if existing is not None and not regrading:
            if submission.status in (SubmissionStatus.SUBMITTED, SubmissionStatus.POLLING):
                # The result was written but the process died before the submission was marked done.
                self._submissions.update(
                    submission_id, {"status": SubmissionStatus.SUCCEEDED.value, "updated_at": _utc_now().isoformat()}
                )
            return existing

        target_version = (submission.regrade_base_version or 0) + 1 if regrading else 1
        outcome: list = []

        def commit(results) -> dict:
            # Written before the submission is marked done, under a deterministic id: re-collecting after a crash
            # rewrites the same version instead of adding one or losing it.
            if existing is not None and existing.version >= target_version:
                outcome.append(existing)
            else:
                result_id = f"{submission_id}-v{target_version}"
                if existing is None:
                    grading = new_grading_result(
                        submission_id=submission_id, candidate_id=submission.candidate_id, run_id=submission.run_id,
                        uid=submission.uid, question_id=submission.question_id, test_results=results,
                        submission_hash=submission.submission_hash, runtime_image=submission.runtime_image,
                        test_suite_version=submission.test_suite_version, now=_utc_now(), result_id=result_id,
                    )
                else:
                    grading = create_regrade(
                        existing, test_results=results, now=_utc_now(), graded_by=submission.regrade_by or "system",
                        reason=submission.regrade_reason or "", result_id=result_id,
                    )
                self._grading_results.create(grading)
                outcome.append(grading)
            return {"regrade_reason": None, "regrade_by": None, "regrade_base_version": None} if regrading else {}

        results = self._worker.poll_one(submission_id, test_groups=all_test_groups, commit=commit)
        if results is None:
            return None
        return outcome[0]

    def regrade(self, submission_id: str, *, all_test_groups: list[TestGroupSpec], allowlist, reviewer: str, reason: str):
        """Creates a NEW version - the original submitted code and every prior grading version are untouched.
        The request is recorded on the submission first, so a result that arrives after a restart still lands as
        the next version; returns None while the judge is still working."""
        previous = self._grading_results.latest_for_submission(submission_id)
        if previous is None:
            raise LookupError(f"no existing grading result for submission {submission_id}")
        submission = self._submissions.get(submission_id)
        if submission is None:
            raise LookupError(f"submission {submission_id} not found")

        self._submissions.update(
            submission_id,
            {
                "status": SubmissionStatus.PENDING.value, "provider_tokens": {}, "attempts": 0, "last_error": None,
                "regrade_reason": reason, "regrade_by": reviewer, "regrade_base_version": previous.version,
                "updated_at": _utc_now().isoformat(),
            },
        )
        refreshed = self._submissions.get(submission_id)
        self._worker.submit(refreshed, test_groups=all_test_groups, allowlist=allowlist)
        return self.poll_final_grade(submission_id, all_test_groups=all_test_groups)

    # ── Operator path for work the judge could not finish ─────────────────────

    def list_stalled(self, run_id: str, *, limit: int = 100) -> list[CodeSubmission]:
        return self._submissions.list_stalled_for_run(run_id, limit=limit)

    def requeue_stalled(self, run_id: str, *, limit: int = 100) -> list[str]:
        """Puts a run's INFRA_EXHAUSTED submissions back to PENDING with a fresh attempt budget. Never creates a
        score: they are graded only when the judge returns a result."""
        requeued: list[str] = []
        now = _utc_now().isoformat()
        for submission in self._submissions.list_stalled_for_run(run_id, limit=limit):
            self._submissions.update(
                submission.id,
                {"status": SubmissionStatus.PENDING.value, "provider_tokens": {}, "attempts": 0, "last_error": None, "updated_at": now},
            )
            requeued.append(submission.id)
        return requeued

    # ── MCQ (exact match) ─────────────────────────────────────────────────────

    def grade_mcq(self, *, candidate_id: str, run_id: str, uid: str, question_id: str, student_answer: str, expected_answer: str) -> McqGradeResult:
        # Version 1 has a deterministic id, so a retried finalization cannot add a second (or overwrite a regrade).
        result = McqGradeResult(
            id=_first_result_id("mcq", run_id, uid, question_id, candidate_id), run_id=run_id, uid=uid,
            question_id=question_id, candidate_id=candidate_id,
            correct=grade_mcq_exact_match(student_answer, expected_answer), graded_at=_utc_now(),
        )
        if not self._mcq_results.create_if_absent(result):
            return self._mcq_results.get(result.id) or result
        return result

    def regrade_mcq(self, *, run_id: str, uid: str, question_id: str, reviewer: str, correct: bool, reason: str) -> McqGradeResult:
        """Explicit faculty override — e.g. a defective-question resolution
        or a grade appeal upheld. Always a NEW version; the original
        automatic result is never edited (contract: 'never overwrite')."""
        previous = self._mcq_results.latest_for_question(run_id, uid, question_id)
        if previous is None:
            raise LookupError(f"no existing MCQ result for run={run_id} uid={uid} question={question_id}")
        new_version = McqGradeResult(
            id=uuid4().hex, run_id=run_id, uid=uid, question_id=question_id, candidate_id=previous.candidate_id,
            correct=correct, version=previous.version + 1, graded_at=_utc_now(), graded_by=reviewer, regrade_reason=reason,
        )
        self._mcq_results.create(new_version)
        return new_version

    # ── Short answer (human rubric review) ────────────────────────────────────

    def create_short_answer_review(
        self, *, candidate_id: str, run_id: str, uid: str, question_id: str, student_answer: str, max_score: float
    ) -> ShortAnswerReview:
        review = ShortAnswerReview(
            id=_first_result_id("sar", run_id, uid, question_id, candidate_id), run_id=run_id, uid=uid,
            question_id=question_id, candidate_id=candidate_id,
            student_answer=student_answer, max_score=max_score, created_at=_utc_now(),
        )
        if not self._short_answer_reviews.create_if_absent(review):
            return self._short_answer_reviews.get(review.id) or review  # never reset a review that was already done
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
        """Polls every submission still `submitted`/`polling`, regardless of whether anyone is watching it - this is
        what completes FINAL_GRADE submissions the outbox handler created (nobody polls those over HTTP). Bounded per
        call and safe to repeat or run concurrently: results are written under deterministic ids before a submission
        is marked done, so a crash or a second worker never double-grades or loses a result.

        One bad submission cannot stop the others: each is handled on its own, and a sealed-grade submission that has
        produced nothing for `EXAM_JUDGE_STALL_SECONDS` is handed to an operator instead of polling forever. Then
        unsent and failed sealed-grade submissions are retried with backoff (`retry_stalled_once`)."""
        generation = GenerationRepository(self._db)
        now = _utc_now()
        processed = 0
        for submission in self._submissions.list_pending_or_polling(limit=limit):
            try:
                if submission.purpose == SubmissionPurpose.FINAL_GRADE and self._overdue(submission, now):
                    self._exhaust(submission, f"no result from the judge within {settings.EXAM_JUDGE_STALL_SECONDS} seconds")
                    continue
                candidate = generation.get_candidate(submission.candidate_id)
                if candidate is None:
                    continue  # orphaned submission - nothing sane to grade against
                public_groups = to_test_groups(candidate.public_tests)
                if submission.purpose == SubmissionPurpose.PUBLIC_RUN:
                    self.poll_public_run(submission.id, public_tests=public_groups)
                else:
                    all_groups = public_groups + to_test_groups(candidate.hidden_test_groups)
                    self.poll_final_grade(submission.id, all_test_groups=all_groups)
                processed += 1
            except Exception as exc:  # noqa: BLE001 - isolate one submission; the next tick retries it
                logger.warning("judge sweep: submission %s could not be polled: %s", submission.id, type(exc).__name__)
        try:
            self.retry_stalled_once(limit=limit, generation=generation)
        except Exception:  # noqa: BLE001
            logger.exception("judge sweep: retry step failed")
        return processed

    def retry_stalled_once(self, *, limit: int = 25, generation=None) -> int:
        """Sealed-grade submissions that never reached the judge (`pending`) or failed on infrastructure
        (`infra_error`): resend after a backoff, or after `EXAM_JUDGE_MAX_INFRA_ATTEMPTS` failures stop and wait for
        an operator. Nothing here ever produces a score."""
        generation = generation or GenerationRepository(self._db)
        now = _utc_now()
        allowlist = configured_allowlist()
        resent = 0
        for submission in self._submissions.list_by_status(["pending", "infra_error"], limit=limit):
            if submission.purpose != SubmissionPurpose.FINAL_GRADE:
                continue  # a student's test run is not retried on their behalf; they run it again
            try:
                if submission.attempts >= settings.EXAM_JUDGE_MAX_INFRA_ATTEMPTS:
                    self._exhaust(submission, submission.last_error or "retries exhausted")
                    continue
                if (now - submission.updated_at).total_seconds() < retry_backoff_seconds(submission.attempts):
                    continue
                candidate = generation.get_candidate(submission.candidate_id)
                if candidate is None:
                    self._exhaust(submission, "the question is no longer available")
                    continue
                groups = to_test_groups(candidate.public_tests) + to_test_groups(candidate.hidden_test_groups)
                self._worker.submit(submission, test_groups=groups, allowlist=allowlist)
                resent += 1
            except RuntimeNotAllowedError:
                self._exhaust(submission, f"language {submission.language_key!r} is not usable")
            except Exception as exc:  # noqa: BLE001
                logger.warning("judge retry: submission %s: %s", submission.id, type(exc).__name__)
        return resent

    def _overdue(self, submission: CodeSubmission, now: datetime) -> bool:
        return (now - submission.updated_at).total_seconds() > settings.EXAM_JUDGE_STALL_SECONDS

    def _exhaust(self, submission: CodeSubmission, reason: str) -> None:
        self._submissions.update(
            submission.id,
            {"status": SubmissionStatus.INFRA_EXHAUSTED.value, "last_error": reason, "updated_at": _utc_now().isoformat()},
        )
        logger.error(
            "judge submission %s (run=%s) needs an operator: %s", submission.id, submission.run_id, reason,
        )
