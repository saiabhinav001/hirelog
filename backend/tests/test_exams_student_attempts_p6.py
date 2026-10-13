"""Prompt 6 (ADR 0022): student attempts, drafts and immutable submit - HTTP + service level against the
in-memory Firestore with optimistic transactions (`exam_inmemory.py`). Covers double click, two tabs,
stale autosave, response loss, stale epoch, guessed and future slot ids, late retry, worker crash,
concurrent submit / terminate / expiry, direct API calls and hidden-content leakage."""

from __future__ import annotations

import inspect
import json
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api.routes import exams_judge as judge_routes
from app.api.routes import exams_student as student_routes
from app.core.config import settings
from app.domain.exams.decisions import (
    decide_save_draft,
    decide_submit,
    draft_content_hash,
    resolve_released_slot,
    submit_request_hash,
)
from app.domain.exams.errors import CommandRejectedError
from app.domain.exams.models import Attempt, AttemptStatus, ExamRevision, ExamRun, RunStatus, SubmitReceipt
from app.domain.exams.policy import ExamPolicy
from app.domain.exams.repository import ExamRunRepository
from app.domain.exams.student_service import ExamStudentService
from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.generation.repository import VariantAssignmentRepository
from app.domain.live.events import attempt_status_changed_event, run_state_changed_event
from app.exam_main import app
from exam_inmemory import T0, make_env, seed_attempt, seed_run

BASE = "/api/exams/v1/student"
RUN = "run-1"
REV = "rev-1"
Q0, Q1, Q2 = f"{REV}:slot:0", f"{REV}:slot:1", f"{REV}:slot:2"
SECRETS = ["SECRET-KEY-MARKER", "SECRET-EXPLANATION", "SECRET-RUBRIC", "SECRET-REFERENCE-CODE", "SECRET-HIDDEN", "SECRET-RATIONALE"]


# -- pure decisions -----------------------------------------------------------------------------

def _run(status=RunStatus.RELEASED) -> ExamRun:
    return ExamRun(id=RUN, definition_id="def-1", revision_id=REV, status=status, policy=ExamPolicy(duration_minutes=60), created_at=T0, updated_at=T0)


def _attempt(status=AttemptStatus.IN_PROGRESS, **kw) -> Attempt:
    data = dict(run_id=RUN, uid="s1", status=status, epoch=1, admitted_at=T0, deadline=T0 + timedelta(minutes=60))
    data.update(kw)
    return Attempt(**data)


def _receipt(key="k", request_hash=None) -> SubmitReceipt:
    return SubmitReceipt(idempotency_key=key, finalized_at=T0, receipt_id="r1", request_hash=request_hash)


@pytest.mark.parametrize("status, reason", [
    (AttemptStatus.SUBMITTED, "already_submitted"), (AttemptStatus.EXPIRED, "attempt_expired"), (AttemptStatus.FINALIZED, "already_finalized"),
])
def test_a_retry_with_the_same_key_is_answered_from_the_receipt_in_every_resolved_state(status, reason) -> None:
    attempt = _attempt(status, submit_receipt=_receipt("k"))
    same = decide_submit(attempt=attempt, run=_run(), now=T0 + timedelta(hours=5), idempotency_key="k", client_epoch=99,
                         expected_draft_versions={}, current_draft_versions={"x": 9})
    assert same.accepted and same.reused_existing_receipt and same.receipt.receipt_id == "r1"
    other = decide_submit(attempt=attempt, run=_run(), now=T0, idempotency_key="other", client_epoch=1,
                          expected_draft_versions={}, current_draft_versions={})
    assert not other.accepted and other.conflict_reason == reason


def test_a_reused_key_with_a_different_payload_is_a_conflict() -> None:
    first = submit_request_hash("submit", {"a": 1})
    attempt = _attempt(AttemptStatus.SUBMITTED, submit_receipt=_receipt("k", first))
    same = decide_submit(attempt=attempt, run=_run(), now=T0, idempotency_key="k", client_epoch=1, expected_draft_versions={},
                         current_draft_versions={}, request_hash=submit_request_hash("submit", {"a": 1}))
    assert same.accepted
    for changed in (submit_request_hash("submit", {"a": 2}), submit_request_hash("finish", None)):
        outcome = decide_submit(attempt=attempt, run=_run(), now=T0, idempotency_key="k", client_epoch=1, expected_draft_versions={},
                                current_draft_versions={}, request_hash=changed)
        assert not outcome.accepted and outcome.conflict_reason == "idempotency_key_reused"


def test_the_request_hash_ignores_key_order() -> None:
    assert submit_request_hash("submit", {"a": 1, "b": 2}) == submit_request_hash("submit", {"b": 2, "a": 1})


def _save(attempt=None, **kw):
    data = dict(attempt=attempt or _attempt(), run=_run(), now=T0, expected_version=1, current_version=1)
    data.update(kw)
    return decide_save_draft(**data)


def test_a_save_retried_with_its_own_id_is_acknowledged_again_without_a_new_version() -> None:
    h = draft_content_hash({"selected": "A"}, False)
    replay = _save(expected_version=0, current_version=1, client_save_id="s-1", content_hash=h, last_save_id="s-1", last_content_hash=h)
    assert replay.accepted and replay.replayed and replay.new_version == 1
    changed = _save(expected_version=0, current_version=1, client_save_id="s-1", content_hash=draft_content_hash({"selected": "B"}, False),
                    last_save_id="s-1", last_content_hash=h)
    assert not changed.accepted and changed.conflict_reason == "idempotency_key_reused"
    stale = _save(expected_version=0, current_version=1, client_save_id="s-2", content_hash=h, last_save_id="s-1", last_content_hash=h)
    assert stale.conflict_reason == "draft_version_conflict"


def test_a_save_replay_never_revives_a_sealed_or_late_attempt() -> None:
    h = draft_content_hash({}, False)
    kw = dict(client_save_id="s", content_hash=h, last_save_id="s", last_content_hash=h)
    assert _save(_attempt(AttemptStatus.SUBMITTED), **kw).conflict_reason == "attempt_not_in_progress"
    assert _save(_attempt(deadline=T0 - timedelta(seconds=1)), **kw).conflict_reason == "deadline_passed"


def test_a_stale_epoch_is_refused_on_a_draft_save_but_an_absent_epoch_is_not_checked() -> None:
    assert _save(_attempt(epoch=3), client_epoch=2).conflict_reason == "stale_epoch"
    assert _save(_attempt(epoch=3), client_epoch=3).accepted
    assert _save(_attempt(epoch=3)).accepted


@pytest.mark.parametrize("question_id, index, reason", [
    (Q0, 0, None), (Q1, 1, None), (Q2, None, "slot_not_released"), (f"{REV}:slot:9999", None, "slot_not_released"),
    ("garbage", None, "question_not_in_run"), ("other-rev:slot:0", None, "question_not_in_run"), (f"{REV}:slot:01", None, "question_not_in_run"),
    (f"{REV}:slot:-1", None, "question_not_in_run"), (f"{REV}:slot:1:slot:2", None, "question_not_in_run"), (f"{REV}:slot:", None, "question_not_in_run"),
    (f"{REV}:slot:12345", None, "question_not_in_run"), (f"{REV}:slot:１", None, "question_not_in_run"),
])
def test_only_slots_already_shown_resolve(question_id, index, reason) -> None:
    resolution = resolve_released_slot(revision_id=REV, question_id=question_id, current_slot_index=1)
    assert (resolution.slot_index, resolution.conflict_reason) == (index, reason)


# -- fixtures --------------------------------------------------------------------------------------

def _candidate(cid: str, slot: int, kind=ItemType.SINGLE_CHOICE) -> GenerationCandidate:
    return GenerationCandidate(
        id=cid, request_id="r", blueprint_id="bp", slot_index=slot, family_id="f", local_candidate_id=cid, item_type=kind,
        objective_ids=["o"], difficulty="easy", marks=2, estimated_minutes=3, uniqueness_signature=cid, public_prompt=f"Public prompt {slot}",
        options=[{"key": "A", "text": "Alpha"}, {"key": "B", "text": "Beta"}] if kind == ItemType.SINGLE_CHOICE else [],
        private_expected_answer="SECRET-KEY-MARKER", private_explanation="SECRET-EXPLANATION", marking_rubric={"note": "SECRET-RUBRIC"},
        reference_solutions={"python3.11": "SECRET-REFERENCE-CODE"}, equivalence_rationale="SECRET-RATIONALE",
        public_tests=[{"group_name": "pub", "tests": [{"stdin": "1", "expected_output": "1"}]}],
        hidden_test_groups=[{"group_name": "hid", "weight": 1, "tests": [{"stdin": "SECRET-HIDDEN-IN", "expected_output": "SECRET-HIDDEN-OUT"}]}],
        status=CandidateStatus.APPROVED, created_at=T0,
    )


class Api:
    def __init__(self, env, who) -> None:
        self.env, self.who = env, who
        self.client = TestClient(app)

    def as_(self, uid: str) -> "Api":
        self.who["uid"] = uid
        return self

    def service(self) -> ExamStudentService:
        return ExamStudentService(self.env.db, clock=self.env.clock)

    def save(self, qid=Q0, expected=0, answer=None, **extra):
        return self.client.patch(f"{BASE}/runs/{RUN}/drafts/{qid}", json={"expected_version": expected, "answer": answer if answer is not None else {"selected": "A"}, **extra})

    def submit(self, key="k1", versions=None, epoch=1):
        return self.client.post(f"{BASE}/runs/{RUN}/submit", json={"idempotency_key": key, "client_epoch": epoch, "expected_draft_versions": versions or {}})

    def finish(self, key="k1", epoch=1):
        return self.client.post(f"{BASE}/runs/{RUN}/finish", json={"idempotency_key": key, "client_epoch": epoch})

    def attempt_doc(self, uid="s1") -> dict:
        return self.env.db.get("exam_runs", RUN, "attempts", uid)

    def outbox(self, uid: str) -> list[dict]:
        return [d for d in self.env.db.collection_docs("exam_outbox") if d["kind"] == "attempt_finalized" and d["payload"].get("uid") == uid]


@pytest.fixture
def api(monkeypatch):
    env = make_env(monkeypatch)
    who = {"uid": "s1"}
    for module in (student_routes, judge_routes):
        monkeypatch.setattr(module, "db", env.db)
    monkeypatch.setattr(student_routes, "_service", lambda: ExamStudentService(env.db, clock=env.clock))
    monkeypatch.setattr(judge_routes, "ExamStudentService", lambda db: ExamStudentService(db, clock=env.clock))
    monkeypatch.setattr(judge_routes, "authorize_student_for_run", lambda _u, _r: None)
    user = lambda: {"uid": who["uid"]}  # noqa: E731
    for dependency in (deps.get_current_exam_user, deps.require_student_run_scope, deps.require_seb_admission):
        app.dependency_overrides[dependency] = user
    app.dependency_overrides[deps.require_exams_enabled] = lambda: None

    seed_run(env.db, run_id=RUN, status=RunStatus.RELEASED, revision_id=REV, enrolled=("s1", "s2"))
    revision = ExamRevision(id=REV, definition_id="def-1", revision_number=1, title="t", published_at=T0, published_by="t1", slot_count=3)
    env.db.put("exam_definitions", "def-1", "revisions", REV, revision.model_dump(mode="json"))
    assignments = VariantAssignmentRepository(env.db)
    for slot in range(3):
        env.db.put("generation_candidates", f"c{slot}", _candidate(f"c{slot}", slot).model_dump(mode="json"))
        for uid in ("s1", "s2"):
            assignments.save_assignment(run_id=RUN, uid=uid, slot_index=slot, candidate_id=f"c{slot}", now=T0)
    seed_attempt(env.db, RUN, "s1")
    seed_attempt(env.db, RUN, "s2")
    yield Api(env, who)
    app.dependency_overrides.clear()


# -- released-slot gating --------------------------------------------------------------------------

def test_a_guessed_or_future_question_id_creates_no_draft_and_is_refused(api) -> None:
    for qid, status in ((Q2, 403), (Q1, 403), ("garbage", 404), ("other-rev:slot:0", 404), (f"{REV}:slot:00", 404), (f"{REV}:slot:99999", 404)):
        response = api.save(qid)
        assert response.status_code == status, (qid, response.text)
    assert [k for k in api.env.db.docs if "drafts" in k] == []
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q2}").status_code == 403
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/garbage").status_code == 404


def test_only_the_current_slot_can_be_fetched_by_default(api) -> None:
    assert api.client.get(f"{BASE}/runs/{RUN}/question").json()["question_id"] == Q0
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/0").status_code == 200
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/1").status_code == 403
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/2").json()["detail"] == "slot_not_released"
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/3").status_code == 404
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/-1").status_code == 404
    assert api.client.post(f"{BASE}/runs/{RUN}/next-question").status_code == 200
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/1").json()["slot_index"] == 1
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/0").json()["detail"] == "slot_revisit_disabled"


def test_earlier_released_slots_can_be_reopened_when_the_flag_is_on(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_STUDENT_REVISIT_RELEASED_SLOTS", True)
    api.client.post(f"{BASE}/runs/{RUN}/next-question")
    reopened = api.client.get(f"{BASE}/runs/{RUN}/questions/0")
    assert reopened.status_code == 200 and reopened.json()["question_id"] == Q0
    assert api.client.get(f"{BASE}/runs/{RUN}/questions/2").status_code == 403  # still never the future


def test_the_navigator_lists_released_slots_without_content_and_counts_locked(api) -> None:
    assert api.save(Q0, 0, {"selected": "A"}, marked_for_review=True).status_code == 200
    body = api.client.get(f"{BASE}/runs/{RUN}/slots").json()
    assert (body["current_slot_index"], body["total_slots"], body["locked_count"]) == (0, 3, 2)
    assert body["slots"] == [{"slot_index": 0, "question_id": Q0, "answered": True, "marked_for_review": True, "version": 1}]
    api.client.post(f"{BASE}/runs/{RUN}/next-question")
    body = api.client.get(f"{BASE}/runs/{RUN}/slots").json()
    assert [s["answered"] for s in body["slots"]] == [True, False] and body["locked_count"] == 1
    assert "prompt" not in json.dumps(body) and "options" not in json.dumps(body)


def test_a_saved_draft_reports_what_the_navigator_will_show_so_the_client_need_not_ask_again(api) -> None:
    assert api.save(Q0, 0, {"selected": "A"}).json()["answered"] is True
    assert api.save(Q0, 1, {}).json()["answered"] is False
    saved = api.save(Q0, 2, {"selected": "B"}, client_save_id="s-1").json()
    replay = api.save(Q0, 2, {"selected": "B"}, client_save_id="s-1").json()
    assert (saved["answered"], replay["answered"], replay["replayed"]) == (True, True, True)
    slot = api.client.get(f"{BASE}/runs/{RUN}/slots").json()["slots"][0]
    assert (slot["answered"], slot["version"]) == (True, 3)


def test_marking_for_review_is_a_flag_that_a_later_save_keeps_unless_told_otherwise(api) -> None:
    api.save(Q0, 0, {"selected": "A"}, marked_for_review=True)
    api.save(Q0, 1, {"selected": "B"})  # flag omitted: unchanged
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}").json() == {"version": 2, "answer": {"selected": "B"}, "marked_for_review": True}
    api.save(Q0, 2, {"selected": "B"}, marked_for_review=False)
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}").json()["marked_for_review"] is False


# -- draft saves: versions, idempotency, size, epoch ---------------------------------------------------------------

def test_a_stale_autosave_and_a_second_tab_get_an_explicit_conflict_not_an_overwrite(api) -> None:
    assert api.save(Q0, 0, {"selected": "A"}).json()["version"] == 1  # tab A
    tab_b = api.save(Q0, 0, {"selected": "B"})  # tab B was still on version 0
    assert tab_b.status_code == 409 and tab_b.json()["detail"]["code"] == "draft_version_conflict"
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}").json()["answer"] == {"selected": "A"}
    assert api.save(Q0, 1, {"selected": "B"}).json()["version"] == 2  # B reloads, then saves


def test_a_lost_save_response_is_recovered_by_retrying_with_the_same_save_id(api) -> None:
    first = api.save(Q0, 0, {"selected": "A"}, client_save_id="save-1").json()
    retry = api.save(Q0, 0, {"selected": "A"}, client_save_id="save-1").json()
    assert (first["version"], retry["version"], retry["replayed"]) == (1, 1, True)
    reused = api.save(Q0, 0, {"selected": "B"}, client_save_id="save-1")
    assert reused.status_code == 409 and reused.json()["detail"]["code"] == "idempotency_key_reused"
    assert api.save(Q0, 1, {"selected": "B"}, client_save_id="save-2").json()["version"] == 2


def test_an_oversized_answer_is_refused_and_not_stored(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_DRAFT_MAX_BYTES", 200)
    assert api.save(Q0, 0, {"source_code": "x" * 500}).status_code == 413
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}").json()["version"] == 0


def test_a_stale_epoch_is_refused_for_drafts_and_submit(api) -> None:
    assert api.save(Q0, 0, client_epoch=0).json()["detail"]["code"] == "stale_epoch"
    assert api.save(Q0, 0, client_epoch=1).status_code == 200
    api.env.db.put("exam_runs", RUN, "attempts", "s1", {**api.attempt_doc(), "epoch": 5})
    assert api.save(Q0, 1, client_epoch=1).status_code == 409
    assert api.submit(epoch=1).json()["detail"]["code"] == "stale_epoch"
    assert api.attempt_doc()["status"] == "in_progress"


# -- submit: double click, response loss, late retry, key reuse --------------------------------------------------------

def test_a_double_click_submit_returns_one_receipt_and_one_grading_trigger(api) -> None:
    api.save(Q0, 0)
    first, second = api.submit("k1", {Q0: 1}), api.submit("k1", {Q0: 1})
    assert first.status_code == second.status_code == 200
    assert first.json()["receipt"]["receipt_id"] == second.json()["receipt"]["receipt_id"]
    assert api.attempt_doc()["status"] == "submitted" and len(api.outbox("s1")) == 1


def test_a_lost_submit_response_is_recovered_after_the_deadline_without_allowing_late_work(api) -> None:
    api.save(Q0, 0, {"selected": "A"})
    receipt_id = api.submit("k1", {Q0: 1}).json()["receipt"]["receipt_id"]
    api.env.clock.advance(timedelta(hours=3))  # far past the 60 minute deadline
    retry = api.submit("k1", {Q0: 1})
    assert retry.status_code == 200 and retry.json()["receipt"]["receipt_id"] == receipt_id
    assert api.client.get(f"{BASE}/runs/{RUN}/receipt").json()["receipt_id"] == receipt_id
    assert api.finish("brand-new-key").json()["detail"]["code"] == "already_submitted"
    assert api.save(Q0, 1, {"selected": "B"}).status_code == 409
    assert api.attempt_doc()["submit_receipt"]["sealed_draft_versions"] == {Q0: 1}
    assert len(api.outbox("s1")) == 1


def test_the_same_key_with_a_different_payload_is_a_conflict_not_a_second_submit(api) -> None:
    api.save(Q0, 0)
    receipt_id = api.submit("k1", {Q0: 1}).json()["receipt"]["receipt_id"]
    changed = api.submit("k1", {Q0: 2})
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_key_reused"
    assert api.finish("k1").json()["detail"]["code"] == "idempotency_key_reused"  # a different command under the same key
    assert api.client.get(f"{BASE}/runs/{RUN}/receipt").json()["receipt_id"] == receipt_id


def test_submit_with_stale_draft_versions_is_a_conflict_and_seals_nothing(api) -> None:
    api.save(Q0, 0)
    stale = api.submit("k1", {Q0: 0})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "draft_version_conflict"
    assert api.attempt_doc()["status"] == "in_progress" and api.outbox("s1") == []


def test_finish_seals_the_last_acknowledged_draft_and_blank_slots_stay_blank(api) -> None:
    api.save(Q0, 0)
    api.client.post(f"{BASE}/runs/{RUN}/next-question")
    receipt = api.finish("done-1").json()["receipt"]
    assert receipt["sealed_draft_versions"] == {Q0: 1}  # slot 1 was never answered: absent = blank
    assert api.finish("done-1").json()["receipt"]["receipt_id"] == receipt["receipt_id"]


# -- ownership / direct API calls ----------------------------------------------------------------------------------------

def test_a_student_only_ever_touches_their_own_attempt_and_drafts(api) -> None:
    api.save(Q0, 0, {"selected": "A"})
    api.as_("s2")
    assert api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}").json()["version"] == 0  # not s1's draft
    assert api.save(Q0, 0, {"selected": "B"}).json()["version"] == 1
    assert api.env.db.get("exam_runs", RUN, "attempts", "s1", "drafts", Q0)["answer"] == {"selected": "A"}
    assert api.submit("k", {Q0: 1}).status_code == 200
    assert api.attempt_doc("s1")["status"] == "in_progress"


def test_direct_calls_by_a_student_who_is_not_enrolled_or_not_admitted_are_refused(api) -> None:
    api.as_("outsider")
    for response in (api.client.get(f"{BASE}/runs/{RUN}/question"), api.client.get(f"{BASE}/runs/{RUN}/slots"), api.save(Q0), api.submit(),
                     api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}"), api.client.get(f"{BASE}/runs/{RUN}/receipt")):
        assert response.status_code in (403, 404), response.text
    api.env.db.put("exam_runs", RUN, "attempts", "s2", {**api.attempt_doc("s2"), "status": "not_started"})
    api.as_("s2")
    assert api.client.get(f"{BASE}/runs/{RUN}/question").status_code == 403
    assert api.save(Q0).status_code == 409


def test_content_stops_when_the_run_is_not_released_or_the_deadline_has_passed(api) -> None:
    api.env.clock.advance(timedelta(minutes=61))
    assert api.client.get(f"{BASE}/runs/{RUN}/question").json()["detail"] == "deadline_passed"
    assert api.client.get(f"{BASE}/runs/{RUN}/slots").status_code == 403
    assert api.save(Q0).json()["detail"]["code"] == "deadline_passed"


def test_the_public_test_run_refuses_guessed_future_and_sealed_questions(api) -> None:
    body = lambda qid: {"run_id": RUN, "question_id": qid, "language_key": "python3.11", "source_code": "print(1)"}  # noqa: E731
    url = f"{BASE}/judge/public-run"
    assert api.client.post(url, json=body("garbage")).status_code == 404
    assert api.client.post(url, json=body(f"{REV}:slot:abc")).status_code == 404
    assert api.client.post(url, json=body(Q1)).status_code == 403
    assert api.client.get(f"{BASE}/judge/public-run/x", params={"run_id": RUN, "question_id": Q2}).status_code == 403
    api.submit("k")
    assert api.client.post(url, json=body(Q0)).json()["detail"] == "attempt_not_active"


# -- concurrency -------------------------------------------------------------------------------------------------------------

def _race(*targets) -> list:
    results: list = [None] * len(targets)

    def run(i, fn):
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - the loser of a race may raise a domain error
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _fresh_attempt(api, uid: str, **extra) -> None:
    run = api.env.db.get("exam_runs", RUN)
    api.env.db.put("exam_runs", RUN, {**run, "enrolled_uids": [*run["enrolled_uids"], uid]})
    ExamRunRepository(api.env.db).invalidate(RUN)  # a write that bypasses the repository must drop the cached run
    seed_attempt(api.env.db, RUN, uid, **extra)


def test_two_tabs_saving_from_the_same_version_produce_exactly_one_winner(api) -> None:
    service = api.service()
    for i in range(10):
        uid = f"t{i}"
        _fresh_attempt(api, uid)
        results = _race(
            lambda: service.save_draft(RUN, uid, Q0, expected_version=0, answer={"selected": "A"}),
            lambda: service.save_draft(RUN, uid, Q0, expected_version=0, answer={"selected": "B"}),
        )
        assert sorted(r.accepted for r in results) == [False, True]
        assert api.env.db.get("exam_runs", RUN, "attempts", uid, "drafts", Q0)["version"] == 1


def test_concurrent_submit_and_faculty_terminate_produce_one_serialized_outcome(api) -> None:
    service = api.service()
    outcomes = set()
    for i in range(15):
        uid = f"x{i}"
        _fresh_attempt(api, uid)
        results = _race(
            lambda: service.submit(RUN, uid, idempotency_key=f"k{i}", client_epoch=1, expected_draft_versions={}),
            lambda: api.env.service.terminate_attempt(run_id=RUN, uid=uid, idempotency_key=f"t{i}", actor_uid="teacher", reason="test"),
        )
        doc = api.attempt_doc(uid)
        assert doc["status"] in ("submitted", "expired") and doc["submit_receipt"]["receipt_id"]
        assert len(api.outbox(uid)) == 1
        submit_won = getattr(results[0], "accepted", False)
        assert submit_won == (doc["status"] == "submitted")
        assert isinstance(results[1], CommandRejectedError) == submit_won
        outcomes.add(doc["status"])
    assert outcomes  # either side may win; what matters is that never both did


def test_a_submit_racing_the_sweeper_after_the_deadline_is_sealed_once_as_expired(api) -> None:
    api.env.db.docs.pop(("exam_runs", RUN, "attempts", "s2"))  # keep the sweeper's work to s1 only
    service = api.service()
    api.save(Q0, 0)
    api.env.clock.advance(timedelta(minutes=61))
    results = _race(
        lambda: service.finish(RUN, "s1", idempotency_key="late", client_epoch=1),
        lambda: api.env.sweeper.sweep_once(RUN, batch_size=10),
        lambda: api.env.sweeper.sweep_once(RUN, batch_size=10),
    )
    assert results[0].accepted is False and results[0].conflict_reason in ("deadline_passed", "attempt_expired")
    assert api.attempt_doc()["status"] == "expired" and len(api.outbox("s1")) == 1
    assert api.attempt_doc()["submit_receipt"]["sealed_draft_versions"] == {Q0: 1}
    assert sum(r for r in results[1:] if isinstance(r, int)) == 1


def test_a_draft_saved_between_the_read_and_the_commit_is_not_missed_by_the_seal(api, monkeypatch) -> None:
    from app.domain.exams.repository import DraftRepository

    service = api.service()
    _fresh_attempt(api, "late", current_slot_index=1)
    real, fired = DraftRepository.all_versions_in_transaction, {"done": False}

    def read_then_race(self, transaction, run_id, uid):
        versions = real(self, transaction, run_id, uid)
        if uid == "late" and not fired["done"]:
            fired["done"] = True  # a concurrent writer commits right after the seal read its snapshot
            assert service.save_draft(RUN, "late", Q1, expected_version=0, answer={"selected": "B"}).accepted
        return versions

    monkeypatch.setattr(DraftRepository, "all_versions_in_transaction", read_then_race)
    outcome = service.finish(RUN, "late", idempotency_key="f", client_epoch=1)
    assert outcome.accepted and outcome.receipt.sealed_draft_versions == {Q1: 1}
    assert api.attempt_doc("late")["submit_receipt"]["sealed_draft_versions"] == {Q1: 1}


def test_the_receipt_always_lists_exactly_the_drafts_that_exist_when_sealing(api) -> None:
    service = api.service()
    for i in range(30):
        uid = f"d{i}"
        _fresh_attempt(api, uid, current_slot_index=1)
        service.save_draft(RUN, uid, Q0, expected_version=0, answer={"selected": "A"})
        _race(
            lambda: service.finish(RUN, uid, idempotency_key=f"f{i}", client_epoch=1),
            lambda: service.save_draft(RUN, uid, Q1, expected_version=0, answer={"selected": "B"}),
        )
        sealed = api.attempt_doc(uid)["submit_receipt"]["sealed_draft_versions"]
        stored = {k[-1]: v["data"]["version"] for k, v in api.env.db.docs.items() if k[:4] == ("exam_runs", RUN, "attempts", uid) and "drafts" in k}
        assert sealed == stored, (i, sealed, stored)


def test_a_sweeper_that_crashes_mid_seal_leaves_nothing_and_the_next_pass_seals_once(api, monkeypatch) -> None:
    api.env.db.docs.pop(("exam_runs", RUN, "attempts", "s2"))
    api.save(Q0, 0)
    api.env.clock.advance(timedelta(minutes=61))
    real = api.env.sweeper._outbox.add_in_transaction
    calls = {"n": 0}

    def crash_once(transaction, entry):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker died")
        return real(transaction, entry)

    monkeypatch.setattr(api.env.sweeper._outbox, "add_in_transaction", crash_once)
    with pytest.raises(RuntimeError):
        api.env.sweeper.sweep_once(RUN, batch_size=10)
    doc = api.attempt_doc()
    assert doc["status"] == "in_progress" and not doc.get("submit_receipt") and api.outbox("s1") == []
    assert api.env.sweeper.sweep_once(RUN, batch_size=10) == 1
    assert api.env.sweeper.sweep_once(RUN, batch_size=10) == 0
    assert len(api.outbox("s1")) == 1 and api.attempt_doc()["status"] == "expired"


def test_expiry_seals_the_last_acknowledged_draft_or_blank_exactly_once(api) -> None:
    api.save(Q0, 0, {"selected": "A"})
    api.save(Q0, 1, {"selected": "B"})
    api.env.clock.advance(timedelta(minutes=61))
    api.env.sweeper.sweep_once(RUN, batch_size=10)
    first = api.attempt_doc()["submit_receipt"]
    assert first["sealed_draft_versions"] == {Q0: 2}
    api.env.sweeper.sweep_once(RUN, batch_size=10)
    assert api.attempt_doc()["submit_receipt"] == first
    api.env.clock.advance(timedelta(seconds=1))
    assert api.save(Q0, 2, {"selected": "A"}).status_code == 409  # no write can change what was sealed
    api.env.sweeper.sweep_once(RUN, batch_size=10)
    seal_a = api.attempt_doc("s2")["submit_receipt"]  # s2 never answered anything: sealed blank
    assert seal_a["sealed_draft_versions"] == {}


# -- hidden-content leakage and caching ----------------------------------------------------------------------------------------

def _student_routes():
    return [r for r in list(student_routes.router.routes) + list(judge_routes.student_router.routes) if hasattr(r, "endpoint")]


def test_the_grading_trigger_skips_a_draft_id_that_is_not_a_slot_id() -> None:
    from app.domain.judge.outbox_handler import _parse_slot_index

    assert _parse_slot_index(Q1) == 1
    assert [_parse_slot_index(x) for x in ("garbage", f"{REV}:slot:", f"{REV}:slot:x", "")] == [None, None, None, None]


def test_every_student_route_sets_no_store() -> None:
    missing = [r.path for r in _student_routes() if "no-store" not in inspect.getsource(r.endpoint) and "_set_no_store" not in inspect.getsource(r.endpoint)]
    assert missing == []


def test_no_student_response_carries_private_content_and_all_are_no_store(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_STUDENT_REVISIT_RELEASED_SLOTS", True)
    seen: list[tuple[str, str, dict]] = []

    def record(label, response):
        seen.append((label, response.text, dict(response.headers)))
        return response

    record("runs", api.client.get(f"{BASE}/runs"))
    record("accommodations", api.client.get(f"{BASE}/runs/{RUN}/accommodations"))
    record("state", api.client.get(f"{BASE}/runs/{RUN}/state"))
    record("q0", api.client.get(f"{BASE}/runs/{RUN}/question"))
    record("save", api.save(Q0, 0, {"selected": "A"}))
    record("draft", api.client.get(f"{BASE}/runs/{RUN}/drafts/{Q0}"))
    record("versions", api.client.get(f"{BASE}/runs/{RUN}/drafts"))
    record("next", api.client.post(f"{BASE}/runs/{RUN}/next-question"))
    record("q1", api.client.get(f"{BASE}/runs/{RUN}/question"))
    record("q0-again", api.client.get(f"{BASE}/runs/{RUN}/questions/0"))
    record("slots", api.client.get(f"{BASE}/runs/{RUN}/slots"))
    record("future", api.client.get(f"{BASE}/runs/{RUN}/questions/2"))
    record("bad-draft", api.client.get(f"{BASE}/runs/{RUN}/drafts/garbage"))
    record("submit", api.submit("k", {Q0: 1}))
    record("receipt", api.client.get(f"{BASE}/runs/{RUN}/receipt"))
    record("state-after", api.client.get(f"{BASE}/runs/{RUN}/state"))
    assert len(seen) == 16
    for label, text, headers in seen:
        assert not [s for s in SECRETS if s in text], f"{label} leaked a private field"
        assert headers.get("cache-control") == "no-store", label
    q1 = json.loads(dict((label, text) for label, text, _ in seen)["q1"])
    assert q1["public_content"]["prompt"] == "Public prompt 1" and "private_expected_answer" not in q1["public_content"]


def test_live_events_carry_no_question_or_answer_material() -> None:
    attempt = _attempt(submit_receipt=_receipt("k"))
    blob = attempt_status_changed_event(attempt, seq=1, now=T0).model_dump_json() + run_state_changed_event(_run(), seq=2, now=T0).model_dump_json()
    assert not [s for s in SECRETS if s in blob] and "prompt" not in blob and "answer" not in blob
