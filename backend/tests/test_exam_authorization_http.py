"""HTTP-level authorization regression tests (adversarial review §1 and the
follow-up faculty/roster/live findings). Real routes + repositories run against
an in-memory Firestore; only token verification is faked."""

from __future__ import annotations

import pytest

from app.domain.institution.models import MembershipStatus
from tests.support.exam_scenarios import revoke as _revoke
from tests.support.exam_scenarios import save_draft as _save_draft

STUDENT_ROUTES_GET = ("state", "drafts", "accommodations", "receipt")


# ── adversarial review §1: revoked / stale sessions ───────────────────────────

def test_active_student_can_read_and_save_before_revocation(exam) -> None:
    client = exam.world.client
    assert client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=exam.student).status_code == 200
    assert _save_draft(exam, exam.student, 0).status_code == 200


@pytest.mark.parametrize("suffix", STUDENT_ROUTES_GET)
def test_revoked_student_session_is_rejected_on_every_student_read(exam, suffix) -> None:
    _revoke(exam)
    response = exam.world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/{suffix}", headers=exam.student)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "membership_not_active"


def test_revoked_student_cannot_modify_draft_or_submit(exam) -> None:
    assert _save_draft(exam, exam.student, 0).status_code == 200
    _revoke(exam)

    assert _save_draft(exam, exam.student, 1, "B").status_code == 403
    submit = exam.world.client.post(
        f"/api/exams/v1/student/runs/{exam.run_id}/submit", headers=exam.student,
        json={"idempotency_key": "k1", "client_epoch": 1, "expected_draft_versions": {exam.question_id: 1}},
    )
    assert submit.status_code == 403

    uid = exam.world.member(exam.inst, "S1").uid
    draft = exam.world.db.raw(f"exam_runs/{exam.run_id}/attempts/{uid}/drafts/{exam.question_id}")
    attempt = exam.world.db.raw(f"exam_runs/{exam.run_id}/attempts/{uid}")
    assert draft["version"] == 1 and draft["answer"] == {"choice": "A"}  # the post-revocation write never landed
    assert attempt["status"] == "in_progress" and attempt.get("submit_receipt") is None


def test_revoked_student_cannot_get_a_question_or_open_live_ticket(exam) -> None:
    _revoke(exam)
    client = exam.world.client
    assert client.get(f"/api/exams/v1/student/runs/{exam.run_id}/question", headers=exam.student).status_code == 403
    ticket = client.post("/api/exams/v1/live/ticket", json={"run_id": exam.run_id}, headers=exam.student)
    assert ticket.status_code == 403


def test_revoked_student_no_longer_sees_the_run_in_their_list(exam) -> None:
    _revoke(exam)
    legacy_style = exam.world.tokens.issue(exam.world.member(exam.inst, "S1").uid, provider="google.com")
    response = exam.world.client.get("/api/exams/v1/student/runs", headers=exam.world.tokens.headers(legacy_style))
    assert response.status_code == 200 and response.json()["results"] == []


def test_stale_authorization_version_forces_reauthentication(exam) -> None:
    member = exam.world.member(exam.inst, "S1")
    # membership moved on (e.g. role change) after the token was minted
    from app.domain.institution.repository import MembershipRepository

    MembershipRepository(exam.world.db).upsert(member.model_copy(update={"authorization_version": member.authorization_version + 1}))
    response = exam.world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=exam.student)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "stale_authorization"


def test_revocation_applies_to_a_legacy_linked_account_without_institution_claim(exam) -> None:
    """A Google/legacy account linked to a membership carries no institution
    claim — the run's own institution must still gate it."""
    uid = exam.world.member(exam.inst, "S1").uid
    legacy = exam.world.tokens.headers(exam.world.tokens.issue(uid, provider="google.com", email="s1@a.edu"))
    assert exam.world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=legacy).status_code == 200
    _revoke(exam)
    assert exam.world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=legacy).status_code == 403


def test_revoked_faculty_session_loses_faculty_access(exam) -> None:
    _revoke(exam, "F1")
    response = exam.world.client.get(f"/api/exams/v1/faculty/runs/{exam.run_id}", headers=exam.faculty)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "membership_not_active"


def test_a_student_membership_alone_never_grants_faculty_routes(exam) -> None:
    response = exam.world.client.get(f"/api/exams/v1/faculty/runs/{exam.run_id}", headers=exam.student)
    assert response.status_code == 403


# ── enrolment / cross-institution student access ──────────────────────────────

def test_student_not_enrolled_in_the_run_is_forbidden(exam) -> None:
    world = exam.world
    world.import_roster(exam.inst, [{"identity_key": "S3", "contact_email": "s3@a.edu", "role_scope": "student"}])
    outsider = world.session_for(exam.inst, "S3")
    assert world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=outsider).status_code == 403


def test_member_of_another_institution_cannot_use_this_institutions_run(exam) -> None:
    world = exam.world
    other = world.create_institution("College B")
    world.import_roster(other, [{"identity_key": "S1", "contact_email": "s1@b.edu", "role_scope": "student"}])
    outsider = world.session_for(other, "S1")
    assert world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=outsider).status_code == 403
    assert world.client.get("/api/exams/v1/student/runs", headers=outsider).json()["results"] == []


# ── faculty scoping ───────────────────────────────────────────────────────────

@pytest.fixture
def other_college(exam):
    world = exam.world
    inst_b = world.create_institution("College B")
    world.import_roster(inst_b, [{"identity_key": "F9", "contact_email": "f9@b.edu", "role_scope": "faculty"}])
    world.link_faculty_uid(inst_b, "F9", "exam-faculty-b")
    return type("Other", (), dict(inst=inst_b, faculty=world.session_for(inst_b, "F9")))


def test_faculty_of_another_institution_cannot_read_or_control_the_run(exam, other_college) -> None:
    client, run = exam.world.client, exam.run_id
    assert client.get(f"/api/exams/v1/faculty/runs/{run}", headers=other_college.faculty).status_code == 404
    pause = client.post(
        f"/api/exams/v1/faculty/runs/{run}/commands/pause", headers=other_college.faculty,
        json={"expected_version": 3, "idempotency_key": "x"},
    )
    assert pause.status_code == 404
    assert client.get(f"/api/exams/v1/faculty/runs/{run}/attempts", headers=other_college.faculty).status_code == 404
    assert client.get(f"/api/exams/v1/faculty/lockdown/runs/{run}/profile", headers=other_college.faculty).status_code == 404
    assert client.get(f"/api/exams/v1/faculty/gradebook/runs/{run}/rows", headers=other_college.faculty).status_code == 404
    assert client.get(
        f"/api/exams/v1/faculty/proctoring/runs/{run}/uids/x/incidents", headers=other_college.faculty
    ).status_code == 404
    # ...and the run really was not paused
    assert exam.world.db.raw(f"exam_runs/{run}")["status"] == "released"


def test_faculty_of_another_institution_cannot_publish_or_schedule_on_a_foreign_definition(exam, other_college) -> None:
    client = exam.world.client
    published = client.post(
        f"/api/exams/v1/faculty/definitions/{exam.definition['id']}/publish", json={"slot_count": 1}, headers=other_college.faculty
    )
    assert published.status_code == 404
    scheduled = client.post(
        "/api/exams/v1/faculty/runs", headers=other_college.faculty,
        json={"definition_id": exam.definition["id"], "revision_id": exam.revision["id"], "policy": {"duration_minutes": 30}},
    )
    assert scheduled.status_code == 404
    blueprint = client.post(
        "/api/exams/v1/faculty/blueprints", headers=other_college.faculty,
        json={"exam_definition_id": exam.definition["id"], "title": "t", "slots": [{
            "slot_index": 0, "item_type": "single_choice", "objective_ids": ["o"], "difficulty": "easy",
            "marks": 1, "target_minutes": 1, "requested_variant_count": 1}]},
    )
    assert blueprint.status_code == 404


def test_faculty_cannot_live_ticket_for_another_institutions_run(exam, other_college) -> None:
    response = exam.world.client.post("/api/exams/v1/live/ticket", json={"run_id": exam.run_id}, headers=other_college.faculty)
    assert response.status_code == 403


def test_run_and_definition_are_stamped_with_the_institution(exam) -> None:
    assert exam.definition["institution_id"] == exam.inst
    assert exam.world.db.raw(f"exam_runs/{exam.run_id}")["institution_id"] == exam.inst


def test_enrolment_rejects_uids_that_are_not_active_students_of_the_institution(exam) -> None:
    faculty_uid = exam.world.member(exam.inst, "F1").uid
    response = exam.world.client.post(
        "/api/exams/v1/faculty/runs", headers=exam.faculty,
        json={"definition_id": exam.definition["id"], "revision_id": exam.revision["id"],
              "policy": {"duration_minutes": 30}, "enrolled_uids": [faculty_uid, "someone-else"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "uids_not_active_students"


def test_enroll_all_institution_students_skips_revoked_members(exam) -> None:
    _revoke(exam, "S2")
    run = exam.world.create_run(
        exam.faculty, exam.definition["id"], exam.revision["id"], enroll_all_institution_students=True
    )
    assert run["enrolled_uids"] == [exam.world.member(exam.inst, "S1").uid]


# ── verified-email faculty authority ──────────────────────────────────────────

def test_allow_listed_email_must_be_verified(exam_world) -> None:
    from app.api.exams_dependencies import is_bootstrap_faculty  # noqa: F401 - import proves the helper exists

    tokens = exam_world.tokens
    unverified = tokens.headers(tokens.issue("attacker", email="ops@platform.example", email_verified=False, provider="password"))
    response = exam_world.client.post("/api/exams/v1/faculty/definitions", json={"title": "x"}, headers=unverified)
    assert response.status_code == 403


def test_a_client_written_profile_email_grants_nothing(exam_world) -> None:
    """The legacy `users/{uid}.email` is client-writable under the deployed
    Firestore rules; the exam API must never consult it."""
    exam_world.db.seed("users/attacker", {"uid": "attacker", "email": "ops@platform.example", "role": "viewer"})
    token = exam_world.tokens.issue("attacker", email="attacker@evil.example", email_verified=True)
    response = exam_world.client.post(
        "/api/exams/v1/faculty/definitions", json={"title": "x"}, headers=exam_world.tokens.headers(token)
    )
    assert response.status_code == 403
    assert "users" not in exam_world.db.counter.by_collection  # the legacy collection is never read or written


# ── roster / institution administration ───────────────────────────────────────

def test_only_platform_operators_create_institutions(exam) -> None:
    response = exam.world.client.post("/api/exams/v1/faculty/institutions", json={"name": "Rogue"}, headers=exam.admin)
    assert response.status_code == 403


def test_institution_list_only_shows_what_the_caller_may_act_in(exam, other_college) -> None:
    client = exam.world.client
    url = "/api/exams/v1/faculty/institutions"
    own = client.get(url, headers=exam.faculty).json()
    assert [item["id"] for item in own["results"]] == [exam.inst]
    assert own["is_platform_operator"] is False and own["results"][0]["can_administer"] is False
    assert client.get(url, headers=exam.admin).json()["results"][0]["can_administer"] is True
    everything = client.get(url, headers=exam.world.operator()).json()
    assert everything["is_platform_operator"] is True
    assert {exam.inst, other_college.inst} <= {item["id"] for item in everything["results"]}
    assert client.get(url, headers=exam.student).status_code == 403


def test_faculty_importer_cannot_overwrite_an_existing_members_contact(exam) -> None:
    """The account-takeover path: re-pointing a member's contact email makes the
    attacker's inbox the OTP destination for that member's uid."""
    world = exam.world
    before = world.member(exam.inst, "S1")
    rows = [{"identity_key": "S1", "contact_email": "attacker@evil.example", "role_scope": "student"}]
    base = f"/api/exams/v1/faculty/institutions/{exam.inst}/roster"
    dry = world.client.post(f"{base}/dry-run", json={"rows": rows}, headers=exam.faculty).json()
    assert dry["row_results"][0]["ok"] is False
    assert dry["row_results"][0]["error"] == "existing_membership_requires_admin"
    world.client.post(f"{base}/{dry['id']}/apply", json={"rows": rows}, headers=exam.faculty)
    assert world.member(exam.inst, "S1").contact_email == before.contact_email


def test_faculty_importer_can_still_add_new_students_but_not_elevated_roles(exam) -> None:
    world = exam.world
    rows = [
        {"identity_key": "S7", "contact_email": "s7@a.edu", "role_scope": "student"},
        {"identity_key": "F7", "contact_email": "f7@a.edu", "role_scope": "faculty"},
    ]
    job = world.import_roster(exam.inst, rows, headers=exam.faculty)
    outcomes = {r["identity_key"]: r for r in job["row_results"]}
    assert outcomes["S7"]["ok"] is True
    assert outcomes["F7"]["ok"] is False and "not_authorized_to_grant" in outcomes["F7"]["error"]
    assert world.member(exam.inst, "S7").uid.startswith("exam-")  # pre-provisioned so it can be enrolled before first login


def test_admin_contact_change_bumps_authorization_version_and_invalidates_sessions(exam) -> None:
    world = exam.world
    before = world.member(exam.inst, "S1")
    world.import_roster(
        exam.inst, [{"identity_key": "S1", "contact_email": "new@a.edu", "role_scope": "student"}], headers=exam.admin
    )
    after = world.member(exam.inst, "S1")
    assert after.contact_email == "new@a.edu" and after.authorization_version == before.authorization_version + 1
    assert after.uid == before.uid
    stale = world.client.get(f"/api/exams/v1/student/runs/{exam.run_id}/state", headers=exam.student)
    assert stale.status_code == 401


def test_roster_of_another_institution_is_not_reachable(exam, other_college) -> None:
    rows = [{"identity_key": "X1", "contact_email": "x1@evil.example", "role_scope": "student"}]
    base = f"/api/exams/v1/faculty/institutions/{exam.inst}/roster"
    assert exam.world.client.post(f"{base}/dry-run", json={"rows": rows}, headers=other_college.faculty).status_code == 404
    assert exam.world.client.get(
        f"/api/exams/v1/faculty/institutions/{exam.inst}/members", headers=other_college.faculty
    ).status_code == 404


def test_roster_job_cannot_be_applied_to_a_different_institution(exam, other_college) -> None:
    world = exam.world
    rows = [{"identity_key": "S9", "contact_email": "s9@a.edu", "role_scope": "student"}]
    dry = world.client.post(
        f"/api/exams/v1/faculty/institutions/{exam.inst}/roster/dry-run", json={"rows": rows}, headers=exam.admin
    ).json()
    cross = world.client.post(
        f"/api/exams/v1/faculty/institutions/{other_college.inst}/roster/{dry['id']}/apply",
        json={"rows": rows}, headers=world.operator(),
    )
    assert cross.status_code == 404


def test_only_admins_revoke_and_not_themselves(exam) -> None:
    client = exam.world.client
    base = f"/api/exams/v1/faculty/institutions/{exam.inst}/memberships/revoke"
    assert client.post(base, json={"identity_key": "S1"}, headers=exam.faculty).status_code == 403
    assert client.post(base, json={"identity_key": "AD"}, headers=exam.admin).status_code == 409
    assert exam.world.member(exam.inst, "AD").status == MembershipStatus.ACTIVE


def test_member_list_hides_contact_email_from_non_admin_faculty(exam) -> None:
    url = f"/api/exams/v1/faculty/institutions/{exam.inst}/members"
    faculty_view = exam.world.client.get(url, headers=exam.faculty).json()["results"]
    admin_view = exam.world.client.get(url, headers=exam.admin).json()["results"]
    assert all("contact_email" not in row for row in faculty_view)
    assert all("contact_email" in row for row in admin_view)


# ── draft input hardening ─────────────────────────────────────────────────────

def test_draft_rejects_oversized_answers_and_malformed_question_ids(exam) -> None:
    client = exam.world.client
    big = client.patch(
        f"/api/exams/v1/student/runs/{exam.run_id}/drafts/{exam.question_id}", headers=exam.student,
        json={"expected_version": 0, "answer": {"text": "x" * 400_000}},
    )
    assert big.status_code == 422
    bad_id = client.patch(
        f"/api/exams/v1/student/runs/{exam.run_id}/drafts/not-a-question", headers=exam.student,
        json={"expected_version": 0, "answer": {}},
    )
    assert bad_id.status_code == 422
