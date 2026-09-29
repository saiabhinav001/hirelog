"""Institutional auth service tests: challenge (generic ack, no enumeration),
verify (wrong-college, replay, revocation-during-session via authz_version),
account linking (conflict/recovery), and legacy-user-with-no-membership.

`verify_and_sign_in`'s Firestore transaction is exercised with a lightweight
in-memory membership store and `firestore.transactional` patched to a plain
passthrough — this proves the uid-provisioning LOGIC (reuse vs. mint), not
true multi-writer transactional concurrency. That still requires a real
Firestore/emulator run (see docs/adr/0010 "Known limitations").

Each test uses its own identity_key — `_challenge_limiter`/`_verify_limiter`
in auth_service.py are module-level singletons, so reusing one key across
many tests would trip the rate limit across test functions.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.domain.institution import auth_service as auth_service_module
from app.domain.institution.auth_service import InstitutionAuthService
from app.domain.institution.errors import (
    IdentityAlreadyLinkedError,
    MembershipNotActiveError,
    StaleAuthorizationVersionError,
)
from app.domain.institution.identity_provider import DevSinkOtpDelivery
from app.domain.institution.models import Membership, MembershipStatus, RoleScope

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
INSTITUTION = "inst-1"


class _FakeMembershipRepo:
    """In-memory stand-in — supports the plain AND '_in_transaction' method
    names the service calls, both operating on the same dict (no real
    Firestore transaction semantics; see module docstring)."""

    def __init__(self, memberships: dict[str, Membership] | None = None) -> None:
        self._by_key: dict[str, Membership] = dict(memberships or {})

    def get(self, institution_id, identity_key):
        return self._by_key.get(f"{institution_id}::{identity_key.upper()}")

    def get_in_transaction(self, _transaction, institution_id, identity_key):
        return self.get(institution_id, identity_key)

    def upsert(self, membership: Membership) -> None:
        self._by_key[f"{membership.institution_id}::{membership.identity_key}"] = membership

    def update_in_transaction(self, _transaction, institution_id, identity_key, update: dict) -> None:
        current = self.get(institution_id, identity_key)
        self._by_key[f"{institution_id}::{identity_key.upper()}"] = current.model_copy(update=update)

    def find_by_uid(self, uid: str) -> list[Membership]:
        return [m for m in self._by_key.values() if m.uid == uid]


class _FakeOtpRepo:
    def __init__(self) -> None:
        self._records: dict[str, object] = {}

    def save(self, institution_id, identity_key, record) -> None:
        self._records[f"{institution_id}::{identity_key.upper()}"] = record

    def get(self, institution_id, identity_key):
        return self._records.get(f"{institution_id}::{identity_key.upper()}")

    def increment_attempts(self, institution_id, identity_key) -> None:
        key = f"{institution_id}::{identity_key.upper()}"
        record = self._records[key]
        self._records[key] = replace(record, attempts=record.attempts + 1)

    def mark_consumed(self, institution_id, identity_key) -> None:
        key = f"{institution_id}::{identity_key.upper()}"
        record = self._records[key]
        self._records[key] = replace(record, consumed=True)


def _membership(identity_key: str, **overrides) -> Membership:
    defaults = dict(
        institution_id=INSTITUTION, identity_key=identity_key, contact_email="student@college.edu",
        role_scope=RoleScope.STUDENT, status=MembershipStatus.ACTIVE, uid=None,
        authorization_version=1, created_at=NOW, updated_at=NOW,
    )
    defaults.update(overrides)
    return Membership(**defaults)


class _FakeDb:
    """`.transaction()` only needs to return SOMETHING — `firestore.transactional`
    is patched to a passthrough in `_service()` below, so the returned value
    is never used for real transaction semantics."""

    def transaction(self):
        return object()


def _service(monkeypatch, *, memberships: dict[str, Membership] | None = None):
    service = InstitutionAuthService.__new__(InstitutionAuthService)
    service._db = _FakeDb()
    service._memberships = _FakeMembershipRepo(memberships)
    service._otp_repo = _FakeOtpRepo()
    service._delivery = DevSinkOtpDelivery()
    monkeypatch.setattr(auth_service_module.firestore, "transactional", lambda fn: fn)
    monkeypatch.setattr(
        auth_service_module.firebase_auth, "create_custom_token",
        lambda uid, developer_claims=None: f"token-for-{uid}".encode(),
    )
    return service


def _seed_and_send_code(service, *, identity_key: str, email: str = "student@college.edu") -> str:
    ack = service.challenge(institution_id=INSTITUTION, identity_key=identity_key, contact_email=email)
    assert ack["status"] == "ok"
    return service._delivery.last_sent_code_for(email)


# ── Challenge: no user enumeration ────────────────────────────────────────────

def test_challenge_unknown_identity_returns_generic_ack_and_sends_nothing(monkeypatch) -> None:
    service = _service(monkeypatch, memberships={})
    ack = service.challenge(institution_id=INSTITUTION, identity_key="K01", contact_email="nobody@x.edu")
    assert ack["status"] == "ok"
    assert service._delivery.last_sent_code_for("nobody@x.edu") is None


def test_challenge_email_mismatch_returns_generic_ack_and_sends_nothing(monkeypatch) -> None:
    key = f"{INSTITUTION}::K02"
    service = _service(monkeypatch, memberships={key: _membership("K02")})
    ack = service.challenge(institution_id=INSTITUTION, identity_key="K02", contact_email="attacker@evil.com")
    assert ack["status"] == "ok"
    assert service._delivery.last_sent_code_for("attacker@evil.com") is None


def test_challenge_known_active_identity_sends_a_code(monkeypatch) -> None:
    key = f"{INSTITUTION}::K03"
    service = _service(monkeypatch, memberships={key: _membership("K03")})
    code = _seed_and_send_code(service, identity_key="K03")
    assert code is not None and len(code) == 6


# ── Verify: wrong college, replay, revocation ─────────────────────────────────

def test_verify_wrong_college_is_rejected(monkeypatch) -> None:
    key = f"{INSTITUTION}::K04"
    service = _service(monkeypatch, memberships={key: _membership("K04")})
    code = _seed_and_send_code(service, identity_key="K04")
    with pytest.raises(HTTPException) as exc:
        service.verify_and_sign_in(institution_id="a-different-institution", identity_key="K04", code=code)
    assert exc.value.status_code == 400


def test_verify_provisions_a_new_uid_on_first_login(monkeypatch) -> None:
    key = f"{INSTITUTION}::K05"
    service = _service(monkeypatch, memberships={key: _membership("K05")})
    code = _seed_and_send_code(service, identity_key="K05")
    result = service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="K05", code=code)
    assert result.uid.startswith("exam-")
    assert result.custom_token == f"token-for-{result.uid}"


def test_verify_reuses_existing_uid_on_subsequent_login(monkeypatch) -> None:
    key = f"{INSTITUTION}::K06"
    service = _service(monkeypatch, memberships={key: _membership("K06", uid="exam-existing-uid")})
    code = _seed_and_send_code(service, identity_key="K06")
    result = service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="K06", code=code)
    assert result.uid == "exam-existing-uid"


def test_verify_rejects_replayed_code(monkeypatch) -> None:
    key = f"{INSTITUTION}::K07"
    service = _service(monkeypatch, memberships={key: _membership("K07")})
    code = _seed_and_send_code(service, identity_key="K07")
    service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="K07", code=code)
    with pytest.raises(HTTPException) as exc:
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="K07", code=code)
    assert exc.value.status_code == 400
    assert "already_consumed" in exc.value.detail


def test_verify_rejects_expired_code(monkeypatch) -> None:
    key = f"{INSTITUTION}::K08"
    service = _service(monkeypatch, memberships={key: _membership("K08")})
    code = _seed_and_send_code(service, identity_key="K08")
    record = service._otp_repo.get(INSTITUTION, "K08")
    service._otp_repo._records[f"{INSTITUTION}::K08"] = replace(record, expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(HTTPException):
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="K08", code=code)


# ── Account linking ────────────────────────────────────────────────────────────

def test_link_sets_uid_when_membership_has_none(monkeypatch) -> None:
    key = f"{INSTITUTION}::K09"
    service = _service(monkeypatch, memberships={key: _membership("K09")})
    code = _seed_and_send_code(service, identity_key="K09")
    service.link_existing_account(institution_id=INSTITUTION, identity_key="K09", code=code, existing_uid="legacy-uid-1")
    assert service._memberships.get(INSTITUTION, "K09").uid == "legacy-uid-1"


def test_link_is_idempotent_for_the_same_uid(monkeypatch) -> None:
    key = f"{INSTITUTION}::K10"
    service = _service(monkeypatch, memberships={key: _membership("K10", uid="legacy-uid-1")})
    code = _seed_and_send_code(service, identity_key="K10")
    service.link_existing_account(institution_id=INSTITUTION, identity_key="K10", code=code, existing_uid="legacy-uid-1")


def test_link_conflict_when_already_linked_to_different_uid(monkeypatch) -> None:
    key = f"{INSTITUTION}::K11"
    service = _service(monkeypatch, memberships={key: _membership("K11", uid="someone-elses-uid")})
    code = _seed_and_send_code(service, identity_key="K11")
    with pytest.raises(IdentityAlreadyLinkedError):
        service.link_existing_account(institution_id=INSTITUTION, identity_key="K11", code=code, existing_uid="my-uid")


# ── Active membership / revocation-during-session ─────────────────────────────

def test_check_active_membership_succeeds_with_matching_authz_version(monkeypatch) -> None:
    key = f"{INSTITUTION}::K12"
    service = _service(monkeypatch, memberships={key: _membership("K12", uid="u1", authorization_version=1)})
    membership = service.check_active_membership(uid="u1", institution_id=INSTITUTION, token_authz_version=1)
    assert membership.identity_key == "K12"


def test_check_active_membership_rejects_stale_authz_version_after_revocation(monkeypatch) -> None:
    """The token was issued when authorization_version==1; the membership was
    since revoked/bumped to 2 — the OLD token must be rejected even though
    it is still cryptographically valid and unexpired."""
    key = f"{INSTITUTION}::K13"
    service = _service(monkeypatch, memberships={key: _membership("K13", uid="u1", authorization_version=2)})
    with pytest.raises(StaleAuthorizationVersionError):
        service.check_active_membership(uid="u1", institution_id=INSTITUTION, token_authz_version=1)


def test_check_active_membership_rejects_revoked_status(monkeypatch) -> None:
    key = f"{INSTITUTION}::K14"
    service = _service(monkeypatch, memberships={key: _membership("K14", uid="u1", status=MembershipStatus.REVOKED)})
    with pytest.raises(MembershipNotActiveError):
        service.check_active_membership(uid="u1", institution_id=INSTITUTION, token_authz_version=1)


def test_legacy_user_with_no_exam_membership_raises_clean_not_active_error(monkeypatch) -> None:
    """A pre-existing legacy Hirelog user who never touched the exam module
    at all — must fail cleanly, not crash."""
    service = _service(monkeypatch, memberships={})
    with pytest.raises(MembershipNotActiveError):
        service.check_active_membership(uid="legacy-user-uid", institution_id=INSTITUTION, token_authz_version=None)


def test_revoke_bumps_authorization_version_and_deactivates(monkeypatch) -> None:
    key = f"{INSTITUTION}::K15"
    service = _service(monkeypatch, memberships={key: _membership("K15", uid="u1", authorization_version=1)})
    service.revoke(institution_id=INSTITUTION, identity_key="K15")
    updated = service._memberships.get(INSTITUTION, "K15")
    assert updated.status == MembershipStatus.REVOKED
    assert updated.authorization_version == 2
