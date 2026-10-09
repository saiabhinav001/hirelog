"""Roster service tests for Prompt 2 (docs/adr/0018) with in-memory stand-ins:
digest-bound resumable apply, replay, no implicit delete/reactivation, role and
contact-email authority, atomic audit, and teacher assignment.

`firestore.transactional` is patched to a passthrough, so these prove the LOGIC
(re-check against stored state, audit with the change), not true multi-writer
transaction semantics — that needs an emulator/staging run (see ADR 0018).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.domain.institution import roster_service as roster_service_module
from app.domain.institution.errors import RosterRowRejectedError, RosterRowsChangedError
from app.domain.institution.models import (
    IdentityKeyPolicy,
    Institution,
    Membership,
    MembershipStatus,
    RoleScope,
    RosterImportJob,
    RosterImportStatus,
    RosterRowInput,
)
from app.domain.institution.roster_csv import RosterParseError
from app.domain.institution.roster_import import rows_digest
from app.domain.institution.roster_service import RosterService

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
INST = "i1"


class _FakeMemberships:
    def __init__(self, members: dict[str, Membership] | None = None) -> None:
        self.members = dict(members or {})
        self.created: list[str] = []
        self.updates: list[tuple[str, dict]] = []
        self.create_race_keys: set[str] = set()
        self.txn_view: dict[str, Membership] = {}  # what a transactional read sees (to simulate races)

    def get_many(self, _institution_id, keys):
        return {k: self.members[k] for k in dict.fromkeys(keys) if k in self.members}

    def create_if_absent(self, membership) -> bool:
        if membership.identity_key in self.create_race_keys or membership.identity_key in self.members:
            return False
        self.members[membership.identity_key] = membership
        self.created.append(membership.identity_key)
        return True

    def get_in_transaction(self, _txn, _institution_id, key):
        return self.txn_view.get(key, self.members.get(key))

    def create_in_transaction(self, _txn, membership) -> None:
        self.members[membership.identity_key] = membership
        self.created.append(membership.identity_key)

    def update_in_transaction(self, _txn, _institution_id, key, update: dict) -> None:
        self.members[key] = Membership.model_validate({**self.members[key].model_dump(), **update})
        self.updates.append((key, update))


class _FakeJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, RosterImportJob] = {}

    def create(self, job) -> None:
        self.jobs[job.id] = job.model_copy(deep=True)

    def get(self, job_id):
        job = self.jobs.get(job_id)
        return job.model_copy(deep=True) if job else None

    def update(self, job_id, update: dict) -> None:
        self.jobs[job_id] = RosterImportJob.model_validate({**self.jobs[job_id].model_dump(), **update})


class _FakeInstitutions:
    def __init__(self, policy: IdentityKeyPolicy | None) -> None:
        self._policy = policy

    def get(self, institution_id):
        return Institution(id=institution_id, name="College", created_at=NOW, updated_at=NOW, identity_policy=self._policy)


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list = []

    def append(self, event) -> None:
        self.events.append(event)

    def append_in_transaction(self, _txn, event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class _FakeDb:
    def transaction(self):
        return object()


def _service(monkeypatch, members=None, policy=None) -> RosterService:
    svc = RosterService.__new__(RosterService)
    svc._db = _FakeDb()
    svc._memberships = _FakeMemberships(members)
    svc._institutions = _FakeInstitutions(policy)
    svc._jobs = _FakeJobs()
    svc._audit = _FakeAudit()
    monkeypatch.setattr(roster_service_module.firestore, "transactional", lambda fn: fn)
    return svc


def _row(key: str, email: str | None = None, role: str = "student") -> RosterRowInput:
    return RosterRowInput(identity_key=key, contact_email=email or f"s{key}@college.edu".lower(), role_scope=role)


def _member(key: str, *, email: str | None = None, role: RoleScope = RoleScope.STUDENT, version: int = 1,
            status: MembershipStatus = MembershipStatus.ACTIVE, uid: str | None = None) -> Membership:
    return Membership(institution_id=INST, identity_key=key, contact_email=email or f"s{key}@college.edu".lower(),
                      role_scope=role, status=status, uid=uid, authorization_version=version, created_at=NOW, updated_at=NOW)


def _import(svc, rows, *, max_scope=RoleScope.ADMIN, can_change_email=False, batch_size=200):
    job = svc.dry_run(institution_id=INST, rows=rows, requester_max_role_scope=max_scope, uploaded_by="uploader",
                      can_change_contact_email=can_change_email)
    return svc.apply_batch(job.id, rows=rows, requester_max_role_scope=max_scope, batch_size=batch_size,
                           can_change_contact_email=can_change_email, actor_uid="uploader")


# ── dry run ──────────────────────────────────────────────────────────────────

def test_dry_run_persists_a_digest_and_row_results_but_writes_no_membership(monkeypatch) -> None:
    svc = _service(monkeypatch)
    rows = [_row("001"), _row("002"), _row("001", "dup@college.edu")]
    job = svc.dry_run(institution_id=INST, rows=rows, requester_max_role_scope=RoleScope.ADMIN, uploaded_by="u")

    assert job.status == RosterImportStatus.DRY_RUN and job.total_rows == 3 and job.applied_cursor == 0
    assert job.rows_digest == rows_digest(rows)
    assert [r.ok for r in job.row_results] == [True, True, False]
    assert job.row_results[2].error == "duplicate_identity_key_in_file"
    assert svc._memberships.members == {} and svc._audit.events == []


def test_csv_parse_errors_flow_through_as_row_errors_and_do_not_block_the_valid_rows(monkeypatch) -> None:
    svc = _service(monkeypatch)
    rows = [_row("001"), RosterParseError(3, "002", "csv_row_has_1_fields_expected_2"), _row("003")]
    done = _import(svc, rows)
    assert sorted(svc._memberships.members) == ["001", "003"]
    assert [(r.ok, r.line_number) for r in done.row_results] == [(True, None), (False, 3), (True, None)]


# ── apply: bounded, resumable, replay-safe ───────────────────────────────────

def test_apply_creates_students_atomically_and_audits_one_batch_event_not_one_per_row(monkeypatch) -> None:
    svc = _service(monkeypatch)
    done = _import(svc, [_row("001"), _row("002"), _row("003")])
    assert done.status == RosterImportStatus.COMPLETED and done.applied_cursor == 3
    assert svc._memberships.created == ["001", "002", "003"]
    assert svc._audit.kinds() == ["roster_batch_applied"]


def test_apply_is_bounded_and_resumes_from_the_persisted_cursor(monkeypatch) -> None:
    svc = _service(monkeypatch)
    rows = [_row(f"{i:03d}") for i in range(10)]
    job = svc.dry_run(institution_id=INST, rows=rows, requester_max_role_scope=RoleScope.ADMIN, uploaded_by="u")
    seen = []
    for _ in range(3):
        step = svc.apply_batch(job.id, rows=rows, requester_max_role_scope=RoleScope.ADMIN, batch_size=4)
        seen.append((step.status, step.applied_cursor, len(svc._memberships.members)))
    assert seen == [
        (RosterImportStatus.APPLYING, 4, 4),
        (RosterImportStatus.APPLYING, 8, 8),
        (RosterImportStatus.COMPLETED, 10, 10),
    ]


def test_applying_a_completed_job_again_writes_nothing(monkeypatch) -> None:
    svc = _service(monkeypatch)
    rows = [_row("001"), _row("002")]
    done = _import(svc, rows)
    before = (list(svc._memberships.created), list(svc._memberships.updates), len(svc._audit.events))

    again = svc.apply_batch(done.id, rows=rows, requester_max_role_scope=RoleScope.ADMIN)
    assert again.status == RosterImportStatus.COMPLETED
    assert (svc._memberships.created, svc._memberships.updates, len(svc._audit.events)) == before


def test_replay_after_a_lost_cursor_update_is_idempotent_and_does_not_duplicate_results(monkeypatch) -> None:
    svc = _service(monkeypatch)
    rows = [_row("001"), _row("002"), _row("003")]
    job = svc.dry_run(institution_id=INST, rows=rows, requester_max_role_scope=RoleScope.ADMIN, uploaded_by="u")
    svc.apply_batch(job.id, rows=rows, requester_max_role_scope=RoleScope.ADMIN, batch_size=2)
    # The writes landed but the cursor update was lost (crash): the job still says cursor 0.
    svc._jobs.jobs[job.id] = svc._jobs.jobs[job.id].model_copy(update={"applied_cursor": 0, "status": RosterImportStatus.DRY_RUN})

    replayed = svc.apply_batch(job.id, rows=rows, requester_max_role_scope=RoleScope.ADMIN, batch_size=2)

    assert svc._memberships.created == ["001", "002"]  # nothing re-created
    assert [r.action for r in replayed.row_results[:2]] == ["skip_unchanged", "skip_unchanged"]
    assert [r.row_index for r in replayed.row_results] == sorted({r.row_index for r in replayed.row_results})


def test_apply_refuses_rows_that_differ_from_the_dry_run(monkeypatch) -> None:
    svc = _service(monkeypatch)
    rows = [_row("001"), _row("002")]
    job = svc.dry_run(institution_id=INST, rows=rows, requester_max_role_scope=RoleScope.ADMIN, uploaded_by="u")

    swapped = [_row("001"), _row("002", "attacker@evil.test")]
    with pytest.raises(RosterRowsChangedError):
        svc.apply_batch(job.id, rows=swapped, requester_max_role_scope=RoleScope.ADMIN)
    with pytest.raises(RosterRowsChangedError):
        svc.apply_batch(job.id, rows=rows + [_row("003")], requester_max_role_scope=RoleScope.ADMIN)
    assert svc._memberships.members == {}


def test_apply_of_an_unknown_job_is_a_lookup_error(monkeypatch) -> None:
    with pytest.raises(LookupError):
        _service(monkeypatch).apply_batch("nope", rows=[_row("001")], requester_max_role_scope=RoleScope.ADMIN)


# ── removing a row is never a delete; a revocation is never undone ───────────

def test_members_missing_from_a_later_import_are_neither_removed_nor_deactivated(monkeypatch) -> None:
    members = {"001": _member("001"), "002": _member("002", uid="u2"), "003": _member("003", role=RoleScope.FACULTY, uid="u3")}
    svc = _service(monkeypatch, members)
    _import(svc, [_row("001")])
    assert set(svc._memberships.members) == {"001", "002", "003"}
    assert svc._memberships.members["002"].uid == "u2" and svc._memberships.members["002"].status == MembershipStatus.ACTIVE
    assert svc._memberships.members["003"].role_scope == RoleScope.FACULTY


def test_an_import_does_not_reactivate_a_revoked_member(monkeypatch) -> None:
    svc = _service(monkeypatch, {"001": _member("001", status=MembershipStatus.REVOKED, version=2)})
    done = _import(svc, [_row("001")])
    assert (done.row_results[0].ok, done.row_results[0].error) == (False, "membership_revoked_reinstate_required")
    member = svc._memberships.members["001"]
    assert (member.status, member.authorization_version) == (MembershipStatus.REVOKED, 2)
    assert svc._memberships.updates == []


# ── role and contact-email authority ─────────────────────────────────────────

def test_a_student_level_uploader_cannot_change_emails_demote_admins_or_grant_roles(monkeypatch) -> None:
    members = {"001": _member("001", email="old@college.edu", uid="u1"), "002": _member("002", role=RoleScope.ADMIN, uid="u2")}
    svc = _service(monkeypatch, members)
    rows = [_row("001", "new@college.edu"), _row("002"), _row("003", role="faculty")]
    done = _import(svc, rows, max_scope=RoleScope.STUDENT, can_change_email=False)
    assert [r.error for r in done.row_results] == [
        "contact_email_change_requires_admin",
        "uploader_not_authorized_to_modify:admin",
        "uploader_not_authorized_to_grant:faculty",
    ]
    assert svc._memberships.members["001"].contact_email == "old@college.edu"
    assert svc._memberships.members["002"].role_scope == RoleScope.ADMIN
    assert "003" not in svc._memberships.members


def test_a_super_admin_level_import_grants_faculty_and_audits_it_with_the_change(monkeypatch) -> None:
    svc = _service(monkeypatch)
    _import(svc, [_row("T01", role="faculty")], max_scope=RoleScope.ADMIN)
    assert svc._memberships.members["T01"].role_scope == RoleScope.FACULTY
    grant = next(e for e in svc._audit.events if e.kind == "membership_role_granted")
    assert grant.actor_uid == "uploader" and grant.institution_id == INST
    assert grant.detail["identity_key"] == "T01" and grant.detail["role_scope"] == "faculty" and grant.detail["via"] == "roster"


def test_an_administrator_changing_a_linked_email_keeps_the_uid_bumps_the_version_and_audits_without_the_address(monkeypatch) -> None:
    svc = _service(monkeypatch, {"001": _member("001", email="old@college.edu", uid="u1", version=3)})
    _import(svc, [_row("001", "new@college.edu")], max_scope=RoleScope.STUDENT, can_change_email=True)

    member = svc._memberships.members["001"]
    assert (member.contact_email, member.uid, member.authorization_version, member.status) == (
        "new@college.edu", "u1", 4, MembershipStatus.ACTIVE
    )
    event = next(e for e in svc._audit.events if e.kind == "membership_contact_changed")
    assert event.detail["contact_changed"] is True and event.detail["was_linked"] is True and event.uid == "u1"
    assert "college.edu" not in json.dumps(event.model_dump(mode="json"))


def test_promotion_bumps_the_version_and_records_the_previous_role(monkeypatch) -> None:
    svc = _service(monkeypatch, {"001": _member("001", uid="u1", version=1)})
    _import(svc, [_row("001", role="faculty")], max_scope=RoleScope.ADMIN)
    assert svc._memberships.members["001"].role_scope == RoleScope.FACULTY
    assert svc._memberships.members["001"].authorization_version == 2
    event = next(e for e in svc._audit.events if e.kind == "membership_role_changed")
    assert event.detail["previous_role_scope"] == "student" and event.detail["role_scope"] == "faculty"


def test_an_unchanged_row_writes_and_audits_nothing(monkeypatch) -> None:
    svc = _service(monkeypatch, {"001": _member("001", uid="u1")})
    done = _import(svc, [_row("001")])
    assert done.row_results[0].action == "skip_unchanged"
    assert svc._memberships.updates == [] and svc._memberships.created == []
    assert svc._audit.kinds() == ["roster_batch_applied"]


# ── races ───────────────────────────────────────────────────────────────────

def test_losing_a_create_race_is_reported_on_that_row_only(monkeypatch) -> None:
    svc = _service(monkeypatch)
    svc._memberships.create_race_keys = {"002"}
    done = _import(svc, [_row("001"), _row("002"), _row("003")])
    assert [(r.ok, r.error) for r in done.row_results] == [(True, None), (False, "membership_created_concurrently"), (True, None)]
    assert sorted(svc._memberships.members) == ["001", "003"]


def test_the_transaction_rechecks_stored_state_so_a_revocation_that_races_the_plan_wins(monkeypatch) -> None:
    svc = _service(monkeypatch, {"001": _member("001", email="old@college.edu", uid="u1")})
    svc._memberships.txn_view = {"001": _member("001", email="old@college.edu", uid="u1", status=MembershipStatus.REVOKED, version=2)}

    done = _import(svc, [_row("001", "new@college.edu")], can_change_email=True)

    assert (done.row_results[0].ok, done.row_results[0].error) == (False, "membership_revoked_reinstate_required")
    assert svc._memberships.updates == [] and "membership_contact_changed" not in svc._audit.kinds()


# ── audit content and per-institution normalization ─────────────────────────

def test_the_batch_audit_event_has_counts_and_no_personal_data(monkeypatch) -> None:
    svc = _service(monkeypatch, {"002": _member("002")})
    done = _import(svc, [_row("001"), _row("002"), _row("002", "dup@college.edu")])
    event = next(e for e in svc._audit.events if e.kind == "roster_batch_applied")
    assert event.detail["job_id"] == done.id
    assert (event.detail["create"], event.detail["skip_unchanged"], event.detail["failed"]) == (1, 1, 1)
    assert "@" not in json.dumps(event.model_dump(mode="json"))


def test_the_institution_policy_decides_the_stored_identity_key(monkeypatch) -> None:
    svc = _service(monkeypatch, policy=IdentityKeyPolicy(remove_characters="-/ "))
    _import(svc, [_row("21-bce-0001")])
    assert list(svc._memberships.members) == ["21BCE0001"]


# ── teacher assignment ───────────────────────────────────────────────────────

def _assign(svc, key="T01", email="teacher@college.edu", role=RoleScope.FACULTY, reason="new hire"):
    return svc.assign_teacher(institution_id=INST, identity_key=key, contact_email=email, role_scope=role,
                              actor_uid="root", reason=reason)


def test_assigning_a_teacher_creates_a_faculty_membership_and_audits_it(monkeypatch) -> None:
    svc = _service(monkeypatch)
    result = _assign(svc)
    assert (result.ok, result.action) == (True, "create")
    member = svc._memberships.members["T01"]
    assert (member.role_scope, member.status, member.uid) == (RoleScope.FACULTY, MembershipStatus.ACTIVE, None)
    event = svc._audit.events[0]
    assert event.kind == "teacher_assigned" and event.actor_uid == "root" and event.detail["reason"] == "new hire"


def test_assigning_promotes_an_existing_student_and_may_change_the_contact_email(monkeypatch) -> None:
    svc = _service(monkeypatch, {"T01": _member("T01", email="old@college.edu", uid="u7", version=2)})
    result = _assign(svc, email="teacher@college.edu", role=RoleScope.ADMIN)
    assert result.action == "update"
    member = svc._memberships.members["T01"]
    assert (member.role_scope, member.contact_email, member.uid, member.authorization_version) == (
        RoleScope.ADMIN, "teacher@college.edu", "u7", 3
    )
    assert svc._audit.events[0].detail["previous_role_scope"] == "student"


def test_assigning_the_same_teacher_twice_is_a_no_op(monkeypatch) -> None:
    svc = _service(monkeypatch)
    _assign(svc)
    events = len(svc._audit.events)
    assert _assign(svc).action == "skip_unchanged"
    assert len(svc._audit.events) == events


def test_a_revoked_member_cannot_be_assigned_until_reinstated(monkeypatch) -> None:
    svc = _service(monkeypatch, {"T01": _member("T01", email="teacher@college.edu", status=MembershipStatus.REVOKED)})
    with pytest.raises(RosterRowRejectedError) as exc:
        _assign(svc)
    assert exc.value.reason == "membership_revoked_reinstate_required"


def test_assignment_validates_the_identity_key_and_email_like_an_import(monkeypatch) -> None:
    svc = _service(monkeypatch, policy=IdentityKeyPolicy(min_length=5))
    with pytest.raises(RosterRowRejectedError) as short:
        _assign(svc, key="T01")
    assert short.value.reason == "identity_key_invalid:too_short"
    with pytest.raises(RosterRowRejectedError) as bad_email:
        _assign(svc, key="TEACH01", email="not-an-email")
    assert bad_email.value.reason == "invalid_contact_email"
