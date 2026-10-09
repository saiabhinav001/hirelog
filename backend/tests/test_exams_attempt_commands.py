"""Prompt 3 (ADR 0019): per-attempt and per-student commands (extend, terminate,
grant/revoke access, recover), the finalization sweeper and schedule
automation, including the extension/expiry race, duplicate retries and
concurrent commands. Runs on the in-memory Firestore with optimistic
transactions (`exam_inmemory.py`)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from app.domain.exams.clock import FixedClock
from app.domain.exams.commands import ExamCommandService
from app.domain.exams.decisions import (
    MAX_MANUAL_EXTENSION_MINUTES,
    decide_save_draft,
    decide_scheduled_command,
    decide_submit,
)
from app.domain.exams.errors import CommandRejectedError
from app.domain.exams.finalization import SWEEPER_ACTOR, ExamFinalizationSweeper
from app.domain.exams.models import Attempt, AttemptStatus, ExamRun, RunStatus
from app.domain.exams.policy import AccommodationWindow, ExamPolicy
from exam_inmemory import T0, audit_kinds, make_env, outbox_kinds, seed_attempt, seed_run

MIN = timedelta(minutes=1)


@pytest.fixture
def env(monkeypatch):
    return make_env(monkeypatch)


def _attempt(env, uid="s1", run_id="run-1") -> dict:
    return env.db.get("exam_runs", run_id, "attempts", uid)


def _extend(env, uid="s1", minutes=15, **kw):
    return env.service.grant_time_extension(run_id="run-1", uid=uid, extra_minutes=minutes, actor_uid="t1", **kw)


def _race(workers) -> list:
    barrier = threading.Barrier(len(workers))
    results: list = [None] * len(workers)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - the test inspects the type
            results[i] = ("err", exc)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(workers)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert all(r is not None for r in results), "a worker never finished"
    return results


def _seed_draft(env, uid, question_id, version, run_id="run-1"):
    env.db.put("exam_runs", run_id, "attempts", uid, "drafts", question_id,
               {"question_id": question_id, "version": version, "answer": {"selected": "A"}})


# -- time extension ---------------------------------------------------------------------

def test_extension_moves_the_deadline_bumps_the_version_and_is_audited(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    env.clock.set(T0 + 30 * MIN)

    result = _extend(env, reason="power cut", idempotency_key="e1")

    assert result.manual_extension_minutes == 15 and not result.reused_existing_receipt
    doc = _attempt(env)
    assert doc["deadline"] == (T0 + 75 * MIN).isoformat() and doc["manual_extension_minutes"] == 15
    assert doc["version"] == 2
    event = [e for e in env.db.collection_docs("exam_authz_audit") if e["kind"] == "exam_attempt_extended"][0]
    assert event["actor_uid"] == "t1" and event["uid"] == "s1"
    assert event["detail"]["reason"] == "power cut" and event["detail"]["extra_minutes"] == 15
    assert event["detail"]["new_deadline"] == (T0 + 75 * MIN).isoformat()


def test_extension_stacks_on_an_accommodated_deadline(env) -> None:
    policy = ExamPolicy(duration_minutes=60, accommodations={"s1": AccommodationWindow(extra_minutes=30)})
    seed_run(env.db, policy=policy)
    seed_attempt(env.db, "run-1", "s1", minutes=90, accommodation_extra_minutes=30)
    _extend(env, minutes=10)
    doc = _attempt(env)
    assert doc["deadline"] == (T0 + 100 * MIN).isoformat()
    assert doc["accommodation_extra_minutes"] == 30 and doc["manual_extension_minutes"] == 10


def test_a_duplicate_retry_with_the_same_key_adds_the_minutes_once(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    first = _extend(env, idempotency_key="same")
    again = _extend(env, idempotency_key="same")
    assert not first.reused_existing_receipt and again.reused_existing_receipt
    assert again.deadline == first.deadline
    assert _attempt(env)["manual_extension_minutes"] == 15
    assert audit_kinds(env.db) == ["exam_attempt_extended"]


def test_distinct_keys_are_distinct_extensions(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    _extend(env, idempotency_key="a")
    _extend(env, idempotency_key="b")
    assert _attempt(env)["manual_extension_minutes"] == 30


def test_the_cumulative_manual_extension_is_capped(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", manual_extension_minutes=MAX_MANUAL_EXTENSION_MINUTES - 5)
    with pytest.raises(CommandRejectedError) as caught:
        _extend(env, minutes=6)
    assert caught.value.reason == "extension_limit_exceeded"
    _extend(env, minutes=5)  # exactly at the cap is fine


def test_non_positive_extensions_are_rejected_before_any_write(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    with pytest.raises(ValueError):
        _extend(env, minutes=0)
    assert _attempt(env)["version"] == 1


@pytest.mark.parametrize("status", [AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.FINALIZED])
def test_a_sealed_attempt_can_never_be_extended(env, status) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", status=status)
    with pytest.raises(CommandRejectedError) as caught:
        _extend(env)
    assert caught.value.reason == "attempt_resolved"
    assert _attempt(env)["status"] == status.value and _attempt(env)["manual_extension_minutes"] == 0


def test_extension_is_rejected_on_an_ended_run_and_for_unknown_attempts(env) -> None:
    seed_run(env.db, status=RunStatus.ENDED)
    seed_attempt(env.db, "run-1", "s1")
    with pytest.raises(CommandRejectedError) as caught:
        _extend(env)
    assert caught.value.reason == "run_ended"
    with pytest.raises(LookupError):
        _extend(env, uid="ghost")


def test_an_extension_after_the_deadline_is_rejected_even_before_the_sweeper_runs(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    env.clock.set(T0 + 61 * MIN)
    with pytest.raises(CommandRejectedError) as caught:
        _extend(env)
    assert caught.value.reason == "deadline_passed"
    assert env.sweeper.sweep_once("run-1", batch_size=10) == 1  # the sweeper then seals it


def test_concurrent_extensions_never_lose_an_update(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    results = _race([(lambda i=i: _extend(env, minutes=5, idempotency_key=f"k{i}")) for i in range(8)])
    assert all(r[0] == "ok" for r in results)
    doc = _attempt(env)
    assert doc["manual_extension_minutes"] == 40
    assert doc["deadline"] == (T0 + 100 * MIN).isoformat()
    assert doc["version"] == 9
    assert env.db.commit_conflicts > 0  # the writers really did collide and re-run


def test_concurrent_duplicate_extension_retries_apply_once(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    results = _race([(lambda: _extend(env, minutes=5, idempotency_key="one")) for _ in range(8)])
    assert all(r[0] == "ok" for r in results)
    assert sum(1 for r in results if not r[1].reused_existing_receipt) == 1
    assert _attempt(env)["manual_extension_minutes"] == 5


# -- extension / expiry race --------------------------------------------------------------

def test_an_extension_granted_before_expiry_protects_the_attempt_from_the_sweeper(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    env.clock.set(T0 + 59 * MIN)
    _extend(env, minutes=15)
    env.clock.set(T0 + 61 * MIN)  # past the ORIGINAL deadline
    assert env.sweeper.sweep_once("run-1", batch_size=10) == 0
    assert _attempt(env)["status"] == "in_progress"
    env.clock.set(T0 + 76 * MIN)  # past the extended one
    assert env.sweeper.sweep_once("run-1", batch_size=10) == 1


def test_an_extension_that_loses_the_race_to_the_sweeper_is_rejected_and_changes_nothing(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    env.clock.set(T0 + 61 * MIN)
    assert env.sweeper.sweep_once("run-1", batch_size=10) == 1
    before = _attempt(env)
    with pytest.raises(CommandRejectedError) as caught:
        _extend(env)
    assert caught.value.reason == "attempt_resolved"
    assert _attempt(env) == before


def test_threaded_extension_versus_sweeper_never_leaves_an_extended_but_sealed_attempt(monkeypatch) -> None:
    """The extension's clock is just inside the deadline, the sweeper's just
    past it. Whoever commits first wins; the loser must observe that and
    back off. The forbidden outcomes are 'sealed AND extended' and 'neither'."""
    outcomes = {"extended": 0, "sealed": 0}
    for _ in range(40):
        env = make_env(monkeypatch)
        seed_run(env.db)
        seed_attempt(env.db, "run-1", "s1", minutes=60)
        extender = ExamCommandService(env.db, clock=FixedClock(T0 + 59 * MIN))
        sweeper = ExamFinalizationSweeper(env.db, clock=FixedClock(T0 + 61 * MIN))
        results = _race([
            lambda: extender.grant_time_extension(run_id="run-1", uid="s1", extra_minutes=10, actor_uid="t1"),
            lambda: sweeper.sweep_once("run-1", batch_size=10),
        ])
        doc = _attempt(env)
        extended = doc["manual_extension_minutes"] == 10
        sealed = doc["status"] == "expired"
        assert extended != sealed, doc  # exactly one of the two
        if sealed:
            assert results[0][0] == "err" and results[0][1].reason == "attempt_resolved"
            assert doc["submit_receipt"] is not None
        else:
            assert results[0][0] == "ok" and results[1] == ("ok", 0)
        outcomes["sealed" if sealed else "extended"] += 1
        assert outbox_kinds(env.db).count("attempt_finalized") == (1 if sealed else 0)
    assert sum(outcomes.values()) == 40


# -- terminate ---------------------------------------------------------------------------------

def _terminate(env, uid="s1", key="t1", reason="caught cheating", actor="teacher-1"):
    return env.service.terminate_attempt(run_id="run-1", uid=uid, idempotency_key=key, actor_uid=actor, reason=reason)


def test_terminate_seals_with_a_receipt_a_reason_and_the_grading_trigger(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    _seed_draft(env, "s1", "rev-1:slot:0", 3)
    env.clock.set(T0 + 10 * MIN)

    result = _terminate(env)

    assert result.status == "expired" and not result.reused_existing_receipt
    doc = _attempt(env)
    assert doc["status"] == "expired" and doc["finalization_reason"] == "faculty_terminated"
    assert doc["finalized_by"] == "teacher-1"
    assert doc["submit_receipt"]["sealed_draft_versions"] == {"rev-1:slot:0": 3}
    assert doc["submit_receipt"]["idempotency_key"] == "terminate:t1"
    trigger = [o for o in env.db.collection_docs("exam_outbox") if o["kind"] == "attempt_finalized"][0]
    assert trigger["payload"]["uid"] == "s1" and trigger["payload"]["reason"] == "faculty_terminated"
    receipt = env.db.get("exam_command_receipts", "run-1__terminate:s1__t1")
    assert receipt["actor_uid"] == "teacher-1" and receipt["reason"] == "caught cheating"
    assert audit_kinds(env.db) == ["exam_attempt_terminated"]


def test_terminate_is_idempotent_for_a_retried_key(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    _terminate(env)
    again = _terminate(env)
    assert again.reused_existing_receipt and not again.changed
    assert outbox_kinds(env.db) == ["attempt_finalized"] and audit_kinds(env.db) == ["exam_attempt_terminated"]


def test_terminating_a_sealed_attempt_with_a_new_key_is_rejected(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    _terminate(env, key="first")
    with pytest.raises(CommandRejectedError) as caught:
        _terminate(env, key="second")
    assert caught.value.reason == "attempt_resolved"
    assert outbox_kinds(env.db) == ["attempt_finalized"]


def test_a_blocked_attempt_can_be_terminated(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", status=AttemptStatus.BLOCKED)
    assert _terminate(env).status == "expired"


def test_terminate_unknown_attempt_is_a_lookup_error(env) -> None:
    seed_run(env.db)
    with pytest.raises(LookupError):
        _terminate(env, uid="ghost")


def test_a_terminated_attempt_is_never_reopened_by_any_student_or_faculty_path(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    _terminate(env)
    attempt = Attempt.model_validate(_attempt(env))
    run = ExamRun.model_validate(env.db.get("exam_runs", "run-1"))
    now = T0 + 5 * MIN

    save = decide_save_draft(attempt=attempt, run=run, now=now, expected_version=0, current_version=0)
    submit = decide_submit(attempt=attempt, run=run, now=now, idempotency_key="x", client_epoch=attempt.epoch,
                           expected_draft_versions={}, current_draft_versions={})
    assert not save.accepted and save.conflict_reason == "attempt_not_in_progress"
    assert not submit.accepted
    with pytest.raises(CommandRejectedError):
        _extend(env)
    assert env.sweeper.sweep_once("run-1", batch_size=10) == 0


def test_terminate_versus_sweeper_seals_exactly_once(monkeypatch) -> None:
    for _ in range(25):
        env = make_env(monkeypatch)
        seed_run(env.db)
        seed_attempt(env.db, "run-1", "s1", minutes=60)
        env.clock.set(T0 + 61 * MIN)
        results = _race([lambda: _terminate(env), lambda: env.sweeper.sweep_once("run-1", batch_size=10)])
        for kind, value in results:
            assert kind == "ok" or isinstance(value, CommandRejectedError)  # the loser backs off cleanly
        assert _attempt(env)["status"] == "expired"
        assert outbox_kinds(env.db).count("attempt_finalized") == 1
        assert sum(1 for k in audit_kinds(env.db) if k in ("exam_attempt_terminated", "exam_attempt_expired")) == 1


# -- grant / revoke access ------------------------------------------------------------------------

def _access(env, action, uid="s9", key="a1", reason=None):
    return env.service.change_access(run_id="run-1", action=action, uid=uid, idempotency_key=key,
                                     actor_uid="t1", reason=reason)


def test_grant_adds_the_student_without_bumping_the_run_version(env) -> None:
    seed_run(env.db, enrolled=("s1",), version=4)
    result = _access(env, "grant", reason="late registration")
    assert result.changed
    doc = env.db.get("exam_runs", "run-1")
    assert doc["enrolled_uids"] == ["s1", "s9"] and doc["version"] == 4
    event = env.db.collection_docs("exam_authz_audit")[0]
    assert event["kind"] == "exam_access_granted" and event["uid"] == "s9" and event["detail"]["reason"] == "late registration"


def test_grant_is_idempotent_by_value_and_by_key(env) -> None:
    seed_run(env.db, enrolled=("s1", "s9"))
    assert _access(env, "grant", key="x").changed is False
    again = _access(env, "grant", key="x")
    assert again.reused_existing_receipt
    assert env.db.get("exam_runs", "run-1")["enrolled_uids"] == ["s1", "s9"]
    assert audit_kinds(env.db) == []  # nothing changed, nothing to audit


def test_revoke_removes_a_student_who_has_not_started(env) -> None:
    seed_run(env.db, enrolled=("s1", "s9"))
    assert _access(env, "revoke").changed
    assert env.db.get("exam_runs", "run-1")["enrolled_uids"] == ["s1"]
    assert audit_kinds(env.db) == ["exam_access_revoked"]


@pytest.mark.parametrize("status", [AttemptStatus.IN_PROGRESS, AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED])
def test_revoke_is_refused_once_the_student_has_an_attempt(env, status) -> None:
    seed_run(env.db, enrolled=("s1", "s9"))
    seed_attempt(env.db, "run-1", "s9", status=status)
    with pytest.raises(CommandRejectedError) as caught:
        _access(env, "revoke")
    assert caught.value.reason == "attempt_exists"
    assert env.db.get("exam_runs", "run-1")["enrolled_uids"] == ["s1", "s9"]


def test_access_changes_are_refused_on_an_ended_run(env) -> None:
    seed_run(env.db, status=RunStatus.ENDED, enrolled=("s1",))
    for action in ("grant", "revoke"):
        with pytest.raises(CommandRejectedError) as caught:
            _access(env, action, uid="s1" if action == "revoke" else "s9", key=action)
        assert caught.value.reason == "run_ended"


def test_concurrent_grants_for_different_students_are_all_kept(env) -> None:
    seed_run(env.db, enrolled=("s1",))
    results = _race([(lambda i=i: _access(env, "grant", uid=f"n{i}", key=f"k{i}")) for i in range(8)])
    assert all(r[0] == "ok" for r in results)
    assert sorted(env.db.get("exam_runs", "run-1")["enrolled_uids"]) == sorted(["s1"] + [f"n{i}" for i in range(8)])


# -- recovery ---------------------------------------------------------------------------------------

def _recover(env, uid="s1", key="r1"):
    return env.service.recover_attempt(run_id="run-1", uid=uid, idempotency_key=key, actor_uid="t1", reason="grading lost")


@pytest.mark.parametrize("status", [AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.FINALIZED])
def test_recovery_re_enqueues_grading_for_a_sealed_attempt_without_touching_it(env, status) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", status=status)
    before = _attempt(env)
    _recover(env)
    assert _attempt(env) == before
    trigger = env.db.collection_docs("exam_outbox")[0]
    assert trigger["kind"] == "attempt_finalized" and trigger["payload"]["reason"] == "recovery"
    assert audit_kinds(env.db) == ["exam_attempt_recovery_requested"]


def test_recovery_is_idempotent_for_a_retried_key(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", status=AttemptStatus.SUBMITTED)
    _recover(env)
    assert _recover(env).reused_existing_receipt
    assert len(env.db.collection_docs("exam_outbox")) == 1


def test_recovery_refuses_a_live_or_missing_attempt(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", status=AttemptStatus.IN_PROGRESS)
    with pytest.raises(CommandRejectedError) as live:
        _recover(env)
    assert live.value.reason == "attempt_not_sealed"
    with pytest.raises(CommandRejectedError) as missing:
        _recover(env, uid="ghost")
    assert missing.value.reason == "attempt_not_found"
    assert env.db.collection_docs("exam_outbox") == []


# -- finalization sweeper ----------------------------------------------------------------------------

def test_the_sweeper_seals_an_expired_attempt_with_receipt_trigger_and_audit(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    _seed_draft(env, "s1", "rev-1:slot:0", 2)
    env.clock.set(T0 + 61 * MIN)

    assert env.sweeper.sweep_once("run-1", batch_size=50) == 1

    doc = _attempt(env)
    assert doc["status"] == "expired" and doc["finalization_reason"] == "deadline_expired"
    assert doc["finalized_by"] == SWEEPER_ACTOR
    assert doc["submit_receipt"]["sealed_draft_versions"] == {"rev-1:slot:0": 2}
    assert outbox_kinds(env.db) == ["attempt_expired", "attempt_finalized"]
    trigger = [o for o in env.db.collection_docs("exam_outbox") if o["kind"] == "attempt_finalized"][0]
    assert trigger["payload"] == {"uid": "s1", "receipt_id": doc["submit_receipt"]["receipt_id"], "reason": "deadline_expired"}
    assert audit_kinds(env.db) == ["exam_attempt_expired"]


def test_the_sweeper_leaves_live_attempts_and_accommodated_students_alone(env) -> None:
    policy = ExamPolicy(duration_minutes=60, accommodations={"acc": AccommodationWindow(extra_minutes=30)})
    seed_run(env.db, policy=policy, enrolled=("s1", "acc", "done"))
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    seed_attempt(env.db, "run-1", "acc", minutes=90, accommodation_extra_minutes=30)
    seed_attempt(env.db, "run-1", "done", status=AttemptStatus.SUBMITTED, minutes=10)
    env.clock.set(T0 + 70 * MIN)

    assert env.sweeper.sweep_once("run-1", batch_size=50) == 1
    assert _attempt(env, "s1")["status"] == "expired"
    assert _attempt(env, "acc")["status"] == "in_progress"
    assert _attempt(env, "done")["status"] == "submitted"
    assert _attempt(env, "done").get("submit_receipt") is None


def test_a_blocked_attempt_is_sealed_when_its_deadline_passes(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", status=AttemptStatus.BLOCKED, minutes=60)
    env.clock.set(T0 + 61 * MIN)
    assert env.sweeper.sweep_once("run-1", batch_size=50) == 1


def test_repeated_sweeps_do_not_double_seal(env) -> None:
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    env.clock.set(T0 + 61 * MIN)
    assert env.sweeper.sweep_once("run-1", batch_size=50) == 1
    assert env.sweeper.sweep_once("run-1", batch_size=50) == 0
    assert outbox_kinds(env.db).count("attempt_finalized") == 1


def test_the_sweep_is_bounded_by_batch_size(env) -> None:
    uids = [f"s{i}" for i in range(5)]
    seed_run(env.db, enrolled=uids)
    for uid in uids:
        seed_attempt(env.db, "run-1", uid)
    env.clock.set(T0 + 61 * MIN)
    assert [env.sweeper.sweep_once("run-1", batch_size=2) for _ in range(4)] == [2, 2, 1, 0]


def test_two_sweepers_at_once_seal_each_attempt_exactly_once(monkeypatch) -> None:
    for _ in range(15):
        env = make_env(monkeypatch)
        uids = [f"s{i}" for i in range(6)]
        seed_run(env.db, enrolled=uids)
        for uid in uids:
            seed_attempt(env.db, "run-1", uid)
        env.clock.set(T0 + 61 * MIN)
        other = ExamFinalizationSweeper(env.db, clock=env.clock)
        results = _race([lambda: env.sweeper.sweep_once("run-1", batch_size=50),
                         lambda: other.sweep_once("run-1", batch_size=50)])
        assert sum(r[1] for r in results) == 6
        assert outbox_kinds(env.db).count("attempt_finalized") == 6
        assert audit_kinds(env.db).count("exam_attempt_expired") == 6


def _set_ended_at(env, run_id, when):
    doc = env.db.get("exam_runs", run_id)
    doc["ended_at"] = when.isoformat()
    env.db.put("exam_runs", run_id, doc)


def test_the_active_run_sweep_covers_live_closed_and_recently_ended_runs_only(env) -> None:
    layout = {
        "released": RunStatus.RELEASED, "paused": RunStatus.PAUSED, "closed": RunStatus.CLOSED,
        "ended-recent": RunStatus.ENDED, "ended-old": RunStatus.ENDED, "published": RunStatus.PUBLISHED,
        "draft": RunStatus.DRAFT,
    }
    for run_id, status in layout.items():
        seed_run(env.db, run_id=run_id, status=status)
        seed_attempt(env.db, run_id, "s1")
    env.clock.set(T0 + 3 * 24 * 60 * MIN)
    _set_ended_at(env, "ended-recent", env.clock.now() - timedelta(hours=2))
    _set_ended_at(env, "ended-old", env.clock.now() - timedelta(days=2))

    assert env.sweeper.sweep_active_runs_once(batch_size=50) == 4
    sealed = {rid for rid in layout if _attempt(env, "s1", rid)["status"] == "expired"}
    assert sealed == {"released", "paused", "closed", "ended-recent"}


def test_one_failing_run_does_not_stop_the_rest_of_the_sweep(env, monkeypatch) -> None:
    for run_id in ("a", "b"):
        seed_run(env.db, run_id=run_id)
        seed_attempt(env.db, run_id, "s1")
    env.clock.set(T0 + 61 * MIN)
    real = env.sweeper.sweep_once

    def flaky(run_id, *, batch_size):
        if run_id == "a":
            raise RuntimeError("boom")
        return real(run_id, batch_size=batch_size)

    monkeypatch.setattr(env.sweeper, "sweep_once", flaky)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 1
    assert _attempt(env, "s1", "b")["status"] == "expired"


def test_the_exam_api_starts_the_sweeper_loop_behind_its_flag() -> None:
    import inspect

    from app import exam_main

    lifespan_source = inspect.getsource(exam_main.lifespan)
    assert "_finalization_sweep_loop" in lifespan_source and "EXAM_FINALIZATION_SWEEP_ENABLED" in lifespan_source
    loop_source = inspect.getsource(exam_main._finalization_sweep_loop)
    assert "sweep_active_runs_once" in loop_source
    assert loop_source.index("EXAM_SCHEDULE_AUTOMATION_ENABLED") < loop_source.index("run_schedule_automation_once")


# -- schedule automation -------------------------------------------------------------------------------

def _scheduled_run(status, *, start=T0, end=T0 + 120 * MIN, **kw) -> ExamRun:
    return ExamRun(id="run-1", definition_id="d", revision_id="r", status=status, version=1,
                   policy=ExamPolicy(duration_minutes=60, scheduled_start=start, scheduled_end=end),
                   created_at=T0, updated_at=T0, **kw)


@pytest.mark.parametrize("status,offset,expected", [
    (RunStatus.PUBLISHED, -1, None), (RunStatus.PUBLISHED, 0, "release"), (RunStatus.PUBLISHED, 60, "release"),
    (RunStatus.PUBLISHED, 120, None),  # the window passed before anyone released it: never opened late
    (RunStatus.DRAFT, 60, None), (RunStatus.RELEASED, 60, None), (RunStatus.RELEASED, 120, "close"),
    (RunStatus.PAUSED, 121, "close"), (RunStatus.CLOSED, 500, None), (RunStatus.ENDED, 500, None),
])
def test_scheduled_command_table(status, offset, expected) -> None:
    assert decide_scheduled_command(run=_scheduled_run(status), now=T0 + offset * MIN) == expected


def test_a_run_without_a_schedule_is_never_automated() -> None:
    run = ExamRun(id="r", definition_id="d", revision_id="v", status=RunStatus.PUBLISHED, policy=ExamPolicy(duration_minutes=60),
                  created_at=T0, updated_at=T0)
    assert decide_scheduled_command(run=run, now=T0 + 999 * MIN) is None


def _seed_scheduled(env, status, **kw):
    policy = ExamPolicy(duration_minutes=60, scheduled_start=T0, scheduled_end=T0 + 120 * MIN)
    return seed_run(env.db, status=status, policy=policy, **kw)


def test_the_scheduler_releases_a_published_run_once_the_window_opens(env) -> None:
    _seed_scheduled(env, RunStatus.PUBLISHED)
    env.clock.set(T0 - MIN)
    assert env.sweeper.run_schedule_automation_once() == 0
    env.clock.set(T0 + MIN)
    assert env.sweeper.run_schedule_automation_once() == 1
    assert env.sweeper.run_schedule_automation_once() == 0  # already released
    doc = env.db.get("exam_runs", "run-1")
    assert doc["status"] == "released" and doc["version"] == 2
    receipt = env.db.get("exam_command_receipts", "run-1__release__schedule-release")
    assert receipt["actor_uid"] == "system:schedule-automation" and receipt["reason"] == "scheduled window"


def test_the_scheduler_closes_at_the_end_without_cutting_off_an_accommodated_student(env) -> None:
    policy = ExamPolicy(duration_minutes=60, scheduled_start=T0, scheduled_end=T0 + 120 * MIN,
                        accommodations={"acc": AccommodationWindow(extra_minutes=60)})
    seed_run(env.db, status=RunStatus.RELEASED, policy=policy, enrolled=("acc",))
    seed_attempt(env.db, "run-1", "acc", admitted_at=T0 + 90 * MIN, minutes=120, accommodation_extra_minutes=60)
    env.clock.set(T0 + 121 * MIN)

    assert env.sweeper.run_schedule_automation_once() == 1

    assert env.db.get("exam_runs", "run-1")["status"] == "closed"
    acc = _attempt(env, "acc")
    assert acc["status"] == "in_progress" and datetime.fromisoformat(acc["deadline"]) == T0 + 210 * MIN
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 0  # still inside its own deadline
    env.clock.set(T0 + 211 * MIN)
    assert env.sweeper.sweep_active_runs_once(batch_size=10) == 1


def test_a_paused_run_is_also_closed_at_the_end_of_its_window(env) -> None:
    _seed_scheduled(env, RunStatus.PAUSED)
    env.clock.set(T0 + 130 * MIN)
    assert env.sweeper.run_schedule_automation_once() == 1
    assert env.db.get("exam_runs", "run-1")["status"] == "closed"


def test_the_scheduler_never_opens_a_window_that_already_ended(env) -> None:
    _seed_scheduled(env, RunStatus.PUBLISHED)
    env.clock.set(T0 + 200 * MIN)
    assert env.sweeper.run_schedule_automation_once() == 0
    assert env.db.get("exam_runs", "run-1")["status"] == "published"


def test_two_schedulers_at_once_apply_the_release_once(monkeypatch) -> None:
    for _ in range(15):
        env = make_env(monkeypatch)
        _seed_scheduled(env, RunStatus.PUBLISHED)
        env.clock.set(T0 + MIN)
        other = ExamFinalizationSweeper(env.db, clock=env.clock)
        results = _race([env.sweeper.run_schedule_automation_once, other.run_schedule_automation_once])
        assert all(r[0] == "ok" for r in results)
        assert sum(r[1] for r in results) == 1
        assert env.db.get("exam_runs", "run-1")["version"] == 2
