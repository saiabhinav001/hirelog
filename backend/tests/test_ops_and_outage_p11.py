"""Prompt 11: database outage / transaction contention must be a retryable 503 (never a bare 500), nothing committed
during an outage, and the operator queue-health read."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc

import exam_inmemory
from app.api import exams_dependencies as deps
from app.api.routes import exams_ops
from app.api.routes import exams_student as student_routes
from app.domain.exams.models import AttemptStatus, ExamRevision, RunStatus
from app.domain.exams.ops_health import collect_queue_health
from app.domain.exams.student_service import ExamStudentService
from app.exam_main import app
from exam_inmemory import FakeFirestore, T0, make_env, seed_attempt, seed_run

BASE = "/api/exams/v1/student"
RUN = "run-1"
Q0 = "rev-1:slot:0"


class Downable(FakeFirestore):
    """A Firestore that can be switched off: every collection / transaction call raises ServiceUnavailable."""

    down = False

    def collection(self, name):
        if self.down:
            raise gexc.ServiceUnavailable("firestore unavailable: internal-host-12345")
        return super().collection(name)

    def transaction(self):
        if self.down:
            raise gexc.ServiceUnavailable("firestore unavailable: internal-host-12345")
        return super().transaction()


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(exam_inmemory, "FakeFirestore", Downable)
    env = make_env(monkeypatch)
    monkeypatch.setattr(student_routes, "db", env.db)
    monkeypatch.setattr(student_routes, "_service", lambda: ExamStudentService(env.db, clock=env.clock))
    user = lambda: {"uid": "s1"}  # noqa: E731
    for dependency in (deps.get_current_exam_user, deps.require_student_run_scope, deps.require_seb_admission):
        app.dependency_overrides[dependency] = user
    app.dependency_overrides[deps.require_exams_enabled] = lambda: None
    seed_run(env.db, run_id=RUN, status=RunStatus.RELEASED, revision_id="rev-1", enrolled=("s1", "s2"))
    revision = ExamRevision(id="rev-1", definition_id="def-1", revision_number=1, title="t", published_at=T0, published_by="t1", slot_count=1)
    env.db.put("exam_definitions", "def-1", "revisions", "rev-1", revision.model_dump(mode="json"))
    seed_attempt(env.db, RUN, "s1")
    yield env
    app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _assert_retryable(response) -> None:
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2" and response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": {"code": "temporarily_unavailable", "message": response.json()["detail"]["message"]}}
    assert "internal-host" not in response.text and "Traceback" not in response.text


def test_a_database_outage_is_a_retryable_503_on_every_student_route_and_commits_nothing(world) -> None:
    client = _client()
    payload = {"idempotency_key": "k1", "client_epoch": 1, "expected_draft_versions": {}}
    world.db.down = True
    for response in (
        client.get(f"{BASE}/runs/{RUN}/state"),
        client.post(f"{BASE}/runs/{RUN}/admit"),
        client.patch(f"{BASE}/runs/{RUN}/drafts/{Q0}", json={"expected_version": 0, "answer": {"selected": "A"}}),
        client.post(f"{BASE}/runs/{RUN}/submit", json=payload),
    ):
        _assert_retryable(response)
    world.db.down = False

    assert world.db.get("exam_runs", RUN, "attempts", "s1")["status"] == AttemptStatus.IN_PROGRESS.value
    assert world.db.collection_docs("exam_runs", RUN, "attempts", "s1", "drafts") == []

    # The very same requests work once the database is back: the retry is safe and the receipt is created once.
    first = client.post(f"{BASE}/runs/{RUN}/submit", json=payload)
    again = client.post(f"{BASE}/runs/{RUN}/submit", json=payload)
    assert first.status_code == again.status_code == 200
    assert first.json()["receipt"]["receipt_id"] == again.json()["receipt"]["receipt_id"]


def test_an_outage_during_the_deadline_window_never_extends_it(world) -> None:
    client = _client()
    world.db.down = True
    world.clock.advance(timedelta(hours=3))  # the deadline passes while the database is down
    _assert_retryable(client.post(f"{BASE}/runs/{RUN}/submit", json={"idempotency_key": "late", "client_epoch": 1, "expected_draft_versions": {}}))
    world.db.down = False
    late = client.post(f"{BASE}/runs/{RUN}/submit", json={"idempotency_key": "late", "client_epoch": 1, "expected_draft_versions": {}})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "deadline_passed"  # the clock kept running


@pytest.mark.parametrize("error", [
    gexc.Aborted("lock"), gexc.ServiceUnavailable("down"), gexc.DeadlineExceeded("slow"), gexc.ResourceExhausted("quota"),
    ValueError("Failed to commit transaction in 5 attempts."),
])
def test_retryable_datastore_failures_and_exhausted_transactions_are_503(world, monkeypatch, error) -> None:
    class Boom:
        def get_state(self, *_a, **_k):
            raise error

    monkeypatch.setattr(student_routes, "_service", lambda: Boom())
    _assert_retryable(_client().get(f"{BASE}/runs/{RUN}/state"))


@pytest.mark.parametrize("error", [gexc.PermissionDenied("secret-detail"), gexc.FailedPrecondition("secret-index-url"), ValueError("secret-detail")])
def test_other_failures_stay_a_generic_500_that_leaks_nothing(world, monkeypatch, error) -> None:
    class Boom:
        def get_state(self, *_a, **_k):
            raise error

    monkeypatch.setattr(student_routes, "_service", lambda: Boom())
    response = _client().get(f"{BASE}/runs/{RUN}/state")
    assert response.status_code == 500 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Internal Server Error"} and "secret" not in response.text


# -- operator queue health --------------------------------------------------------------------------------------------

def test_queue_health_counts_backlog_dead_entries_judge_and_overdue_attempts(world, monkeypatch) -> None:
    db = world.db
    monkeypatch.setattr(exams_ops, "db", db)
    now = T0 + timedelta(hours=2)
    monkeypatch.setattr(exams_ops.system_clock, "now", lambda: now)

    def outbox(entry_id, *, minutes_old, attempts=0, error=None, processed=False):
        db.put("exam_outbox", entry_id, {
            "id": entry_id, "kind": "attempt_finalized", "run_id": RUN, "payload": {"uid": "s1"},
            "created_at": (now - timedelta(minutes=minutes_old)).isoformat(), "processed": processed, "attempts": attempts, "last_error": error,
        })

    outbox("o1", minutes_old=1)
    outbox("o2", minutes_old=30, attempts=5, error="boom")  # reached EXAM_OUTBOX_MAX_ATTEMPTS: dead
    outbox("o3", minutes_old=500, processed=True)  # done: not counted
    for sid, status, age in (("j1", "submitted", 10), ("j2", "infra_error", 20), ("j3", "infra_exhausted", 90)):
        db.put("judge_submissions", sid, {
            "id": sid, "candidate_id": "c", "run_id": RUN, "uid": "s1", "question_id": Q0, "purpose": "final_grade", "language_key": "python3.11",
            "source_code": "x", "submission_hash": "h", "runtime_image": "i", "test_suite_version": 1, "status": status, "attempts": 3,
            "created_at": (now - timedelta(minutes=age)).isoformat(), "updated_at": now.isoformat(),
        })
    # s1's deadline (T0 + 60 min) is 60 minutes behind `now`; s2 is not overdue.
    seed_attempt(db, RUN, "s2", admitted_at=now - timedelta(minutes=5))

    app.dependency_overrides[deps.require_exam_super_admin] = lambda: {"uid": "root"}
    response = _client().get("/api/exams/v1/ops/queue-health")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["outbox"]["pending"] == 2 and body["outbox"]["dead"] == 1 and body["outbox"]["with_errors"] == 1
    assert body["outbox"]["oldest_age_seconds"] == 1800.0 and body["outbox"]["by_kind"] == {"attempt_finalized": 2}
    assert body["judge"] == {"waiting": 1, "oldest_waiting_age_seconds": 600.0, "infra_error_retrying": 1, "infra_exhausted_needs_operator": 1, "max_attempts_seen": 3}
    assert body["finalization"]["overdue_unsealed"] == 1 and body["finalization"]["open_attempts"] == 2
    assert body["finalization"]["max_overdue_seconds"] == 3600.0
    assert "s1" not in response.text and "payload" not in response.text  # counts only


def test_queue_health_is_super_admin_only(world) -> None:
    for user in ({"uid": "teacher", "token_email": "teacher@college.edu", "email_verified": True}, {"uid": "s1"}):
        app.dependency_overrides[deps.get_current_exam_user] = lambda user=user: user
        assert _client().get("/api/exams/v1/ops/queue-health").status_code == 403


def test_queue_health_on_an_empty_system_reports_zeroes(world) -> None:
    body = collect_queue_health(world.db, now=T0, outbox_max_attempts=5)
    assert body["outbox"]["pending"] == 0 and body["outbox"]["oldest_age_seconds"] is None
    assert body["judge"]["waiting"] == 0 and body["finalization"]["overdue_unsealed"] == 0
