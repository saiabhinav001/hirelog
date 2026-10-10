"""Free-tier (Spark) capacity check: one 90-student exam must fit comfortably
inside the 50,000 reads / 20,000 writes per DAY that the shared Firebase project
gets — leaving room for the production app on the same quota.

The per-(student x question) ceilings below are the regression guard: anything
that makes a hot path read or write more fails here before it reaches a deploy.
"""

from __future__ import annotations

import os

import pytest

from tests.support.exam_sim import build_exam, run_exam

SPARK_READS_PER_DAY = 50_000
SPARK_WRITES_PER_DAY = 20_000

# Measured for the 20 students x 5 questions config below: 14.7 reads / 7.4 writes
# per student-question (3 draft saves, 4 proctoring observations, 30 faculty
# refreshes, submit and grading amortised over 5 questions; the 90 x 10 run is
# ~14.3 / 6.2). Ceilings carry ~15% headroom.
MAX_READS_PER_STUDENT_QUESTION = 17.0
MAX_WRITES_PER_STUDENT_QUESTION = 8.5


def _simulate(world, *, students: int, questions: int, **kwargs):
    exam = build_exam(world, students=students, questions=questions)
    world.db.counter.reset()
    return run_exam(world, exam, students=students, questions=questions, **kwargs)


def test_per_student_question_cost_stays_inside_the_ceiling(exam_world, capsys) -> None:
    students, questions = 20, 5
    result = _simulate(exam_world, students=students, questions=questions)
    cells = students * questions
    with capsys.disabled():
        print("\n" + result.report())
        print(f"  per student-question: {result.reads / cells:.1f} reads, {result.writes / cells:.1f} writes")
    assert result.reads / cells <= MAX_READS_PER_STUDENT_QUESTION
    assert result.writes / cells <= MAX_WRITES_PER_STUDENT_QUESTION


def test_faculty_monitoring_costs_almost_nothing_after_the_first_refresh(exam_world) -> None:
    """30 dashboard refreshes of a 25-student cohort: one real query, the rest from cache."""
    students = 25
    exam = build_exam(exam_world, students=students, questions=2)
    run_exam(exam_world, exam, students=students, questions=2, faculty_polls=1)
    exam_world.db.counter.reset()
    for _ in range(30):
        exam_world.client.get(f"/api/exams/v1/faculty/runs/{exam['run_id']}/attempts?limit=200", headers=exam["faculty"])
    assert exam_world.db.counter.reads <= students + 3


@pytest.mark.skipif(not os.environ.get("EXAM_BUDGET_FULL"), reason="set EXAM_BUDGET_FULL=1 for the 90-student run")
def test_ninety_students_fit_inside_a_third_of_the_daily_spark_quota(exam_world, capsys) -> None:
    result = _simulate(exam_world, students=90, questions=10)
    with capsys.disabled():
        print("\n" + result.report())
    assert result.reads <= SPARK_READS_PER_DAY * 0.33
    assert result.writes <= SPARK_WRITES_PER_DAY * 0.33
