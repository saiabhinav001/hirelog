"""Shared scenario for the HTTP-level exam tests."""

from __future__ import annotations

from types import SimpleNamespace

from tests.support.exam_world import World


def seed_roster(world: World, institution_id: str) -> None:
    world.import_roster(institution_id, [
        {"identity_key": "S1", "contact_email": "s1@a.edu", "role_scope": "student"},
        {"identity_key": "S2", "contact_email": "s2@a.edu", "role_scope": "student"},
        {"identity_key": "F1", "contact_email": "f1@a.edu", "role_scope": "faculty"},
        {"identity_key": "AD", "contact_email": "ad@a.edu", "role_scope": "admin"},
    ])
    world.link_faculty_uid(institution_id, "F1", "exam-faculty-1")
    world.link_faculty_uid(institution_id, "AD", "exam-admin-1")


def seed_exam_scenario(world: World) -> SimpleNamespace:
    """College A with a faculty member, an admin, two students and one released
    run in which student S1 has already been admitted."""
    inst = world.create_institution("College A")
    seed_roster(world, inst)
    faculty = world.session_for(inst, "F1")
    definition = world.create_definition(faculty)
    revision = world.publish_revision(faculty, definition["id"])
    run_id = world.released_run(faculty, definition["id"], revision["id"], enrolled_identity_keys=["S1", "S2"])
    student = world.session_for(inst, "S1")
    admitted = world.client.post(f"/api/exams/v1/student/runs/{run_id}/admit", headers=student)
    assert admitted.status_code == 200, admitted.text
    return SimpleNamespace(
        world=world, inst=inst, faculty=faculty, admin=world.session_for(inst, "AD"), student=student,
        definition=definition, revision=revision, run_id=run_id, question_id=f"{revision['id']}:slot:0",
    )


def revoke(exam: SimpleNamespace, identity_key: str = "S1") -> None:
    response = exam.world.client.post(
        f"/api/exams/v1/faculty/institutions/{exam.inst}/memberships/revoke",
        json={"identity_key": identity_key}, headers=exam.admin,
    )
    assert response.status_code == 200, response.text


def save_draft(exam: SimpleNamespace, headers: dict, version: int, answer: str = "A"):
    return exam.world.client.patch(
        f"/api/exams/v1/student/runs/{exam.run_id}/drafts/{exam.question_id}",
        json={"expected_version": version, "answer": {"choice": answer}}, headers=headers,
    )
