"""Prompt 10: scoped gradebook, released results, regrade lineage, exports.

HTTP-level tests over the real routers and repositories (in-memory Firestore); only authentication is replaced, so
the real resource-scope checks (`require_run_scope`, `require_export_job_scope`, `require_grade_appeal_scope`) run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_gradebook as gradebook_routes
from app.api.routes import exams_judge as judge_routes
from app.domain.authz.policy import ExamPrincipal
from app.domain.exams.models import Attempt, AttemptStatus, Draft, ExamDefinition, ExamRevision, OutboxEntry, RunStatus
from app.domain.generation.models import CandidateStatus, GenerationCandidate, ItemType
from app.domain.generation.repository import VariantAssignmentRepository
from app.domain.gradebook.export_service import GradebookExportService
from app.domain.judge.models import McqGradeResult
from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler
from app.domain.judge.repository import McqResultRepository, ShortAnswerReviewRepository
from app.domain.judge.service import JudgeService
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage
from app.exam_main import app
from exam_inmemory import T0, audit_kinds, make_env, seed_run

RUN, REV, DEF = "run-1", "rev-1", "def-1"
Q0, Q1, Q2 = f"{REV}:slot:0", f"{REV}:slot:1", f"{REV}:slot:2"
STUDENT = "/api/exams/v1/student/gradebook"
FACULTY = "/api/exams/v1/faculty/gradebook"
HOSTILE_UID = "=HYPERLINK(\"http://evil.example\",\"x\")"
INTERNAL = "INTERNAL-PROVENANCE"


def _candidate(slot: int, kind: ItemType, *, key: str | None, marks: float) -> GenerationCandidate:
    return GenerationCandidate(
        id=f"c{slot}", request_id="r", blueprint_id="bp", slot_index=slot, family_id="f", local_candidate_id=f"c{slot}",
        item_type=kind, objective_ids=["o"], difficulty="easy", marks=marks, estimated_minutes=3,
        uniqueness_signature=f"c{slot}", public_prompt=f"Public prompt {slot}", private_expected_answer=key,
        status=CandidateStatus.APPROVED, created_at=T0,
    )


def _principal(uid: str) -> dict:
    principal = ExamPrincipal(
        uid=uid, email=f"{uid}@college.edu", email_verified=True, bootstrap_super_admin=False, bootstrap_teacher=True,
        memberships=(),
    )
    return {"uid": uid, deps._PRINCIPAL_KEY: principal}


class World:
    def __init__(self, env, who, storage) -> None:
        self.env, self.who, self.storage = env, who, storage
        self.client = TestClient(app)

    # -- identities ---------------------------------------------------------------
    def student(self, uid: str) -> "World":
        self.who["student"] = uid
        return self

    def teacher(self, uid: str) -> "World":
        self.who["teacher"] = uid
        return self

    # -- seeding / grading ------------------------------------------------------------
    def draft(self, uid: str, question_id: str, answer: dict) -> None:
        self.env.db.put("exam_runs", RUN, "attempts", uid, "drafts", question_id, Draft(question_id=question_id, version=1, answer=answer).model_dump(mode="json"))

    def grade_everyone(self) -> None:
        handler = GradingTriggerOutboxHandler(self.env.db, judge_provider=None, allowlist=None)
        for uid in ("s1", "s2"):
            handler.handle(OutboxEntry(id=f"o-{uid}", kind="attempt_finalized", run_id=RUN, payload={"uid": uid}, created_at=T0))

    def review_short_answers(self, score: float) -> None:
        judge = JudgeService(self.env.db, None)
        for review in ShortAnswerReviewRepository(self.env.db).list_pending():
            judge.submit_short_answer_review(review.id, score=score, rubric_notes="ok", reviewer="owner")

    def release(self, *, note: str = "", acknowledge: bool = True):
        return self.client.post(f"{FACULTY}/runs/{RUN}/release", json={"note": note, "acknowledge_incomplete": acknowledge})

    def results(self, uid: str):
        return self.student(uid).client.get(f"{STUDENT}/runs/{RUN}/results")


@pytest.fixture
def world(monkeypatch):
    env = make_env(monkeypatch)
    who = {"student": "s1", "teacher": "owner"}
    storage = InMemoryEvidenceStorage()
    for module in (gradebook_routes, judge_routes, scope):
        monkeypatch.setattr(module, "db", env.db)
    monkeypatch.setattr(gradebook_routes, "_storage", lambda: storage)
    app.dependency_overrides[deps.require_student_run_scope] = lambda: {"uid": who["student"]}
    app.dependency_overrides[deps.require_exam_faculty] = lambda: _principal(who["teacher"])
    app.dependency_overrides[deps.require_exams_enabled] = lambda: None

    env.db.put("exam_definitions", DEF, ExamDefinition(id=DEF, title="Midterm", owner_uid="owner", created_at=T0, updated_at=T0).model_dump(mode="json"))
    env.db.put("exam_definitions", DEF, "revisions", REV, ExamRevision(id=REV, definition_id=DEF, revision_number=1, title="Midterm", published_at=T0, published_by="owner", slot_count=3).model_dump(mode="json"))
    seed_run(env.db, run_id=RUN, status=RunStatus.ENDED, revision_id=REV, enrolled=("s1", "s2", "s3", HOSTILE_UID))
    candidates = (
        _candidate(0, ItemType.SINGLE_CHOICE, key="A", marks=2.0),
        _candidate(1, ItemType.MULTIPLE_SELECT, key="A,C", marks=3.0),
        _candidate(2, ItemType.SHORT_ANSWER, key=None, marks=5.0),
    )
    assignments = VariantAssignmentRepository(env.db)
    for candidate in candidates:
        env.db.put("generation_candidates", candidate.id, candidate.model_dump(mode="json"))
        for uid in ("s1", "s2", "s3"):
            assignments.save_assignment(run_id=RUN, uid=uid, slot_index=candidate.slot_index, candidate_id=candidate.id, now=T0)
    for uid in ("s1", "s2"):  # s3 never started: enrolled and assigned, but no attempt
        env.db.put("exam_runs", RUN, "attempts", uid, Attempt(
            run_id=RUN, uid=uid, status=AttemptStatus.FINALIZED, current_slot_index=2, admitted_at=T0,
            deadline=T0 + timedelta(minutes=60), updated_at=T0,
        ).model_dump(mode="json"))
    w = World(env, who, storage)
    w.draft("s1", Q0, {"selected": "A"})
    w.draft("s1", Q1, {"selected": ["C", "A"]})  # the exam UI sends a list for multiple select; order is irrelevant
    w.draft("s1", Q2, {"text": "my reasoning"})
    w.draft("s2", Q0, {"selected": "B"})
    w.draft("s2", Q1, {"selected": ["A"]})  # incomplete set: wrong, and no partial credit
    yield w
    app.dependency_overrides.clear()


def _rows(world: World) -> dict:
    body = world.client.get(f"{FACULTY}/runs/{RUN}/rows").json()
    return {row["uid"]: row for row in body["results"]}


def _counts(world: World) -> dict:
    return {c["question_id"]: c for c in world.client.get(f"{FACULTY}/runs/{RUN}/counts").json()["results"]}


# -- scoring semantics ---------------------------------------------------------------------------------------

def test_multiple_select_is_graded_as_an_exact_set_with_no_partial_credit(world) -> None:
    world.grade_everyone()
    mcq = McqResultRepository(world.env.db)
    results = {(r.uid, r.question_id): r.correct for uid in ("s1", "s2") for r in mcq.list_for_attempt(RUN, uid)}
    assert results == {("s1", Q0): True, ("s1", Q1): True, ("s2", Q0): False, ("s2", Q1): False}
    rows = _rows(world)
    assert (rows["s1"]["raw_score"], rows["s1"]["raw_max_score"]) == (5.0, 10.0)  # 2 + 3; the short answer is not reviewed yet
    assert (rows["s2"]["raw_score"], rows["s2"]["raw_max_score"]) == (0.0, 10.0)  # a wrong set scores 0, never a fraction


def test_grade_counts_have_the_documented_meaning(world) -> None:
    world.grade_everyone()
    counts = _counts(world)
    q0, q1, q2 = counts[Q0], counts[Q1], counts[Q2]
    # assigned = every enrolled student with a variant for the slot; presented = reached the slot (a student who never
    # started has not); finalized = attempt sealed; nonblank = has an answer; blank never counts as solved.
    assert (q0["assigned"], q0["presented"], q0["finalized"], q0["nonblank_attempted"]) == (3, 2, 2, 2)
    assert (q0["fully_solved"], q0["partially_solved"], q0["blank"], q0["pending"]) == (1, 1, 1, 0)
    assert (q1["fully_solved"], q1["partially_solved"], q1["blank"], q1["pending"]) == (1, 1, 1, 0)
    # An answered short answer is PENDING until a human reviews it, never a silent zero.
    assert (q2["fully_solved"], q2["partially_solved"], q2["blank"], q2["pending"], q2["nonblank_attempted"]) == (0, 0, 2, 1, 1)
    # Every student is in exactly one outcome bucket per question.
    for c in (q0, q1, q2):
        assert c["fully_solved"] + c["partially_solved"] + c["blank"] + c["pending"] == c["assigned"]
    world.review_short_answers(4.0)
    q2 = _counts(world)[Q2]
    assert (q2["partially_solved"], q2["pending"]) == (1, 0)


def test_a_student_who_never_started_is_not_counted_as_presented(world) -> None:
    rows = _rows(world)
    assert all(q["presented"] is False for q in rows["s3"]["question_results"])
    assert rows["s3"]["attempt_status"] == "not_started"


def test_a_short_answer_score_above_the_maximum_is_refused(world) -> None:
    world.grade_everyone()
    review = ShortAnswerReviewRepository(world.env.db).list_pending()[0]
    resp = world.client.post(f"/api/exams/v1/faculty/judge/short-answer-reviews/{review.id}", json={"score": 5.5, "rubric_notes": "typo"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "score_out_of_range"
    assert ShortAnswerReviewRepository(world.env.db).get(review.id).score is None
    assert world.client.post(f"/api/exams/v1/faculty/judge/short-answer-reviews/{review.id}", json={"score": 5.0, "rubric_notes": "full"}).status_code == 200


def test_a_re_review_keeps_the_earlier_score_in_the_history(world) -> None:
    world.grade_everyone()
    world.review_short_answers(2.0)
    world.review_short_answers(2.0)  # nothing pending any more: the first review stays
    review = ShortAnswerReviewRepository(world.env.db).list_latest_for_attempt(RUN, "s1")[0]
    JudgeService(world.env.db, None).submit_short_answer_review(review.id, score=4.0, rubric_notes="second look", reviewer="owner2")
    current = ShortAnswerReviewRepository(world.env.db).get(review.id)
    assert (current.score, current.version, current.reviewed_by) == (4.0, 2, "owner2")
    assert [(h["version"], h["score"], h["reviewed_by"]) for h in current.history] == [(1, 2.0, "owner")]


def test_defective_resolutions_apply_the_latest_one_whatever_order_the_store_returns(world) -> None:
    world.grade_everyone()
    base = {"run_id": RUN, "question_id": Q0, "reason": "key was wrong", "resolved_by": "owner"}
    # The newer decision has the alphabetically FIRST id, so a last-write-wins-by-iteration bug would pick the older one.
    world.env.db.put("gradebook_defective_questions", "zzz", {**base, "resolution": "exclude_from_total", "resolved_at": T0.isoformat()})
    world.env.db.put("gradebook_defective_questions", "aaa", {**base, "resolution": "full_credit_all", "resolved_at": (T0 + timedelta(hours=1)).isoformat()})
    s2_q0 = next(q for q in _rows(world)["s2"]["question_results"] if q["question_id"] == Q0)
    assert (s2_q0["weighted_score"], s2_q0["max_score"]) == (2.0, 2.0)  # full credit (the newer decision), not excluded


def test_a_defective_resolution_for_a_question_outside_the_run_is_refused(world) -> None:
    for bad in ("garbage", "other-rev:slot:0", f"{REV}:slot:9"):
        assert world.client.post(f"{FACULTY}/runs/{RUN}/questions/{bad}/defective", json={"resolution": "full_credit_all", "reason": "x"}).status_code == 404
    assert world.env.db.collection_docs("gradebook_defective_questions") == []
    ok = world.client.post(f"{FACULTY}/runs/{RUN}/questions/{Q0}/defective", json={"resolution": "full_credit_all", "reason": "key was wrong"})
    assert ok.status_code == 200 and "exam_question_resolved_defective" in audit_kinds(world.env.db)


def test_similarity_evidence_never_changes_a_score_or_reaches_a_student(world) -> None:
    world.grade_everyone()
    before = _rows(world)["s1"]["raw_score"]
    world.env.db.put("similarity_matches", "m1", {
        "id": "m1", "run_id": RUN, "question_id": Q0, "language": "python", "uid_a": "s1", "uid_b": "s2", "submission_id_a": "x", "submission_id_b": "y",
        "similarity_percent": 99.0, "matched_spans": [], "provider": "deterministic_fake", "computed_at": T0.isoformat(),
    })
    assert _rows(world)["s1"]["raw_score"] == before
    assert world.release().status_code == 200
    assert "similar" not in world.results("s1").text.lower()


# -- release: unreleased denial, frozen results, lineage ---------------------------------------------------------------

def test_a_student_gets_nothing_before_a_release_and_only_their_own_row_after(world) -> None:
    world.grade_everyone()
    denied = world.results("s1")
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "results_not_released"
    assert world.student("s1").client.get(f"{STUDENT}/runs/{RUN}/feedback").status_code == 403
    assert world.release(acknowledge=True).status_code == 200  # pending short answer: acknowledged
    mine = world.results("s1")
    assert mine.status_code == 200 and mine.headers["cache-control"] == "no-store"
    assert mine.json()["row"]["uid"] == "s1" and mine.json()["row"]["raw_score"] == 5.0 and mine.json()["release_version"] == 1
    assert world.results("s2").json()["row"]["uid"] == "s2"
    assert "s1" not in world.results("s2").text.replace("s1_", "")  # no other student's data in the response


def test_the_student_view_has_no_internal_grading_provenance(world) -> None:
    world.grade_everyone()
    world.release()
    body = world.results("s1").json()["row"]
    for question in body["question_results"]:
        assert question["submission_hash"] is None and question["runtime_image"] is None and question["test_suite_version"] is None
    assert INTERNAL not in str(body)


def test_releasing_an_incomplete_gradebook_needs_acknowledgement_and_writes_nothing_otherwise(world) -> None:
    world.grade_everyone()  # s1's short answer is not reviewed: 1 pending question
    refused = world.release(acknowledge=False)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "release_incomplete"
    assert world.env.db.collection_docs("gradebook_releases") == [] and world.env.db.collection_docs("gradebook_release_rows") == []
    assert world.results("s1").status_code == 403
    world.review_short_answers(4.0)
    assert world.release(acknowledge=False).status_code == 200  # complete now: no acknowledgement needed


def test_a_regrade_after_release_creates_a_new_version_with_lineage_and_students_see_it_only_after_re_release(world) -> None:
    world.grade_everyone()
    world.review_short_answers(4.0)
    assert world.release(note="", acknowledge=False).json()["version"] == 1
    assert world.results("s2").json()["row"]["raw_score"] == 0.0

    # Faculty regrade s2's Q0 (an upheld appeal): a NEW result version; the automatic one is preserved.
    regraded = JudgeService(world.env.db, None).regrade_mcq(run_id=RUN, uid="s2", question_id=Q0, reviewer="owner", correct=True, reason="key was ambiguous")
    assert regraded.version == 2 and regraded.graded_by == "owner" and regraded.regrade_reason == "key was ambiguous"
    stored = sorted(McqGradeResult.model_validate(d).version for d in world.env.db.collection_docs("judge_mcq_results") if d["uid"] == "s2" and d["question_id"] == Q0)
    assert stored == [1, 2]  # the original is still there

    assert _rows(world)["s2"]["raw_score"] == 2.0  # the live gradebook shows it at once (faculty)
    assert world.results("s2").json()["row"]["raw_score"] == 0.0  # the student still sees release v1

    missing_reason = world.release(note="  ", acknowledge=False)
    assert missing_reason.status_code == 409 and missing_reason.json()["detail"]["code"] == "release_reason_required"
    assert world.results("s2").json()["release_version"] == 1

    v2 = world.release(note="Q1 key was ambiguous; s2 regraded.", acknowledge=False)
    assert v2.status_code == 200 and v2.json()["version"] == 2
    after = world.results("s2").json()
    assert (after["row"]["raw_score"], after["release_version"]) == (2.0, 2)
    releases = world.client.get(f"{FACULTY}/runs/{RUN}/releases").json()["results"]
    assert [(r["version"], r["note"]) for r in releases] == [(1, ""), (2, "Q1 key was ambiguous; s2 regraded.")]
    assert audit_kinds(world.env.db).count("exam_results_released") == 2
    # v1's frozen rows are still stored, unchanged.
    v1_rows = [d for d in world.env.db.collection_docs("gradebook_release_rows") if d["snapshot_id"] == releases[0]["snapshot_id"] and d["uid"] == "s2"]
    assert v1_rows[0]["row"]["raw_score"] == 0.0


def test_a_defective_question_fix_reaches_students_only_with_a_new_release(world) -> None:
    world.grade_everyone()
    world.review_short_answers(4.0)
    world.release(acknowledge=False)
    world.client.post(f"{FACULTY}/runs/{RUN}/questions/{Q0}/defective", json={"resolution": "full_credit_all", "reason": "key was wrong"})
    assert world.results("s2").json()["row"]["raw_score"] == 0.0
    world.release(note="Q0 resolved as defective: full credit.", acknowledge=False)
    assert world.results("s2").json()["row"]["raw_score"] == 2.0


# -- grade appeals ------------------------------------------------------------------------------------------------------

def test_grade_appeals_are_only_possible_for_a_released_question_once_at_a_time(world) -> None:
    world.grade_everyone()
    url = f"{STUDENT}/runs/{RUN}/questions/{Q0}/appeals"
    early = world.student("s2").client.post(url, json={"student_statement": "too early"})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "grade_appeal_not_allowed"
    world.release()
    assert world.student("s2").client.post(f"{STUDENT}/runs/{RUN}/questions/nonsense/appeals", json={"student_statement": "x"}).status_code == 409
    first = world.student("s2").client.post(url, json={"student_statement": "my answer B is right"})
    assert first.status_code == 200 and first.json()["release_version"] == 1 and first.headers["cache-control"] == "no-store"
    assert world.student("s2").client.post(url, json={"student_statement": "again"}).status_code == 409
    assert world.student("s1").client.get(f"{STUDENT}/runs/{RUN}/appeals").json()["results"] == []  # not another student's


def test_only_the_runs_owner_can_resolve_a_grade_appeal(world) -> None:
    world.grade_everyone()
    world.release()
    appeal = world.student("s2").client.post(f"{STUDENT}/runs/{RUN}/questions/{Q0}/appeals", json={"student_statement": "recheck"}).json()
    resolve = f"{FACULTY}/appeals/{appeal['id']}/resolve"
    assert world.teacher("intruder").client.post(resolve, json={"uphold": True}).status_code == 403
    assert world.teacher("owner").client.post(resolve, json={"uphold": True, "resolution_notes": "stands"}).status_code == 200
    assert "exam_grade_appeal_resolved" in audit_kinds(world.env.db)


# -- role-scoped gradebook ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", [
    ("get", "/runs/{run}/rows", None), ("get", "/runs/{run}/counts", None), ("get", "/runs/{run}/releases", None),
    ("get", "/runs/{run}/defective", None), ("get", "/runs/{run}/appeals", None), ("get", "/runs/{run}/similarity", None),
    ("post", "/runs/{run}/release", {}), ("post", "/runs/{run}/export", {"format": "csv"}),
    ("post", "/runs/{run}/questions/" + Q0 + "/defective", {"resolution": "full_credit_all", "reason": "x"}),
])
def test_a_teacher_who_does_not_own_the_run_is_denied_every_gradebook_route(world, method, path, body) -> None:
    world.grade_everyone()
    url = FACULTY + path.format(run=RUN)
    intruder = getattr(world.teacher("intruder").client, method)(url, **({"json": body} if body is not None else {}))
    assert intruder.status_code == 403 and intruder.json()["detail"]["code"] == "scope_denied"
    assert getattr(world.teacher("owner").client, method)(url, **({"json": body} if body is not None else {})).status_code in (200, 409)


# -- exports --------------------------------------------------------------------------------------------------------------------------

def _export(world: World) -> dict:
    return world.teacher("owner").client.post(f"{FACULTY}/runs/{RUN}/export", json={"format": "csv"}).json()


def test_an_export_is_scoped_ticketed_single_use_and_formula_safe(world) -> None:
    world.grade_everyone()
    job = _export(world)
    assert job["status"] == "succeeded" and job["expires_at"] and job["row_count"] == 4
    assert world.teacher("intruder").client.get(f"{FACULTY}/export/{job['id']}").status_code == 403
    assert world.teacher("intruder").client.post(f"{FACULTY}/export/{job['id']}/download-ticket").status_code == 403

    ticket = world.teacher("owner").client.post(f"{FACULTY}/export/{job['id']}/download-ticket").json()["download_ticket"]
    stolen = world.teacher("someone-else").client.get(f"{FACULTY}/export/download/{ticket}")
    assert stolen.status_code == 401 and stolen.json()["detail"]["code"] == "export_ticket_invalid"
    first = world.teacher("owner").client.get(f"{FACULTY}/export/download/{ticket}")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store" and first.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in first.text and "\n=HYPERLINK" not in first.text and not first.text.startswith("=")
    assert world.teacher("owner").client.get(f"{FACULTY}/export/download/{ticket}").status_code == 401  # single use


def test_an_export_expires_and_its_content_is_deleted(world, monkeypatch) -> None:
    world.grade_everyone()
    job = _export(world)
    ticket = world.teacher("owner").client.post(f"{FACULTY}/export/{job['id']}/download-ticket").json()["download_ticket"]
    assert world.storage.exists(f"exam-exports/{RUN}/{job['id']}.csv")

    service = GradebookExportService(world.env.db, world.storage)
    assert service.sweep_expired(now=T0) == 0  # a fresh export is kept
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert service.sweep_expired(now=later) == 1
    assert not world.storage.exists(f"exam-exports/{RUN}/{job['id']}.csv")

    late = world.teacher("owner").client.get(f"{FACULTY}/export/download/{ticket}")  # a ticket issued before the sweep
    assert late.status_code == 404  # the stored content is gone
    again = world.teacher("owner").client.post(f"{FACULTY}/export/{job['id']}/download-ticket")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "export_expired"
    assert world.teacher("owner").client.get(f"{FACULTY}/export/{job['id']}").json()["expired_at"]  # the record stays
    assert _export(world)["status"] == "succeeded"  # and a fresh export can be made


def test_an_expired_but_not_yet_swept_export_is_still_refused(world) -> None:
    world.grade_everyone()
    job = _export(world)
    ticket = world.teacher("owner").client.post(f"{FACULTY}/export/{job['id']}/download-ticket").json()["download_ticket"]
    world.env.db.put("gradebook_export_jobs", job["id"], {**world.env.db.get("gradebook_export_jobs", job["id"]), "expires_at": (T0 - timedelta(days=1)).isoformat()})
    refused = world.teacher("owner").client.get(f"{FACULTY}/export/download/{ticket}")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "export_expired"
