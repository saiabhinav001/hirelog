"""Durable submit/poll worker. All state lives in Firestore
(`CodeSubmission.status`/`provider_tokens`/`attempts`), so any process can
resume a submission after a crash — there is no in-memory-only state a
restart would lose (contract #11).

Polling only (contract: "do not assume signed provider callbacks" — the
safest response to that is to not build a callback trust boundary at all in
this phase). Every result is matched against `provider_tokens` before being
trusted (`verify_result_ownership`), and a submission is retried, never
silently zero-scored, on infra error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import uuid4

from app.domain.exams.clock import Clock, system_clock
from app.domain.judge.errors import ResultOwnershipError
from app.domain.judge.grading import classify_status, is_infra_error
from app.domain.judge.models import (
    CodeSubmission,
    SubmissionStatus,
    TestCaseResult,
    TestGroupSpec,
)
from app.domain.judge.provider import JudgeProvider, JudgeSubmitRequest
from app.domain.judge.repository import SubmissionRepository
from app.domain.judge.runtime_allowlist import resolve_runtime
from app.domain.judge.submission import verify_result_ownership

logger = logging.getLogger("hirelog.judge.worker")

DEFAULT_CPU_TIME_LIMIT_SECONDS = 5.0
DEFAULT_MEMORY_LIMIT_KB = 262_144  # 256 MB


@dataclass(frozen=True)
class _FlatTest:
    test_id: str
    group_name: str
    weight: float
    is_hidden: bool
    stdin: str
    expected_output: str


def _flatten_test_groups(groups: list[TestGroupSpec]) -> list[_FlatTest]:
    flat: list[_FlatTest] = []
    for group in groups:
        per_test_weight = group.weight / max(1, len(group.tests))
        for index, test in enumerate(group.tests):
            flat.append(
                _FlatTest(
                    test_id=f"{group.group_name}:{index}",
                    group_name=group.group_name,
                    weight=per_test_weight,
                    is_hidden=group.is_hidden,
                    stdin=test.stdin,
                    expected_output=test.expected_output,
                )
            )
    return flat


class JudgeWorker:
    def __init__(self, db, provider: JudgeProvider, clock: Clock = system_clock) -> None:
        self._repo = SubmissionRepository(db)
        self._provider = provider
        self._clock = clock

    def submit(self, submission: CodeSubmission, *, test_groups: list[TestGroupSpec], allowlist) -> None:
        """Submits every flattened test case to the provider. Never lets the
        student influence limits/runtime — those come only from the
        allowlist entry resolved server-side."""
        runtime = resolve_runtime(submission.language_key, allowlist)
        tokens: dict[str, str] = {}
        for test in _flatten_test_groups(test_groups):
            token = self._provider.submit(
                JudgeSubmitRequest(
                    source_code=submission.source_code,
                    language_id=runtime.judge0_language_id,
                    stdin=test.stdin,
                    expected_output=test.expected_output,
                    cpu_time_limit_seconds=DEFAULT_CPU_TIME_LIMIT_SECONDS,
                    memory_limit_kb=DEFAULT_MEMORY_LIMIT_KB,
                )
            )
            tokens[test.test_id] = token

        now = self._clock.now()
        self._repo.update(
            submission.id,
            {
                "status": SubmissionStatus.POLLING.value,
                "provider_tokens": tokens,
                "runtime_image": runtime.runtime_image,
                "updated_at": now.isoformat(),
            },
        )

    def poll_one(self, submission_id: str, *, test_groups: list[TestGroupSpec]) -> Optional[list[TestCaseResult]]:
        """Returns the final per-test results once ALL of this submission's
        tokens have resolved, or None if still processing / an infra error
        was hit (submission is left polling/marked infra_error — retryable,
        never a partial or zero score)."""
        submission = self._repo.get(submission_id)
        if submission is None:
            raise LookupError(f"submission {submission_id} not found")
        if submission.status not in (SubmissionStatus.SUBMITTED, SubmissionStatus.POLLING):
            return None  # already terminal — duplicate poll is a safe no-op

        flat_by_id = {t.test_id: t for t in _flatten_test_groups(test_groups)}
        results: list[TestCaseResult] = []
        any_infra_error = False

        for test_id, token in submission.provider_tokens.items():
            raw = self._provider.get_result(token)
            if raw is None:
                return None  # at least one test still processing

            if not verify_result_ownership(submission.provider_tokens, test_id, raw.token):
                raise ResultOwnershipError(f"result token for test {test_id!r} does not match the recorded submission")

            outcome = classify_status(raw)
            if is_infra_error(outcome):
                any_infra_error = True
            spec = flat_by_id[test_id]
            results.append(
                TestCaseResult(
                    test_id=test_id,
                    group_name=spec.group_name,
                    is_hidden=spec.is_hidden,
                    weight=spec.weight,
                    outcome=outcome,
                    stdout=raw.stdout,
                    stderr=raw.stderr,
                    time_ms=raw.time_ms,
                    memory_kb=raw.memory_kb,
                )
            )

        now = self._clock.now()
        if any_infra_error:
            self._repo.update(
                submission_id,
                {
                    "status": SubmissionStatus.INFRA_ERROR.value,
                    "attempts": submission.attempts + 1,
                    "last_error": "one or more tests returned a provider infrastructure error",
                    "updated_at": now.isoformat(),
                },
            )
            return None

        self._repo.update(submission_id, {"status": SubmissionStatus.SUCCEEDED.value, "updated_at": now.isoformat()})
        return results

    def retry_infra_errors(self, *, test_groups: list[TestGroupSpec], allowlist) -> int:
        """Bounded — resubmits submissions stuck in INFRA_ERROR. Callers loop
        this on a timer, mirroring the exam finalization sweep pattern."""
        retried = 0
        for submission in self._repo.list_infra_errors(limit=25):
            self.submit(submission, test_groups=test_groups, allowlist=allowlist)
            retried += 1
        return retried


def new_submission(
    *,
    candidate_id: str,
    run_id: str,
    uid: str,
    question_id: str,
    purpose,
    language_key: str,
    source_code: str,
    test_suite_version: int,
    now: datetime,
) -> CodeSubmission:
    from app.domain.judge.submission import compute_submission_hash

    return CodeSubmission(
        id=uuid4().hex,
        candidate_id=candidate_id,
        run_id=run_id,
        uid=uid,
        question_id=question_id,
        purpose=purpose,
        language_key=language_key,
        source_code=source_code,
        submission_hash=compute_submission_hash(source_code),
        runtime_image="",  # filled in once `submit()` resolves the allowlist entry
        test_suite_version=test_suite_version,
        created_at=now,
        updated_at=now,
    )
