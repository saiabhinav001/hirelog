"""Keeping the exam workers inside the free Firestore plan: the finalization sweep reads drafts only for attempts that
can be sealed, says when it next needs to run, and the workers are woken by the work they wait for."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import background
from app.domain.exams.models import RunStatus
from app.domain.exams.student_service import ExamStudentService
from exam_inmemory import T0, make_env, seed_attempt, seed_run

MIN = timedelta(minutes=1)


@pytest.fixture
def env(monkeypatch):
    return make_env(monkeypatch)


@pytest.fixture
def woken(monkeypatch):
    names: list[str] = []
    monkeypatch.setattr(background, "wake", names.append)
    return names


def _count_draft_reads(env, monkeypatch) -> list[str]:
    reads: list[str] = []
    original = env.sweeper._drafts.all_versions

    def counted(run_id, uid):
        reads.append(uid)
        return original(run_id, uid)

    monkeypatch.setattr(env.sweeper._drafts, "all_versions", counted)
    return reads


# -- the finalization sweep ----------------------------------------------------------------

def test_the_sweeper_reads_drafts_only_for_attempts_past_their_deadline(env, monkeypatch) -> None:
    seed_run(env.db, enrolled=("s1", "s2", "s3"))
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    seed_attempt(env.db, "run-1", "s2", minutes=90)
    seed_attempt(env.db, "run-1", "s3", minutes=90)
    reads = _count_draft_reads(env, monkeypatch)

    env.clock.set(T0 + 30 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 0
    assert reads == []  # nobody is late, so no draft is read

    env.clock.set(T0 + 61 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 1
    assert reads == ["s1"]


def test_the_sweeper_says_when_it_next_needs_to_run(env) -> None:
    assert env.sweeper.seconds_until_next_deadline() is None  # nothing seen yet
    seed_run(env.db, enrolled=("s1", "s2"))
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    seed_attempt(env.db, "run-1", "s2", minutes=90)

    env.clock.set(T0 + 10 * MIN)
    env.sweeper.sweep_active_runs_once(batch_size=10)
    assert env.sweeper.seconds_until_next_deadline() == pytest.approx(50 * 60 + 1.0)  # the earliest deadline, s1's

    env.clock.set(T0 + 61 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 1
    assert env.sweeper.seconds_until_next_deadline() == pytest.approx(29 * 60 + 1.0)  # now s2's

    env.clock.set(T0 + 91 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 1
    assert env.sweeper.seconds_until_next_deadline() is None  # no live attempt is left


def test_a_backlog_keeps_the_sweeper_on_its_fast_cadence(env) -> None:
    seed_run(env.db, enrolled=("s1", "s2", "s3"))
    for uid in ("s1", "s2", "s3"):
        seed_attempt(env.db, "run-1", uid, minutes=60)

    env.clock.set(T0 + 61 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=2) == 2
    assert env.sweeper.seconds_until_next_deadline() == 0.0  # one late attempt is still unsealed
    assert env.sweeper.sweep_active_runs_once(batch_size=2) == 1
    assert env.sweeper.seconds_until_next_deadline() is None


def test_an_attempt_without_a_deadline_is_never_waited_for(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", deadline=None)
    env.db.put("exam_runs", "run-1", "attempts", "s1", {**env.db.get("exam_runs", "run-1", "attempts", "s1"), "deadline": None})
    env.clock.set(T0 + 500 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 0
    assert env.sweeper.seconds_until_next_deadline() is None


# -- work wakes the workers that wait for it -------------------------------------------------

def test_an_applied_run_command_wakes_the_sweeper_and_the_outbox_worker(env, woken) -> None:
    seed_run(env.db, status=RunStatus.PUBLISHED)
    env.service.execute(run_id="run-1", command="release", expected_version=1, idempotency_key="k1")
    assert sorted(woken) == ["finalization", "outbox"]

    env.service.execute(run_id="run-1", command="release", expected_version=1, idempotency_key="k1")  # a replay
    assert len(woken) == 2


def test_admission_wakes_the_sweeper(env, woken) -> None:
    seed_run(env.db)
    ExamStudentService(env.db, clock=env.clock).admit("run-1", "s1")
    assert woken == ["finalization"]


def test_a_sealed_submit_wakes_the_outbox_worker_once(env, woken) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    service = ExamStudentService(env.db, clock=env.clock)
    service.finish("run-1", "s1", idempotency_key="done", client_epoch=1)
    assert woken == ["outbox"]

    service.finish("run-1", "s1", idempotency_key="done", client_epoch=1)  # a retry reuses the receipt
    assert woken == ["outbox"]


def test_terminating_an_attempt_wakes_the_outbox_worker(env, woken) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    env.service.terminate_attempt(run_id="run-1", uid="s1", idempotency_key="t1", actor_uid="teacher-1", reason="cheating")
    assert woken == ["outbox"]
