"""Prompt 8 (ADR 0024) HTTP surface of the judge: honest answers when the runner is unavailable or busy, ownership
before polling, and the operator path for work the judge could not finish. The judge service is replaced by a stub, so
this checks routing, scoping and response shape only."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_judge as judge_routes
from app.domain.judge.errors import JudgeBusyError
from app.domain.judge.models import SubmissionPurpose, SubmissionStatus
from app.exam_main import app

STUDENT = "/api/exams/v1/student/judge"
FACULTY = "/api/exams/v1/faculty/judge"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _submission(**overrides):
    base = dict(
        id="sub-1", uid="s1", run_id="run-1", question_id="run-1:rev:slot:0", purpose=SubmissionPurpose.PUBLIC_RUN,
        status=SubmissionStatus.SUCCEEDED, public_run_results=[], language_key="python3.11", attempts=2,
        last_error="provider submit failed: ConnectionError", updated_at=NOW, source_code="SECRET-SOURCE",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Service:
    """Records what the routes asked of the judge."""

    calls: list = []
    stalled: list = []
    existing = None
    public_run = None
    public_error: Exception | None = None

    def __init__(self, db, provider) -> None:
        pass

    def run_public_test(self, **kwargs):
        type(self).calls.append(("run_public_test", kwargs))
        if type(self).public_error:
            raise type(self).public_error
        return type(self).public_run

    def get_submission(self, submission_id):
        type(self).calls.append(("get_submission", submission_id))
        return type(self).existing

    def poll_public_run(self, submission_id, **kwargs):
        type(self).calls.append(("poll_public_run", submission_id))
        return type(self).existing

    def list_stalled(self, run_id):
        return type(self).stalled

    def requeue_stalled(self, run_id):
        type(self).calls.append(("requeue_stalled", run_id))
        return [s.id for s in type(self).stalled]


class _Audit:
    events: list = []

    def __init__(self, db) -> None:
        pass

    def append(self, event) -> None:
        type(self).events.append(event)


@pytest.fixture
def client(monkeypatch):
    _Service.calls, _Service.stalled, _Service.existing, _Service.public_run, _Service.public_error = [], [], None, None, None
    _Audit.events = []
    user = {"uid": "teacher-1"}
    monkeypatch.setattr(judge_routes, "JudgeService", _Service)
    monkeypatch.setattr(judge_routes, "AuthzAuditRepository", _Audit)
    monkeypatch.setattr(judge_routes, "_judge_provider_for_request", lambda: object())
    monkeypatch.setattr(judge_routes, "authorize_student_for_run", lambda user, run_id: None)
    monkeypatch.setattr(
        judge_routes, "_candidate_for_question",
        lambda run_id, uid, question_id, require_active: SimpleNamespace(id="cand-1", public_tests=[], version=4),
    )
    app.dependency_overrides[deps.require_exams_enabled] = lambda: None
    app.dependency_overrides[deps.get_current_exam_user] = lambda: {"uid": "s1"}
    app.dependency_overrides[deps.require_student_run_scope] = lambda: {"uid": "s1"}
    app.dependency_overrides[scope.require_run_scope] = lambda run_id: user
    yield TestClient(app)
    app.dependency_overrides.clear()


BODY = {"run_id": "run-1", "question_id": "run-1:rev:slot:0", "language_key": "python3.11", "source_code": "print(1)"}


def test_a_test_run_passes_the_variants_suite_version_and_the_configured_registry(client) -> None:
    _Service.public_run = _submission(status=SubmissionStatus.POLLING)
    response = client.post(f"{STUDENT}/public-run", json=BODY)
    assert response.status_code == 200 and response.json() == {"submission_id": "sub-1", "status": "polling"}
    kwargs = dict(_Service.calls)["run_public_test"]
    assert kwargs["test_suite_version"] == 4 and {e.language_key for e in kwargs["allowlist"]} >= {"python3.11", "c17"}


def test_when_the_runner_is_unavailable_the_student_is_told_nothing_ran(client) -> None:
    _Service.public_run = _submission(status=SubmissionStatus.INFRA_ERROR)
    response = client.post(f"{STUDENT}/public-run", json=BODY)
    assert response.status_code == 503 and response.headers["Retry-After"] == "10"
    assert response.json()["detail"]["code"] == "judge_unavailable"
    assert "results" not in response.json()


def test_when_the_judge_is_saturated_a_test_run_is_refused_with_a_retry_hint(client) -> None:
    _Service.public_error = JudgeBusyError("busy")
    response = client.post(f"{STUDENT}/public-run", json=BODY)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "judge_busy"


@pytest.mark.parametrize("existing", [
    None,
    _submission(uid="someone-else"),
    _submission(run_id="run-2"),
    _submission(question_id="run-1:rev:slot:1"),
    _submission(purpose=SubmissionPurpose.FINAL_GRADE),
])
def test_a_submission_that_is_not_the_students_own_test_run_is_never_polled(client, existing) -> None:
    _Service.existing = existing
    response = client.get(f"{STUDENT}/public-run/sub-1", params={"run_id": "run-1", "question_id": "run-1:rev:slot:0"})
    assert response.status_code == 404
    assert ("poll_public_run", "sub-1") not in _Service.calls


def test_a_students_own_test_run_is_polled_and_answered(client) -> None:
    _Service.existing = _submission()
    response = client.get(f"{STUDENT}/public-run/sub-1", params={"run_id": "run-1", "question_id": "run-1:rev:slot:0"})
    assert response.status_code == 200 and response.json() == {"status": "succeeded", "results": []}
    assert response.headers["Cache-Control"] == "no-store"


def test_the_operator_sees_stalled_work_without_any_source_code(client) -> None:
    _Service.stalled = [_submission(id="sub-9", status=SubmissionStatus.INFRA_EXHAUSTED, purpose=SubmissionPurpose.FINAL_GRADE)]
    response = client.get(f"{FACULTY}/runs/run-1/stalled-submissions")
    assert response.status_code == 200
    assert response.json() == {"results": [{
        "submission_id": "sub-9", "uid": "s1", "question_id": "run-1:rev:slot:0", "language_key": "python3.11",
        "attempts": 2, "reason": "provider submit failed: ConnectionError", "updated_at": NOW.isoformat(),
    }]}
    assert "SECRET-SOURCE" not in response.text


def test_requeueing_is_audited_with_the_actor_and_does_nothing_when_there_is_nothing_to_do(client) -> None:
    _Service.stalled = [_submission(id="sub-9"), _submission(id="sub-10")]
    assert client.post(f"{FACULTY}/runs/run-1/requeue-stalled").json() == {"requeued": 2}
    event = _Audit.events[0]
    assert event.kind == "exam_judge_requeue" and event.actor_uid == "teacher-1"
    assert event.detail == {"run_id": "run-1", "count": 2, "submission_ids": ["sub-9", "sub-10"]}

    _Audit.events.clear()
    _Service.stalled = []
    assert client.post(f"{FACULTY}/runs/run-1/requeue-stalled").json() == {"requeued": 0}
    assert _Audit.events == []
