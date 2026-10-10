"""Prompt 8 (ADR 0024): judge hardening. Everything here runs against the deterministic FAKE provider and in-memory
repositories. These tests prove the application's own behaviour - what it sends, stores, retries, refuses and never
scores - and say NOTHING about whether a real sandbox isolates code, blocks egress/metadata or enforces limits.
That needs staging evidence from the actual judge (Prompt 11)."""

from __future__ import annotations

import dataclasses
import json
import socket
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.exams_errors import raise_for_domain_error
from app.core.config import settings
from app.domain.exams.clock import FixedClock
from app.domain.generation.models import ItemType
from app.domain.gradebook.decisions import classify_question_outcome
from app.domain.gradebook.models import QuestionOutcome
from app.domain.judge import limits as judge_limits
from app.domain.judge import service as judge_service
from app.domain.judge.errors import JudgeBusyError, JudgeUnavailableError, ResultOwnershipError, RuntimeNotAllowedError
from app.domain.judge.grading import public_test_result_dto, sealed_grade_summary_dto
from app.domain.judge.limits import JudgeLimits, MAX_STORED_STREAM_CHARS, truncate_stream
from app.domain.judge.models import SubmissionPurpose, SubmissionStatus, TestCaseOutcome, TestCaseSpec, TestGroupSpec
from app.domain.judge.provider import (
    DeterministicFakeJudgeProvider,
    JudgeRawResult,
    JudgeSubmitRequest,
    STATUS_ACCEPTED,
    STATUS_COMPILE_ERROR,
    STATUS_OUTPUT_LIMIT_EXCEEDED,
    STATUS_WRONG_ANSWER,
    UnconfiguredJudgeProvider,
)
from app.domain.judge.runtime_allowlist import configured_allowlist, default_allowlist, resolve_runtime
from test_judge_service import ALLOWLIST_ENTRIES, _hidden_groups, _public_groups, _service

HIDDEN_MARKER = "HIDDEN-MARKER-7f3a9c"

CANDIDATE = SimpleNamespace(
    public_tests=[{"group_name": "public", "weight": 4.0, "is_hidden": False, "tests": [{"stdin": "1 1", "expected_output": "2"}]}],
    hidden_test_groups=[{"group_name": "hidden", "weight": 6.0, "is_hidden": True, "tests": [{"stdin": "9 9", "expected_output": "18"}]}],
    version=3,
)


class _Generation:
    def __init__(self, db) -> None:
        pass

    def get_candidate(self, candidate_id):
        return CANDIDATE


class _FailingProvider(DeterministicFakeJudgeProvider):
    """Reaches the judge only when `down` is False; the error text deliberately looks like it carries a secret."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.down = True

    def submit(self, request: JudgeSubmitRequest) -> str:
        if self.down:
            raise ConnectionError("https://judge.internal/submissions?api_key=SECRET-KEY refused")
        return super().submit(request)


@pytest.fixture
def clock(monkeypatch) -> FixedClock:
    fixed = FixedClock()
    monkeypatch.setattr(judge_service, "_utc_now", fixed.now)
    monkeypatch.setattr(judge_service, "GenerationRepository", _Generation)
    return fixed


def _svc(provider, clock: FixedClock):
    service = _service(provider)
    service._worker._clock = clock
    return service


def _final(service, **overrides):
    kwargs = dict(
        candidate_id="cand-1", run_id="run-1", uid="u1", question_id="run-1:rev:slot:0", language_key="python3.11",
        source_code="print(2)", test_suite_version=3, all_test_groups=_public_groups() + _hidden_groups(),
        allowlist=ALLOWLIST_ENTRIES,
    )
    kwargs.update(overrides)
    return service.submit_final_grade(**kwargs)


def _groups():
    return _public_groups() + _hidden_groups()


# -- limits, network and what a student can steer ------------------------------------------------------------------

def test_every_request_to_the_judge_carries_the_server_owned_limits() -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _service(provider)
    service.run_public_test(
        candidate_id="c", run_id="run-lim", uid="u-lim", question_id="q", language_key="python3.11", source_code="x",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    sent = list(provider._pending.values())
    assert sent and all(r.limits == JudgeLimits() for r in sent)
    assert sent[0].cpu_time_limit_seconds == judge_limits.CPU_TIME_LIMIT_SECONDS == 5.0
    assert sent[0].memory_limit_kb == judge_limits.MEMORY_LIMIT_KB == 262_144
    fields = sent[0].limits.judge0_fields()
    assert fields["wall_time_limit"] > fields["cpu_time_limit"]
    assert fields["max_processes_and_or_threads"] == 32 and fields["max_file_size"] == 1_024 and fields["stack_limit"] == 65_536


def test_the_sandbox_network_is_off_and_cannot_be_switched_on() -> None:
    assert judge_limits.NETWORK_ENABLED is False
    assert JudgeLimits().judge0_fields()["enable_network"] is False
    with pytest.raises(TypeError):
        JudgeLimits(enable_network=True)  # type: ignore[call-arg]
    assert {f.name for f in dataclasses.fields(JudgeLimits)} == {
        "cpu_time_seconds", "wall_time_seconds", "memory_kb", "stack_kb", "max_processes", "max_file_kb",
    }


def test_a_request_has_no_field_through_which_a_student_could_steer_the_sandbox() -> None:
    names = {f.name for f in dataclasses.fields(JudgeSubmitRequest)}
    assert names == {"source_code", "language_id", "stdin", "expected_output", "cpu_time_limit_seconds", "memory_limit_kb", "limits"}
    assert not any(word in n for n in names for word in ("callback", "url", "image", "network", "flags", "compiler", "env"))
    assert not any(word in k for k in JudgeLimits().judge0_fields() for word in ("callback", "url", "image"))


def test_source_that_targets_the_network_or_the_metadata_service_is_only_data_to_the_api_process(monkeypatch) -> None:
    def _no_network(*args, **kwargs):
        raise AssertionError("the API process opened a socket")

    monkeypatch.setattr(socket.socket, "connect", _no_network)
    hostile = "import urllib.request\nurllib.request.urlopen('http://169.254.169.254/latest/meta-data/')"
    provider = DeterministicFakeJudgeProvider()
    service = _service(provider)
    submission = service.run_public_test(
        candidate_id="c", run_id="run-net", uid="u-net", question_id="q", language_key="python3.11", source_code=hostile,
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    service.poll_public_run(submission.id, public_tests=_public_groups())
    assert next(iter(provider._pending.values())).source_code == hostile  # handed over verbatim, never interpreted here


# -- verdicts and what is stored --------------------------------------------------------------------------------------

def _poll_once(raw: JudgeRawResult):
    provider = DeterministicFakeJudgeProvider(default_result=raw)
    service = _service(provider)
    sub = service.run_public_test(
        candidate_id="c", run_id="run-v", uid="u-v", question_id="q", language_key="python3.11", source_code="x",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    return service.poll_public_run(sub.id, public_tests=_public_groups())


def test_a_compile_error_shows_the_compilers_message_not_an_empty_box() -> None:
    done = _poll_once(JudgeRawResult(token="", status_id=STATUS_COMPILE_ERROR, status_description="CE", compile_output="main.c:1: error: expected ';'"))
    result = done.public_run_results[0]
    assert result.outcome == TestCaseOutcome.COMPILE_ERROR and "expected ';'" in result.stderr


def test_an_output_flood_is_bounded_in_what_is_stored_and_still_classified() -> None:
    flood = "A" * 5_000_000
    done = _poll_once(JudgeRawResult(token="", status_id=STATUS_OUTPUT_LIMIT_EXCEEDED, status_description="OLE", stdout=flood, stderr=flood))
    result = done.public_run_results[0]
    assert result.outcome == TestCaseOutcome.OUTPUT_LIMIT_EXCEEDED
    assert len(result.stdout) <= MAX_STORED_STREAM_CHARS + len(judge_limits.TRUNCATION_MARK) and result.stdout.endswith(judge_limits.TRUNCATION_MARK)
    assert len(result.stderr) <= MAX_STORED_STREAM_CHARS + len(judge_limits.TRUNCATION_MARK)


def test_short_output_is_stored_untouched() -> None:
    assert truncate_stream("42\n") == "42\n"


def test_a_wrong_answer_is_a_failed_test_not_an_infrastructure_error() -> None:
    done = _poll_once(JudgeRawResult(token="", status_id=STATUS_WRONG_ANSWER, status_description="WA", stdout="3"))
    assert done.status == SubmissionStatus.SUCCEEDED and done.public_run_results[0].outcome == TestCaseOutcome.FAILED


# -- hidden data never reaches the student -----------------------------------------------------------------------------

def test_hidden_tests_and_outputs_never_appear_in_what_a_student_can_receive() -> None:
    hidden = [TestGroupSpec(group_name="hidden", weight=6.0, is_hidden=True, tests=[TestCaseSpec(stdin="9 9", expected_output=HIDDEN_MARKER)])]
    provider = DeterministicFakeJudgeProvider(
        outcomes_by_stdin={"9 9": JudgeRawResult(token="", status_id=STATUS_WRONG_ANSWER, status_description="WA", stdout=HIDDEN_MARKER, stderr=HIDDEN_MARKER)}
    )
    service = _service(provider)

    pub = service.run_public_test(
        candidate_id="c", run_id="run-h", uid="u-h", question_id="q", language_key="python3.11", source_code="x",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    assert all(HIDDEN_MARKER not in r.expected_output for r in provider._pending.values())  # hidden tests were never sent for a test run

    final = _final(service, run_id="run-h", uid="u-h", all_test_groups=_public_groups() + hidden)
    graded = service.poll_final_grade(final.id, all_test_groups=_public_groups() + hidden)
    summary = json.dumps(sealed_grade_summary_dto(graded))
    assert HIDDEN_MARKER not in summary
    hidden_results = [r for r in graded.test_results if r.is_hidden]
    assert hidden_results and HIDDEN_MARKER in hidden_results[0].stdout  # faculty keep the evidence; students get only the summary
    done = service.poll_public_run(pub.id, public_tests=_public_groups())
    assert all(HIDDEN_MARKER not in json.dumps(public_test_result_dto(r)) for r in done.public_run_results)
    assert done.public_run_results and HIDDEN_MARKER not in done.model_dump_json()


# -- infrastructure failure is pending, never a score -------------------------------------------------------------------

def test_a_provider_that_refuses_work_leaves_the_submission_retryable_with_no_score_and_no_secret(clock) -> None:
    service = _svc(_FailingProvider(), clock)
    sub = _final(service)
    assert sub.status == SubmissionStatus.INFRA_ERROR and sub.attempts == 1
    assert sub.last_error == "provider submit failed: ConnectionError" and "SECRET" not in sub.last_error
    assert service._grading_results.store == {}


def test_without_a_configured_judge_the_work_is_unavailable_not_accepted() -> None:
    service = _service(UnconfiguredJudgeProvider())
    sub = service.run_public_test(
        candidate_id="c", run_id="run-u", uid="u-u", question_id="q", language_key="python3.11", source_code="x",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    assert sub.status == SubmissionStatus.INFRA_ERROR and not sub.public_run_results
    with pytest.raises(JudgeUnavailableError):
        UnconfiguredJudgeProvider().get_result("t")


def test_the_dev_fake_is_not_used_in_production(monkeypatch) -> None:
    from app.exam_main import _judge_provider

    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_BASE_URL", None)
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_API_KEY", None)
    monkeypatch.setattr(settings, "ENV", "production")
    assert isinstance(_judge_provider(), UnconfiguredJudgeProvider)
    monkeypatch.setattr(settings, "ENV", "development")
    assert isinstance(_judge_provider(), DeterministicFakeJudgeProvider)
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_BASE_URL", "https://judge.example")
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_API_KEY", "k")
    assert type(_judge_provider()).__name__ == "Judge0HttpAdapter"


def test_a_failed_submission_is_retried_after_a_backoff_and_then_graded(clock) -> None:
    provider = _FailingProvider()
    service = _svc(provider, clock)
    sub = _final(service)
    service.sweep_pending_once()
    assert service._submissions.get(sub.id).attempts == 1  # too soon: untouched

    provider.down = False
    clock.advance(timedelta(seconds=judge_service.retry_backoff_seconds(1) + 1))
    service.sweep_pending_once()
    assert service._submissions.get(sub.id).status == SubmissionStatus.POLLING
    service.sweep_pending_once()
    graded = service._grading_results.latest_for_submission(sub.id)
    assert graded is not None and graded.weighted_score == graded.max_score == 10.0


def test_when_retries_run_out_it_waits_for_an_operator_and_is_never_scored(clock, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_JUDGE_MAX_INFRA_ATTEMPTS", 3)
    provider = _FailingProvider()
    service = _svc(provider, clock)
    sub = _final(service)
    for _ in range(6):
        clock.advance(timedelta(minutes=11))
        service.sweep_pending_once()
    stored = service._submissions.get(sub.id)
    assert stored.status == SubmissionStatus.INFRA_EXHAUSTED and stored.attempts == 3
    assert service._grading_results.store == {}
    assert [s.id for s in service.list_stalled("run-1")] == [sub.id]
    # the gradebook shows it as pending, not zero
    assert classify_question_outcome(has_nonblank_answer=True, grading_available=False, weighted_score=0.0, max_score=10.0) == QuestionOutcome.PENDING

    provider.down = False
    assert service.requeue_stalled("run-1") == [sub.id]
    assert service._submissions.get(sub.id).status == SubmissionStatus.PENDING and service._submissions.get(sub.id).attempts == 0
    clock.advance(timedelta(minutes=1))
    service.sweep_pending_once()
    service.sweep_pending_once()
    assert service._grading_results.latest_for_submission(sub.id) is not None
    assert service.requeue_stalled("run-1") == []


def test_requeue_only_touches_the_named_run(clock, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_JUDGE_MAX_INFRA_ATTEMPTS", 1)
    service = _svc(_FailingProvider(), clock)
    mine, other = _final(service, run_id="run-1"), _final(service, run_id="run-2")
    clock.advance(timedelta(minutes=11))
    service.sweep_pending_once()
    assert service.requeue_stalled("run-1") == [mine.id]
    assert service._submissions.get(other.id).status == SubmissionStatus.INFRA_EXHAUSTED


def test_a_submission_the_judge_never_finishes_is_handed_to_an_operator_not_polled_forever(clock) -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _svc(provider, clock)
    sub = _final(service)
    for token in list(sub.provider_tokens.values()):
        provider.mark_still_processing(token)
    run = service.run_public_test(
        candidate_id="c", run_id="run-s", uid="u-s", question_id="q", language_key="python3.11", source_code="x",
        public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES,
    )
    for token in list(run.provider_tokens.values()):
        provider.mark_still_processing(token)

    clock.advance(timedelta(seconds=settings.EXAM_JUDGE_STALL_SECONDS + 1))
    service.sweep_pending_once()
    assert service._submissions.get(sub.id).status == SubmissionStatus.INFRA_EXHAUSTED
    assert service._submissions.get(run.id).status == SubmissionStatus.POLLING  # a student's own test run is left alone


def test_a_language_that_is_not_usable_goes_to_an_operator_instead_of_failing_the_whole_attempt(clock) -> None:
    service = _svc(DeterministicFakeJudgeProvider(), clock)
    sub = _final(service, language_key="cobol")
    assert sub.status == SubmissionStatus.INFRA_EXHAUSTED and "cobol" in (sub.last_error or "")
    assert service._grading_results.store == {}


# -- duplicates, crashes and restarts ----------------------------------------------------------------------------------

def test_a_repeated_final_grade_trigger_resumes_the_same_submission(clock) -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _svc(provider, clock)
    first, second = _final(service), _final(service)
    assert first.id == second.id and len(service._submissions.store) == 1
    assert len(provider._pending) == 2  # the two tests were sent once, not twice
    assert first.id.startswith("final-") and "u1" not in first.id


def test_a_trigger_after_a_crash_between_create_and_send_sends_it_once(clock) -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _svc(provider, clock)
    template = _final(service)
    service._submissions.store.clear()
    provider._pending.clear()
    service._submissions.store[template.id] = template.model_copy(update={"status": SubmissionStatus.PENDING, "provider_tokens": {}})
    resumed = _final(service)
    assert resumed.status == SubmissionStatus.POLLING and len(provider._pending) == 2 and len(service._submissions.store) == 1


def test_a_crash_after_the_result_is_written_but_before_the_submission_is_marked_done_loses_nothing(clock) -> None:
    service = _svc(DeterministicFakeJudgeProvider(), clock)
    sub = _final(service)
    original, armed = service._submissions.update, {"on": True}

    def dying(submission_id, update):
        if armed["on"] and update.get("status") == "succeeded":
            armed["on"] = False
            raise RuntimeError("worker died")
        original(submission_id, update)

    service._submissions.update = dying
    with pytest.raises(RuntimeError):
        service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert len(service._grading_results.store) == 1 and service._submissions.get(sub.id).status == SubmissionStatus.POLLING

    again = service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert again.id == f"{sub.id}-v1" and len(service._grading_results.store) == 1
    assert service._submissions.get(sub.id).status == SubmissionStatus.SUCCEEDED


def test_a_crash_before_any_result_is_written_is_collected_on_the_next_poll(clock) -> None:
    service = _svc(DeterministicFakeJudgeProvider(), clock)
    sub = _final(service)
    original_create = service._grading_results.create
    armed = {"on": True}

    def dying(result):
        if armed["on"]:
            armed["on"] = False
            raise RuntimeError("worker died")
        original_create(result)

    service._grading_results.create = dying
    with pytest.raises(RuntimeError):
        service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert service._submissions.get(sub.id).status == SubmissionStatus.POLLING  # nothing was marked done
    assert service.poll_final_grade(sub.id, all_test_groups=_groups()) is not None


def test_one_bad_submission_cannot_stop_the_others_from_being_graded(clock) -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _svc(provider, clock)
    bad, good = _final(service, uid="u-bad"), _final(service, uid="u-good")
    real_poll = service._worker.poll_one

    def poll(submission_id, **kwargs):
        if submission_id == bad.id:
            raise ResultOwnershipError("a result that does not belong here")
        return real_poll(submission_id, **kwargs)

    service._worker.poll_one = poll
    service.sweep_pending_once()
    assert service._grading_results.latest_for_submission(good.id) is not None
    assert service._grading_results.latest_for_submission(bad.id) is None


def test_a_duplicate_provider_result_never_adds_a_second_grade(clock) -> None:
    service = _svc(DeterministicFakeJudgeProvider(), clock)
    sub = _final(service)
    for _ in range(3):
        service.sweep_pending_once()
        service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert len(service._grading_results.store) == 1


def test_a_regrade_that_finishes_later_still_becomes_the_next_version_and_keeps_the_first(clock) -> None:
    provider = DeterministicFakeJudgeProvider()
    service = _svc(provider, clock)
    sub = _final(service)
    first = service.poll_final_grade(sub.id, all_test_groups=_groups())

    pending_tokens: set[str] = set()
    real_submit = provider.submit

    def slow_submit(request):
        token = real_submit(request)
        provider.mark_still_processing(token)
        pending_tokens.add(token)
        return token

    provider.submit = slow_submit
    assert service.regrade(sub.id, all_test_groups=_groups(), allowlist=ALLOWLIST_ENTRIES, reviewer="t1", reason="re-run after fix") is None
    assert len(service._grading_results.store) == 1

    for token in pending_tokens:
        provider.release(token)
    service.sweep_pending_once()  # the sweep, not the HTTP caller, completes it
    latest = service._grading_results.latest_for_submission(sub.id)
    assert latest.version == 2 and latest.graded_by == "t1" and latest.regrade_reason == "re-run after fix"
    assert service._grading_results.get(first.id).version == 1  # the original is untouched
    service.sweep_pending_once()
    service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert len(service._grading_results.store) == 2
    stored = service._submissions.get(sub.id)
    assert stored.status == SubmissionStatus.SUCCEEDED and stored.regrade_reason is None


def test_a_crash_after_the_regrade_was_written_does_not_add_a_third_version(clock) -> None:
    service = _svc(DeterministicFakeJudgeProvider(), clock)
    sub = _final(service)
    service.poll_final_grade(sub.id, all_test_groups=_groups())
    original, armed = service._submissions.update, {"on": True}

    def dying(submission_id, update):
        if armed["on"] and update.get("status") == "succeeded":
            armed["on"] = False
            raise RuntimeError("worker died")
        original(submission_id, update)

    service._submissions.update = dying
    with pytest.raises(RuntimeError):
        service.regrade(sub.id, all_test_groups=_groups(), allowlist=ALLOWLIST_ENTRIES, reviewer="t1", reason="again")
    service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert sorted(r.version for r in service._grading_results.store.values()) == [1, 2]


def test_a_retried_finalization_neither_duplicates_nor_resets_mcq_and_short_answer_work() -> None:
    service = _service(DeterministicFakeJudgeProvider())
    kw = dict(candidate_id="c", run_id="r", uid="u", question_id="q")
    first = service.grade_mcq(student_answer="A", expected_answer="A", **kw)
    service.regrade_mcq(run_id="r", uid="u", question_id="q", reviewer="t", correct=False, reason="defective question")
    again = service.grade_mcq(student_answer="A", expected_answer="A", **kw)
    assert again.id == first.id and sorted(r.version for r in service._mcq_results.store.values()) == [1, 2]

    review = service.create_short_answer_review(student_answer="x", max_score=5.0, **kw)
    service.submit_short_answer_review(review.id, score=4.0, rubric_notes="ok", reviewer="t")
    service.create_short_answer_review(student_answer="x", max_score=5.0, **kw)
    kept = service._short_answer_reviews.get(review.id)
    assert kept.status == "reviewed" and kept.score == 4.0 and len(service._short_answer_reviews.store) == 1


# -- queue saturation ------------------------------------------------------------------------------------------------------

def test_a_saturated_judge_refuses_student_test_runs_but_never_sealed_grading(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_JUDGE_MAX_IN_FLIGHT", 2)
    provider = DeterministicFakeJudgeProvider()
    service = _service(provider)
    kw = dict(candidate_id="c", language_key="python3.11", source_code="x", public_tests=_public_groups(), allowlist=ALLOWLIST_ENTRIES)
    runs = [service.run_public_test(run_id="run-q", uid=f"u{i}", question_id="q", **kw) for i in range(2)]
    with pytest.raises(JudgeBusyError):
        service.run_public_test(run_id="run-q", uid="u-late", question_id="q", **kw)

    sealed = _final(service, run_id="run-q", uid="u-sealed")  # exam grading is durable work, not shed
    assert sealed.status == SubmissionStatus.POLLING

    for run in runs:
        service.poll_public_run(run.id, public_tests=_public_groups())
    service.poll_final_grade(sealed.id, all_test_groups=_groups())
    service.run_public_test(run_id="run-q", uid="u-after", question_id="q", **kw)  # capacity is back


def test_a_busy_or_unavailable_judge_answers_503_with_a_retry_hint() -> None:
    for error in (JudgeBusyError("busy"), JudgeUnavailableError("down")):
        with pytest.raises(HTTPException) as caught:
            raise_for_domain_error(error)
        assert caught.value.status_code == 503 and caught.value.headers["Retry-After"] == "10"
        assert caught.value.detail["code"] == error.code


# -- the server-owned language registry --------------------------------------------------------------------------------------

def test_the_registry_names_exactly_the_candidate_languages() -> None:
    keys = {e.language_key for e in default_allowlist()}
    assert keys == {"c17", "cpp17", "java17", "python3.11", "node18", "typescript5"}
    assert all(not e.validated_against_provider for e in default_allowlist())  # none is validated by this repository


def test_with_the_fake_judge_every_registry_language_is_usable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_BASE_URL", None)
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_API_KEY", None)
    assert len(configured_allowlist()) == 6


def test_with_a_real_judge_only_languages_an_operator_validated_are_usable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_BASE_URL", "https://judge.example")
    monkeypatch.setattr(settings, "EXAM_JUDGE_PROVIDER_API_KEY", "k")
    monkeypatch.setattr(settings, "EXAM_JUDGE_VALIDATED_LANGUAGES", "")
    assert configured_allowlist() == []
    monkeypatch.setattr(settings, "EXAM_JUDGE_VALIDATED_LANGUAGES", " python3.11, c17 ,not-a-language")
    usable = configured_allowlist()
    assert {e.language_key for e in usable} == {"python3.11", "c17"} and all(e.validated_against_provider for e in usable)


@pytest.mark.parametrize("key", ["sql", "bash", "python", "python3.11; rm -rf /", "../python3.11", "", "PYTHON3.11", "java17 --enable-network"])
def test_only_registry_keys_resolve_and_ids_never_come_from_the_student(key) -> None:
    with pytest.raises(RuntimeNotAllowedError):
        resolve_runtime(key, default_allowlist())


def test_sql_stays_out_until_a_disposable_runner_is_qualified() -> None:
    assert "sql" not in {i.value for i in ItemType}
    assert not any("sql" in e.language_key.lower() or "sql" in e.display_name.lower() for e in default_allowlist())


def test_a_result_is_bound_to_the_exact_code_variant_runtime_and_suite_version(clock) -> None:
    service = _svc(DeterministicFakeJudgeProvider(), clock)
    sub = _final(service, source_code="print(7)", test_suite_version=3)
    graded = service.poll_final_grade(sub.id, all_test_groups=_groups())
    assert graded.candidate_id == "cand-1" and graded.test_suite_version == 3 and graded.runtime_image == "judge0/python:3.11"
    assert graded.submission_hash == sub.submission_hash and len(graded.submission_hash) == 64


def test_the_unavailable_provider_is_a_submission_purpose_nobody_can_choose_from_a_request() -> None:
    assert {p.value for p in SubmissionPurpose} == {"public_run", "final_grade"}
    assert STATUS_ACCEPTED == 3
