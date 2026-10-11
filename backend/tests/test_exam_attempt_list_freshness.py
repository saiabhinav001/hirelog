"""The faculty candidate grid is cached to spare the free-tier read quota, but it must
never keep hiding a change the faculty member is waiting to see."""

from __future__ import annotations

from app.api import exams_scope
from app.domain.exams.student_service import ExamStudentService


def _grid(exam, run_id: str, **params) -> list[dict]:
    response = exam.world.client.get(
        f"/api/exams/v1/faculty/runs/{run_id}/attempts", params=params, headers=exam.faculty
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


def _fresh_run(exam) -> str:
    return exam.world.released_run(
        exam.faculty, exam.definition["id"], exam.revision["id"], enrolled_identity_keys=["S2"]
    )


def test_an_admission_shows_up_in_the_grid_immediately(exam) -> None:
    run_id = _fresh_run(exam)
    assert _grid(exam, run_id) == []  # now cached as empty

    admitted = exam.world.client.post(
        f"/api/exams/v1/student/runs/{run_id}/admit", headers=exam.world.session_for(exam.inst, "S2")
    )
    assert admitted.status_code == 200, admitted.text

    assert len(_grid(exam, run_id)) == 1


def test_the_grid_names_students_by_college_id(exam) -> None:
    run_id = _fresh_run(exam)
    exam.world.client.post(
        f"/api/exams/v1/student/runs/{run_id}/admit", headers=exam.world.session_for(exam.inst, "S2")
    )
    assert [row["college_id"] for row in _grid(exam, run_id)] == ["S2"]


def test_a_change_in_one_run_does_not_drop_another_runs_cached_grid(exam) -> None:
    quiet_run = _fresh_run(exam)
    assert _grid(exam, quiet_run) == []
    assert (quiet_run, 100, None) in exams_scope.attempt_pages._data

    other_run = _fresh_run(exam)
    admitted = exam.world.client.post(
        f"/api/exams/v1/student/runs/{other_run}/admit", headers=exam.world.session_for(exam.inst, "S2")
    )
    assert admitted.status_code == 200

    assert (quiet_run, 100, None) in exams_scope.attempt_pages._data


def test_a_forced_refresh_is_honoured_only_for_a_page_that_is_old_enough(exam) -> None:
    run_id = _fresh_run(exam)
    key = (run_id, 100, None)
    assert _grid(exam, run_id) == []

    # An admission that never went through the live publisher (e.g. made by another process).
    ExamStudentService(exam.world.db).admit(run_id, exam.world.member(exam.inst, "S2").uid)
    assert _grid(exam, run_id) == []  # still the cached page
    assert _grid(exam, run_id, fresh=True) == []  # too young: a refresh button cannot hammer the database

    loaded_at, value = exams_scope.attempt_pages._data[key]
    exams_scope.attempt_pages._data[key] = (loaded_at - exams_scope.ATTEMPT_PAGE_MIN_REFRESH_SECONDS - 1, value)
    assert len(_grid(exam, run_id, fresh=True)) == 1
