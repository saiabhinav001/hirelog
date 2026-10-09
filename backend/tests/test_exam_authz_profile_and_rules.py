"""Profile-write and Firestore-rules checks for Prompt 1 (ADR 0017).

Two separate layers, on purpose:
  * API authorization is covered in the other `test_exam_authz_*` files and
    holds on its own — the backend uses the Admin SDK, which BYPASSES
    firestore.rules, so a rule is never what protects an exam endpoint.
  * `firestore.rules` guards only direct client access. Here it is checked
    STATICALLY (the rules text). Behavioral rules tests need the Firestore
    emulator, which is outside this phase (tracked for the emulator/staging
    phase in docs/exams/delta-plan.md).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import exams_dependencies as deps
from app.api.routes import users as users_route
from app.models.schemas import UserCreate

RULES_PATH = Path(__file__).resolve().parents[2] / "firestore.rules"


def _rules_text() -> str:
    if not RULES_PATH.exists():
        pytest.skip("firestore.rules is not reachable from this checkout layout")
    return RULES_PATH.read_text(encoding="utf-8")


# ── public profile surface ───────────────────────────────────────────────────

def test_public_profile_payload_accepts_only_a_name() -> None:
    """No role, e-mail, college ID, institution or any other authority field."""
    assert set(UserCreate.model_fields) == {"name"}


class _Snapshot:
    def __init__(self, data: dict | None) -> None:
        self._data, self.exists = data, data is not None
        self.id = "uid"

    def to_dict(self):
        return dict(self._data) if self._data else {}


class _DocRef:
    def __init__(self, initial: dict | None) -> None:
        self._data = dict(initial) if initial else None
        self.set_calls: list[dict] = []

    def get(self):
        return _Snapshot(self._data)

    def set(self, data: dict, merge: bool = False) -> None:
        self.set_calls.append(data)
        self._data = {**(self._data or {}), **data}


class _Db:
    def __init__(self, doc_ref: _DocRef) -> None:
        self._doc_ref = doc_ref

    def collection(self, _name: str) -> "_Db":
        return self

    def document(self, _uid: str) -> _DocRef:
        return self._doc_ref


class _NoMemberships:
    """Stands in for `MembershipRepository(db)`: callable ctor, no memberships."""

    def __call__(self, _db):
        return self

    def find_by_uid(self, _uid: str) -> list:
        return []


def test_profile_sync_with_forged_identity_fields_creates_no_exam_authority(monkeypatch) -> None:
    """End to end across the two layers: a viewer POSTs a profile that smuggles
    a role, an allowlisted e-mail and a college id; afterwards the exam API
    still treats them as a person with no authority."""
    doc_ref = _DocRef(initial=None)
    monkeypatch.setattr(users_route, "db", _Db(doc_ref))
    monkeypatch.setattr(deps.settings, "EXAM_FACULTY_EMAILS", "prof@college.edu")
    monkeypatch.setattr(deps.settings, "EXAM_SUPER_ADMIN_EMAILS", "owner@college.edu")
    monkeypatch.setattr("app.domain.institution.repository.MembershipRepository", _NoMemberships())

    session_user = {"uid": "attacker", "email": "attacker@evil.test", "role": "viewer",
                    "token_email": "attacker@evil.test", "email_verified": True}
    payload = UserCreate.model_validate({
        "name": "Attacker", "role": "super_admin", "email": "owner@college.edu", "college_id": "CS-001",
    })
    users_route.create_or_update_user(payload, session_user)

    stored = doc_ref.set_calls[-1]
    assert stored["role"] == "viewer" and stored["email"] == "attacker@evil.test"
    assert "college_id" not in stored

    with pytest.raises(HTTPException) as faculty:
        deps.require_exam_faculty({**session_user})
    with pytest.raises(HTTPException) as admin:
        deps.require_exam_super_admin({**session_user})
    assert faculty.value.status_code == admin.value.status_code == 403


# ── firestore.rules (static) ─────────────────────────────────────────────────

LEGACY_RULE_COLLECTIONS = {"databases", "users", "interview_experiences", "practice_lists", "questions", "metadata"}


def test_rules_grant_nothing_to_exam_or_institution_collections() -> None:
    """Every collection with its own `match` block is a pre-exam legacy
    collection. Exam, institution, KT, generation, judge, proctoring, evidence,
    gradebook, similarity, live, lockdown, staff-binding and authz-audit
    collections get NO rule and fall through to the default-deny catch-all."""
    matched = set(re.findall(r"match\s+/([A-Za-z0-9_]+)/\{", _rules_text()))
    assert matched <= LEGACY_RULE_COLLECTIONS, f"unexpected rule blocks: {sorted(matched - LEGACY_RULE_COLLECTIONS)}"
    for server_only in ("exam_staff_bindings", "exam_authz_audit", "institution_memberships", "exam_definitions", "exam_runs"):
        assert server_only not in matched


def test_rules_end_with_a_default_deny_catch_all() -> None:
    text = _rules_text()
    catch_all = re.search(r"match\s+/\{document=\*\*\}\s*\{(?P<body>[^}]*)\}", text)
    assert catch_all is not None
    assert re.search(r"allow\s+read,\s*write\s*:\s*if\s+false\s*;", catch_all.group("body"))
    # nothing (other than closing braces) may follow it, so it cannot be shadowed by a later wider rule
    assert re.fullmatch(r"[\s}]*", text[catch_all.end():])


def test_users_rules_block_client_role_and_authority_changes() -> None:
    text = _rules_text()
    users_block = re.search(r"match\s+/users/\{userId\}\s*\{(?P<body>.*?)\n    \}", text, re.S)
    assert users_block is not None
    body = users_block.group("body")

    create = re.search(r"allow create:(?P<rule>.*?);", body, re.S)
    update = re.search(r"allow update:(?P<rule>.*?);", body, re.S)
    assert create is not None and update is not None
    assert "request.resource.data.role == 'viewer'" in create.group("rule")
    assert re.search(r"hasOnly\(\['uid', 'name', 'display_name', 'email', 'role', 'created_at'\]\)", create.group("rule"))
    assert "request.resource.data.role == resource.data.role" in update.group("rule")
    assert re.search(r"hasOnly\(\['name', 'display_name', 'name_last_updated_at'\]\)", update.group("rule"))
    assert "allow delete" not in body  # clients cannot delete their profile either
