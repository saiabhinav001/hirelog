"""`JudgeService` tests: quota'd public runs (public tests only), idempotent
final grading (duplicate polls / simulated worker restarts never double-
grade), explicit regrade versioning, MCQ exact-match, the short-answer
review workflow, and the "late grading after exam end" design guarantee
(grading has no dependency on `RunStatus`/`ExamRun` at all).

`JudgeService.__init__` builds its repositories from a Firestore `db`, so —
matching the existing convention (see `test_institution_auth_service.py`) —
tests construct it via `__new__` and inject in-memory fakes directly.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.domain.exams.clock import FixedClock
from app.domain.judge.models import (
    CodeSubmission,
    GradingResult,
    McqGradeResult,
    ShortAnswerReview,
    ShortAnswerReviewStatus,
    SubmissionPurpose,
    SubmissionStatus,
    TestCaseSpec,
    TestGroupSpec,
)
from app.domain.judge.provider import DeterministicFakeJudgeProvider
from app.domain.judge.runtime_allowlist import default_allowlist
from app.domain.judge.service import JudgeService
from app.domain.judge.worker import JudgeWorker


class _FakeSubmissionRepo:
    def __init__(self) -> None:
        self.store: dict[str, CodeSubmission] = {}

    def create(self, submission: CodeSubmission) -> None:
        self.store[submission.id] = submission

    def get(self, submission_id: str):
        return self.store.get(submission_id)

    def update(self, submission_id: str, update: dict) -> None:
        current = self.store[submission_id]
        self.store[submission_id] = current.model_copy(update=update)

    def list_pending_or_polling(self, *, limit: int):
        matches = [s for s in self.store.values() if s.status in (SubmissionStatus.SUBMITTED, SubmissionStatus.POLLING)]
        return matches[:limit]

    def list_infra_errors(self, *, limit: int):
        return [s for s in self.store.values() if s.status == SubmissionStatus.INFRA_ERROR][:limit]


class _FakeGradingResultRepo:
    def __init__(self) -> None:
        self.store: dict[str, GradingResult] = {}

    def create(self, result: GradingResult) -> None:
        self.store[result.id] = result

    def get(self, result_id: str):
        return self.store.get(result_id)

    def latest_for_submission(self, submission_id: str):
        matches = [r for r in self.store.values() if r.submission_id == submission_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.version)


class _FakeMcqResultRepo:
    def __init__(self) -> None:
        self.store: dict[str, McqGradeResult] = {}

    @property
    def saved(self) -> list[McqGradeResult]:
        return list(self.store.values())

    def create(self, result: McqGradeResult) -> None:
        self.store[result.id] = result

    def get(self, result_id: str):
        return self.store.get(result_id)

    def latest_for_question(self, run_id: str, uid: str, question_id: str):
        matches = [r for r in self.store.values() if r.run_id == run_id and r.uid == uid and r.question_id == question_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.version)


class _FakeShortAnswerReviewRepo:
    def __init__(self) -> None:
        self.store: dict[str, ShortAnswerReview] = {}

    def create(self, review: ShortAnswerReview) -> None:
        self.store[review.id] = review

    def get(self, review_id: str):
        return self.store.get(review_id)

    def update(self, review_id: str, update: dict) -> None:
        current = self.store[review_id]
        self.store[review_id] = current.model_copy(update=update)

    def list_pending(self, *, limit: int = 100):
        matches = [r for r in self.store.values() if r.status == ShortAnswerReviewStatus.PENDING]
        return matches[:limit]


def _service(provider):
    svc = JudgeService.__new__(JudgeService)
    svc._db = object()
    svc._provider = provider
    svc._submissions = _FakeSubmissionRepo()
    svc._grading_results = _FakeGradingResultRepo()
    svc._mcq_results = _FakeMcqResultRepo()
    svc._short_answer_reviews = _FakeShortAnswerReviewRepo()
    worker = JudgeWorker.__new__(JudgeWorker)
    worker._repo = svc._submissions
    worker._provider = provider
    worker._clock = FixedClock()
    svc._worker = worker
    return svc


def _public_groups() -> list[TestGroupSpec]:
    return [TestGroupSpec(group_name="public", weight=4.0, is_hidden=False, tests=[TestCaseSpec(stdin="1 1", expected_output="2")])]


def _hidden_groups() -> list[TestGroupSpec]:
    return [TestGroupSpec(group_name="hidden", weight=6.0, is_hidden=True, tests=[TestCaseSpec(stdin="9 9", expected_output="18")])]


ALLOWLIST_ENTRIES = default_allowlist()


# ── Public run: quota + public-tests-only ────────────────────────────────────

def test_run_public_test_submits_only_public_tests() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    submission = service.run_public_test(
        candidate_id="cand-1", run_id="run-pub-1", uid="u-pub-1", question_id="q1",
        language_key="python3.11", source_code="print(2)",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    assert submission.purpose == SubmissionPurpose.PUBLIC_RUN
    assert len(submission.provider_tokens) == 1  # only the one public test — no hidden tests submitted


def test_poll_public_run_populates_results_once_ready() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    submission = service.run_public_test(
        candidate_id="cand-1", run_id="run-pub-2", uid="u-pub-2", question_id="q1",
        language_key="python3.11", source_code="print(2)",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    updated = service.poll_public_run(submission.id, public_tests=_public_groups())
    assert updated.status == SubmissionStatus.SUCCEEDED
    assert len(updated.public_run_results) == 1


def test_run_public_test_quota_exceeded_raises_429() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    kwargs = dict(
        candidate_id="cand-1", run_id="run-quota-unique", uid="u-quota-unique", question_id="q-quota",
        language_key="python3.11", source_code="print(2)", public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    for _ in range(10):  # limiter allows 10 per window
        service.run_public_test(**kwargs)

    with pytest.raises(HTTPException) as exc_info:
        service.run_public_test(**kwargs)
    assert exc_info.value.status_code == 429


# ── Final grade: idempotency (duplicate polls / simulated worker restart) ───

def test_poll_final_grade_twice_creates_only_one_grading_result() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    submission = service.submit_final_grade(
        candidate_id="cand-1", run_id="run-final-1", uid="u-final-1", question_id="q1",
        language_key="python3.11", source_code="print(2)", test_suite_version=1,
        all_test_groups=_public_groups() + _hidden_groups(), allowlist=ALLOWLIST_ENTRIES,
    )

    first = service.poll_final_grade(submission.id, all_test_groups=_public_groups() + _hidden_groups())
    second = service.poll_final_grade(submission.id, all_test_groups=_public_groups() + _hidden_groups())

    assert first is not None
    assert second is not None
    assert first.id == second.id  # same document — never double-created
    assert len(service._grading_results.store) == 1


def test_poll_final_grade_still_processing_returns_none() -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _service(provider)
    submission = service.submit_final_grade(
        candidate_id="cand-1", run_id="run-final-2", uid="u-final-2", question_id="q1",
        language_key="python3.11", source_code="print(2)", test_suite_version=1,
        all_test_groups=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    stored = service._submissions.get(submission.id)
    token = next(iter(stored.provider_tokens.values()))
    provider.mark_still_processing(token)

    result = service.poll_final_grade(submission.id, all_test_groups=_public_groups())

    assert result is None
    assert len(service._grading_results.store) == 0


def test_grading_proceeds_with_no_dependency_on_any_run_status() -> None:
    """'Late grading after exam end' guarantee: `JudgeService` never looks up
    `RunStatus`/`ExamRun` at all, so grading a submission for a `run_id` with
    no corresponding exam run (as if the run had long since ended/archived)
    must still succeed."""
    service = _service(DeterministicFakeJudgeProvider())
    submission = service.submit_final_grade(
        candidate_id="cand-1", run_id="run-that-does-not-exist-anywhere", uid="u-late-1", question_id="q1",
        language_key="python3.11", source_code="print(2)", test_suite_version=1,
        all_test_groups=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    result = service.poll_final_grade(submission.id, all_test_groups=_public_groups())
    assert result is not None
    assert result.weighted_score == result.max_score


# ── Regrade ───────────────────────────────────────────────────────────────────

def test_regrade_creates_new_version_and_preserves_previous() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    submission = service.submit_final_grade(
        candidate_id="cand-1", run_id="run-regrade-1", uid="u-regrade-1", question_id="q1",
        language_key="python3.11", source_code="print(2)", test_suite_version=1,
        all_test_groups=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    original = service.poll_final_grade(submission.id, all_test_groups=_public_groups())
    original_snapshot = original.model_copy(deep=True)

    regraded = service.regrade(
        submission.id, all_test_groups=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
        reviewer="fac-1", reason="rubric adjustment",
    )

    assert regraded.version == original.version + 1
    assert regraded.id != original.id
    assert service._grading_results.get(original.id) == original_snapshot  # untouched


def test_regrade_without_existing_result_raises_lookup_error() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    with pytest.raises(LookupError):
        service.regrade("no-such-submission", all_test_groups=_public_groups(), allowlist=ALLOWLIST_ENTRIES, reviewer="fac-1", reason="x")


# ── MCQ ───────────────────────────────────────────────────────────────────────

def test_grade_mcq_correct_and_incorrect() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    correct = service.grade_mcq(candidate_id="c1", run_id="r1", uid="u1", question_id="q1", student_answer="A", expected_answer="A")
    incorrect = service.grade_mcq(candidate_id="c1", run_id="r1", uid="u2", question_id="q1", student_answer="B", expected_answer="A")
    assert correct.correct is True
    assert incorrect.correct is False
    assert len(service._mcq_results.saved) == 2


def test_regrade_mcq_creates_new_version_and_preserves_original() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    original = service.grade_mcq(candidate_id="c1", run_id="r1", uid="u1", question_id="q1", student_answer="B", expected_answer="A")
    assert original.correct is False

    regraded = service.regrade_mcq(run_id="r1", uid="u1", question_id="q1", reviewer="fac-1", correct=True, reason="Question key was wrong.")

    assert regraded.correct is True
    assert regraded.version == 2
    assert regraded.graded_by == "fac-1"
    # Original document is untouched.
    assert service._mcq_results.get(original.id).correct is False
    assert service._mcq_results.get(original.id).version == 1


def test_regrade_mcq_without_existing_result_raises() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    with pytest.raises(LookupError):
        service.regrade_mcq(run_id="r1", uid="u1", question_id="no-such-question", reviewer="fac-1", correct=True, reason="x")


# ── Short answer review ───────────────────────────────────────────────────────

def test_short_answer_review_lifecycle_and_regrade_bumps_version() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    review = service.create_short_answer_review(
        candidate_id="c1", run_id="r1", uid="u1", question_id="q1", student_answer="my answer", max_score=10.0
    )
    assert review.status == ShortAnswerReviewStatus.PENDING

    reviewed = service.submit_short_answer_review(review.id, score=7.0, rubric_notes="good", reviewer="fac-1")
    assert reviewed.status == ShortAnswerReviewStatus.REVIEWED
    assert reviewed.score == 7.0
    assert reviewed.version == 1

    regraded = service.submit_short_answer_review(review.id, score=9.0, rubric_notes="revised upward", reviewer="fac-2")
    assert regraded.version == 2
    assert regraded.score == 9.0


def test_submit_short_answer_review_unknown_id_raises_lookup_error() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    with pytest.raises(LookupError):
        service.submit_short_answer_review("no-such-review", score=1.0, rubric_notes="", reviewer="fac-1")
