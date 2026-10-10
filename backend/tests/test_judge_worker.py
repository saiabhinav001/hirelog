"""`JudgeWorker` submit/poll orchestration tests: correct/incorrect code,
compile/runtime errors, limits, output flooding, provider infra faults
(never partially scored), idempotent re-polling after a simulated crash, and
a buggy/malicious provider that returns a mismatched result token.

`SubmissionRepository` needs a real Firestore client, so — following the
existing convention (see `test_institution_auth_service.py`) — `JudgeWorker`
is constructed via `__new__` with an in-memory fake repository assigned
directly, rather than touching Firestore.
"""

from __future__ import annotations

import pytest

from app.domain.exams.clock import FixedClock
from app.domain.judge.errors import ResultOwnershipError, RuntimeNotAllowedError
from app.domain.judge.grading import aggregate_weighted_score
from app.domain.judge.models import (
    CodeSubmission,
    SubmissionPurpose,
    SubmissionStatus,
    TestCaseSpec,
    TestGroupSpec,
)
from app.domain.judge.provider import (
    DeterministicFakeJudgeProvider,
    JudgeRawResult,
    STATUS_ACCEPTED,
    STATUS_COMPILE_ERROR,
    STATUS_EXEC_FORMAT_ERROR,
    STATUS_INTERNAL_ERROR,
    STATUS_OUTPUT_LIMIT_EXCEEDED,
    STATUS_RUNTIME_ERROR_MIN,
    STATUS_TIME_LIMIT_EXCEEDED,
    STATUS_WRONG_ANSWER,
)
from app.domain.judge.runtime_allowlist import default_allowlist
from app.domain.judge.worker import JudgeWorker, new_submission

NOW = FixedClock().now()


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
        matches = [s for s in self.store.values() if s.status == SubmissionStatus.INFRA_ERROR]
        return matches[:limit]


def _worker(provider, repo: _FakeSubmissionRepo | None = None) -> tuple[JudgeWorker, _FakeSubmissionRepo]:
    repo = repo or _FakeSubmissionRepo()
    worker = JudgeWorker.__new__(JudgeWorker)
    worker._repo = repo
    worker._provider = provider
    worker._clock = FixedClock()
    return worker, repo


def _submission(**overrides) -> CodeSubmission:
    defaults = dict(
        candidate_id="cand-1", run_id="run-1", uid="u1", question_id="q1",
        purpose=SubmissionPurpose.FINAL_GRADE, language_key="python3.11",
        source_code="print(sum(map(int, input().split())))", test_suite_version=1, now=NOW,
    )
    defaults.update(overrides)
    return new_submission(**defaults)


def _two_test_groups() -> list[TestGroupSpec]:
    return [
        TestGroupSpec(
            group_name="basic", weight=10.0, is_hidden=False,
            tests=[TestCaseSpec(stdin="1 2", expected_output="3"), TestCaseSpec(stdin="2 2", expected_output="4")],
        )
    ]


ALLOWLIST = default_allowlist()


# ── submit ────────────────────────────────────────────────────────────────────

def test_submit_records_one_token_per_flattened_test_and_resolves_runtime_image() -> None:
    provider = DeterministicFakeJudgeProvider()
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)

    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)

    stored = repo.get(submission.id)
    assert stored.status == SubmissionStatus.POLLING
    assert len(stored.provider_tokens) == 2
    assert stored.runtime_image == "judge0/python:3.11"


def test_submit_rejects_language_not_on_allowlist() -> None:
    provider = DeterministicFakeJudgeProvider()
    worker, repo = _worker(provider)
    submission = _submission(language_key="cobol85")
    repo.create(submission)

    with pytest.raises(RuntimeNotAllowedError):
        worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)


# ── poll_one: code outcomes (correct/incorrect/compile/runtime/limits) ───────

def test_poll_one_returns_none_while_any_test_still_processing() -> None:
    """Queue-saturation scenario: one slow test must not produce a partial
    or premature result."""
    provider = DeterministicFakeJudgeProvider()
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)
    stored = repo.get(submission.id)
    first_token = next(iter(stored.provider_tokens.values()))
    provider.mark_still_processing(first_token)

    results = worker.poll_one(submission.id, test_groups=_two_test_groups())

    assert results is None
    assert repo.get(submission.id).status == SubmissionStatus.POLLING


def test_poll_one_all_correct_marks_succeeded_and_scores_full_credit() -> None:
    provider = DeterministicFakeJudgeProvider()  # default outcome = Accepted
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)

    results = worker.poll_one(submission.id, test_groups=_two_test_groups())

    assert results is not None
    assert len(results) == 2
    earned, max_score = aggregate_weighted_score(results)
    assert earned == max_score
    assert repo.get(submission.id).status == SubmissionStatus.SUCCEEDED


def test_poll_one_incorrect_code_scores_partial_credit_not_infra_error() -> None:
    provider = DeterministicFakeJudgeProvider(
        outcomes_by_stdin={"2 2": JudgeRawResult(token="", status_id=STATUS_WRONG_ANSWER, status_description="x")}
    )
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)

    results = worker.poll_one(submission.id, test_groups=_two_test_groups())

    earned, max_score = aggregate_weighted_score(results)
    assert earned == 5.0  # one of two equally-weighted tests passed
    assert max_score == 10.0
    assert repo.get(submission.id).status == SubmissionStatus.SUCCEEDED  # scored, not retried


@pytest.mark.parametrize(
    "status_id,expected_outcome_value",
    [
        (STATUS_COMPILE_ERROR, "compile_error"),
        (STATUS_RUNTIME_ERROR_MIN, "runtime_error"),
        (STATUS_TIME_LIMIT_EXCEEDED, "time_limit_exceeded"),
        (STATUS_OUTPUT_LIMIT_EXCEEDED, "output_limit_exceeded"),
    ],
)
def test_poll_one_classifies_every_code_failure_mode_and_still_succeeds(status_id, expected_outcome_value) -> None:
    """Compile error / runtime error / time limit / output flooding are all
    legitimate scored code failures — none of them should be treated as a
    provider infra fault."""
    provider = DeterministicFakeJudgeProvider(
        outcomes_by_stdin={"1 2": JudgeRawResult(token="", status_id=status_id, status_description="x")}
    )
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)

    results = worker.poll_one(submission.id, test_groups=_two_test_groups())

    outcomes = {r.outcome.value for r in results}
    assert expected_outcome_value in outcomes
    assert repo.get(submission.id).status == SubmissionStatus.SUCCEEDED


@pytest.mark.parametrize("status_id", [STATUS_INTERNAL_ERROR, STATUS_EXEC_FORMAT_ERROR, 12345])
def test_poll_one_any_infra_error_fails_whole_submission_not_partial_score(status_id) -> None:
    """Network-denial / provider-fault scenario: one bad test result must
    mark the WHOLE submission retryable, never produce a partial/zero score."""
    provider = DeterministicFakeJudgeProvider(
        outcomes_by_stdin={"2 2": JudgeRawResult(token="", status_id=status_id, status_description="x")}
    )
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)

    results = worker.poll_one(submission.id, test_groups=_two_test_groups())

    assert results is None
    stored = repo.get(submission.id)
    assert stored.status == SubmissionStatus.INFRA_ERROR
    assert stored.attempts == 1


# ── poll_one: idempotency / crash-safety ─────────────────────────────────────

def test_poll_one_on_already_succeeded_submission_is_a_safe_noop() -> None:
    """Worker-crash-and-restart / duplicate-poll scenario: re-processing an
    already-terminal submission must not re-score or raise."""
    provider = DeterministicFakeJudgeProvider()
    worker, repo = _worker(provider)
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)
    first = worker.poll_one(submission.id, test_groups=_two_test_groups())
    assert first is not None

    second = worker.poll_one(submission.id, test_groups=_two_test_groups())

    assert second is None  # already SUCCEEDED — nothing more to do


def test_poll_one_unknown_submission_raises_lookup_error() -> None:
    provider = DeterministicFakeJudgeProvider()
    worker, _repo = _worker(provider)
    with pytest.raises(LookupError):
        worker.poll_one("does-not-exist", test_groups=_two_test_groups())


class _TokenSwappingProvider:
    """Simulates a buggy/malicious provider whose result claims a DIFFERENT
    token than the one actually queried — e.g. two concurrent queries got
    their results crossed. `poll_one` must refuse to trust it rather than
    silently attributing the wrong result to this submission (contract: 'do
    not assume signed provider callbacks')."""

    def submit(self, request) -> str:
        return "tok-real"

    def get_result(self, token: str):
        return JudgeRawResult(token="tok-DIFFERENT-FROM-QUERY", status_id=STATUS_ACCEPTED, status_description="x")


def test_poll_one_rejects_a_result_whose_token_does_not_match_what_was_queried() -> None:
    worker, repo = _worker(_TokenSwappingProvider())
    submission = _submission()
    repo.create(submission)
    worker.submit(submission, test_groups=_two_test_groups(), allowlist=ALLOWLIST)

    with pytest.raises(ResultOwnershipError):
        worker.poll_one(submission.id, test_groups=_two_test_groups())
