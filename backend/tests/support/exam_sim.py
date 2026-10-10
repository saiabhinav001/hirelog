"""Exam-day simulation used to measure billable Firestore operations (free-tier
budget). Drives the real HTTP routes with N students; counts reads/writes with
the in-memory Firestore's operation counter."""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.support.exam_world import World


@dataclass
class SimResult:
    students: int
    questions: int
    phases: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def reads(self) -> int:
        return sum(p["reads"] for p in self.phases.values())

    @property
    def writes(self) -> int:
        return sum(p["writes"] for p in self.phases.values())

    def report(self) -> str:
        lines = [f"{self.students} students x {self.questions} questions"]
        for name, p in self.phases.items():
            lines.append(f"  {name:<14} reads={p['reads']:>6} writes={p['writes']:>6}")
        lines.append(f"  {'TOTAL':<14} reads={self.reads:>6} writes={self.writes:>6}")
        return "\n".join(lines)


def build_exam(world: World, *, students: int, questions: int) -> dict:
    """College with one faculty, `students` students, and a released run whose
    every slot has an approved single-choice variant reserved per student."""
    inst = world.create_institution("Sim College")
    rows = [{"identity_key": f"R{i:03d}", "contact_email": f"r{i}@sim.edu", "role_scope": "student"} for i in range(students)]
    rows.append({"identity_key": "FAC", "contact_email": "fac@sim.edu", "role_scope": "faculty"})
    world.import_roster(inst, rows)
    world.link_faculty_uid(inst, "FAC", "exam-sim-faculty")
    faculty = world.session_for(inst, "FAC")
    client = world.client

    definition = world.create_definition(faculty, "Sim exam")
    # An approved reference source (the real flow fetches a URL; fetching is not under test here).
    world.db.seed("kt_approved_sources/src-1", {
        "id": "src-1", "kind": "url", "institution_id": inst, "url": "https://example.edu/notes",
        "review_status": "approved", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    })
    slots = [{
        "slot_index": i, "item_type": "single_choice", "objective_ids": ["core"], "difficulty": "medium", "marks": 1,
        "target_minutes": 2, "permitted_languages": [], "requested_variant_count": 2,
    } for i in range(questions)]
    blueprint = client.post("/api/exams/v1/faculty/blueprints", headers=faculty,
                            json={"exam_definition_id": definition["id"], "title": "bp", "slots": slots})
    assert blueprint.status_code == 200, blueprint.text
    blueprint_id = blueprint.json()["id"]
    for i in range(questions):
        generated = client.post("/api/exams/v1/faculty/generation-requests", headers=faculty, json={
            "blueprint_id": blueprint_id, "slot_index": i, "deterministic_seed": i,
            "source_anchors": [{"source_id": "src-1", "anchor_id": "full-document"}]})
        assert generated.status_code == 200, generated.text
        for candidate_id in generated.json()["candidate_ids"]:
            reviewed = client.post(f"/api/exams/v1/faculty/candidates/{candidate_id}/review", headers=faculty,
                                   json={"decision": "approve"})
            assert reviewed.status_code == 200, reviewed.text

    revision = world.publish_revision(faculty, definition["id"], slot_count=questions)
    run_id = world.released_run(faculty, definition["id"], revision["id"], enroll_all_institution_students=True)
    uids = [world.member(inst, f"R{i:03d}").uid for i in range(students)]
    for i in range(questions):
        reserved = client.post(f"/api/exams/v1/faculty/blueprints/{blueprint_id}/slots/{i}/reserve-variants",
                               headers=faculty, json={"run_id": run_id, "uids": uids, "allow_balanced_reuse": True})
        assert reserved.status_code == 200, reserved.text
    return {"inst": inst, "faculty": faculty, "run_id": run_id, "revision_id": revision["id"], "uids": uids}


def _measure(world: World, result: SimResult, phase: str, fn) -> None:
    before = world.db.counter.snapshot()
    fn()
    after = world.db.counter.snapshot()
    bucket = result.phases.setdefault(phase, {"reads": 0, "writes": 0})
    bucket["reads"] += after["reads"] - before["reads"]
    bucket["writes"] += after["writes"] - before["writes"]


def run_exam(
    world: World, exam: dict, *, students: int, questions: int, saves_per_question: int = 3,
    observations: int = 4, faculty_polls: int = 30, finish_via_drain: bool = True,
) -> SimResult:
    from app.domain.exams.clock import system_clock
    from app.domain.exams.outbox import CompositeOutboxHandler, drain_outbox_once
    from app.domain.exams.repository import OutboxRepository
    from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler
    from app.domain.judge.provider import DeterministicFakeJudgeProvider
    from app.domain.judge.runtime_allowlist import default_allowlist

    client, run_id = world.client, exam["run_id"]
    result = SimResult(students=students, questions=questions)
    sessions = [world.session_for(exam["inst"], f"R{i:03d}") for i in range(students)]

    def admit_all():
        for headers in sessions:
            assert client.post(f"/api/exams/v1/student/runs/{run_id}/admit", headers=headers).status_code == 200

    _measure(world, result, "admit", admit_all)

    versions: list[dict[str, int]] = [{} for _ in range(students)]
    epoch = 1

    def work_through_questions():
        for q in range(questions):
            for s, headers in enumerate(sessions):
                question = client.get(f"/api/exams/v1/student/runs/{run_id}/question", headers=headers)
                assert question.status_code == 200, question.text
                qid = question.json()["question_id"]
                for k in range(saves_per_question):
                    version = versions[s].get(qid, 0)
                    saved = client.patch(f"/api/exams/v1/student/runs/{run_id}/drafts/{qid}", headers=headers,
                                         json={"expected_version": version, "answer": {"selected": [k % 2]}})
                    assert saved.status_code == 200, saved.text
                    versions[s][qid] = saved.json()["version"]
                if q < questions - 1:
                    nxt = client.post(f"/api/exams/v1/student/runs/{run_id}/next-question", headers=headers)
                    assert nxt.status_code == 200, nxt.text

    _measure(world, result, "answering", work_through_questions)

    def proctoring():
        kinds = ["page_hidden", "page_visible"]
        for s, headers in enumerate(sessions):
            for n in range(observations):
                posted = client.post(f"/api/exams/v1/student/proctoring/runs/{run_id}/observations", headers=headers, json={
                    "kind": kinds[n % 2], "client_event_id": f"{s}-{n}", "client_timestamp": "2026-10-10T10:00:00Z"})
                assert posted.status_code == 200, posted.text

    _measure(world, result, "proctoring", proctoring)

    def faculty_monitoring():
        for _ in range(faculty_polls):
            assert client.get(f"/api/exams/v1/faculty/runs/{run_id}/attempts?limit=200", headers=exam["faculty"]).status_code == 200
            assert client.get(f"/api/exams/v1/faculty/runs/{run_id}", headers=exam["faculty"]).status_code == 200

    _measure(world, result, "faculty-monitor", faculty_monitoring)

    def submit_all():
        for s, headers in enumerate(sessions):
            done = client.post(f"/api/exams/v1/student/runs/{run_id}/submit", headers=headers, json={
                "idempotency_key": f"submit-{s}", "client_epoch": epoch, "expected_draft_versions": versions[s]})
            assert done.status_code == 200, done.text

    _measure(world, result, "submit", submit_all)

    def grading():
        repo = OutboxRepository(world.db)
        handler = CompositeOutboxHandler([GradingTriggerOutboxHandler(world.db, DeterministicFakeJudgeProvider(), default_allowlist())])
        for _ in range(students // 20 + 3):
            drain_outbox_once(repo, handler, now=system_clock.now(), limit=25, lease_seconds=60, max_attempts=5)

    if finish_via_drain:
        _measure(world, result, "grading", grading)
    return result
