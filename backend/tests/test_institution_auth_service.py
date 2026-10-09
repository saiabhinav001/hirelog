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
    AccountAlreadyLinkedError,
    IdentityAlreadyLinkedError,
    MembershipNotActiveError,
    StaleAuthorizationVersionError,
    UnauthorizedRoleGrantError,
)
from app.domain.institution.identity_provider import (
    DevSinkOtpDelivery,
    OtpDeliveryUnavailableError,
    UnconfiguredOtpDelivery,
    build_otp_delivery,
)
from app.domain.institution.models import IdentityKeyPolicy, Institution, Membership, MembershipStatus, RoleScope

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
        self.txn_log: list[str] = []  # which reads/writes happened inside the consume transaction

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

    # The service consumes codes inside one transaction (patched to a passthrough).
    def get_in_transaction(self, _transaction, institution_id, identity_key):
        self.txn_log.append("get")
        return self.get(institution_id, identity_key)

    def increment_attempts_in_transaction(self, _transaction, institution_id, identity_key) -> None:
        self.txn_log.append("attempt")
        self.increment_attempts(institution_id, identity_key)

    def mark_consumed_in_transaction(self, _transaction, institution_id, identity_key) -> None:
        self.txn_log.append("consume")
        self.mark_consumed(institution_id, identity_key)


class _FakeInstitutionRepo:
    def __init__(self, policy: IdentityKeyPolicy | None = None) -> None:
        self._policy = policy

    def get(self, institution_id):
        return Institution(id=institution_id, name="College", created_at=NOW, updated_at=NOW, identity_policy=self._policy)


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list = []

    def append(self, event) -> None:
        self.events.append(event)

    def append_in_transaction(self, _transaction, event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


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


def _service(monkeypatch, *, memberships: dict[str, Membership] | None = None, policy: IdentityKeyPolicy | None = None):
    service = InstitutionAuthService.__new__(InstitutionAuthService)
    service._db = _FakeDb()
    service._memberships = _FakeMembershipRepo(memberships)
    service._institutions = _FakeInstitutionRepo(policy)
    service._audit = _FakeAudit()
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


# ══ Prompt 2 (docs/adr/0018): delivery, atomic OTP, per-institution IDs, link audit, recovery ══

def _member_in(service_memberships: dict, key: str, **overrides) -> None:
    service_memberships[f"{INSTITUTION}::{key}"] = _membership(key, **overrides)


def _wrong_code(code: str) -> str:
    return "111111" if code != "111111" else "222222"


# ── delivery: adapter contract + synthetic sink only ─────────────────────────

@pytest.mark.parametrize("provider,production,expected", [
    ("", False, DevSinkOtpDelivery),
    ("dev_sink", False, DevSinkOtpDelivery),
    (" DEV_SINK ", False, DevSinkOtpDelivery),
    ("", True, UnconfiguredOtpDelivery),
    ("dev_sink", True, UnconfiguredOtpDelivery),  # the sink can never be built in production
    ("sendgrid", False, UnconfiguredOtpDelivery),  # no adapter registered: fail closed, do not guess
    ("sendgrid", True, UnconfiguredOtpDelivery),
])
def test_delivery_selection_matrix(provider: str, production: bool, expected: type) -> None:
    assert type(build_otp_delivery(provider=provider, production=production)) is expected


def test_the_unconfigured_adapter_never_delivers() -> None:
    with pytest.raises(OtpDeliveryUnavailableError):
        UnconfiguredOtpDelivery().send(contact_email="a@college.edu", code="123456")


def test_unconfigured_delivery_makes_otp_signin_unavailable_for_everyone_alike(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2A01")
    service = _service(monkeypatch, memberships=memberships)
    service._delivery = UnconfiguredOtpDelivery()

    for key in ("P2A01", "NOBODY"):  # a real and an unknown identity must be indistinguishable
        with pytest.raises(HTTPException) as exc:
            service.challenge(institution_id=INSTITUTION, identity_key=key, contact_email="student@college.edu")
        assert exc.value.status_code == 503 and exc.value.detail["code"] == "otp_delivery_not_configured"
    assert service._otp_repo._records == {}  # no code was even created


def test_production_refuses_otp_without_a_shared_hmac_secret(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2A02")
    service = _service(monkeypatch, memberships=memberships)
    monkeypatch.setattr(auth_service_module.settings, "ENV", "production")
    monkeypatch.setattr(auth_service_module.settings, "EXAM_OTP_HMAC_SECRET", None)
    with pytest.raises(HTTPException) as exc:
        service.challenge(institution_id=INSTITUTION, identity_key="P2A02", contact_email="student@college.edu")
    assert exc.value.status_code == 503

    monkeypatch.setattr(auth_service_module.settings, "EXAM_OTP_HMAC_SECRET", "a-shared-secret")
    ack = service.challenge(institution_id=INSTITUTION, identity_key="P2A02", contact_email="student@college.edu")
    assert ack["status"] == "ok" and service._delivery.last_sent_code_for("student@college.edu")


def test_a_delivery_failure_looks_exactly_like_success_and_leaks_nothing(monkeypatch) -> None:
    class _Down:
        def send(self, **_kwargs) -> None:
            raise RuntimeError("smtp unavailable")

    memberships: dict = {}
    _member_in(memberships, "P2A03")
    service = _service(monkeypatch, memberships=memberships)
    service._delivery = _Down()

    real = service.challenge(institution_id=INSTITUTION, identity_key="P2A03", contact_email="student@college.edu")
    unknown = service.challenge(institution_id=INSTITUTION, identity_key="P2A99", contact_email="student@college.edu")
    assert real == unknown == auth_service_module._GENERIC_CHALLENGE_ACK


# ── OTP: single use, atomic, attempt-capped, replaced by a newer challenge ───

def test_a_code_is_checked_and_consumed_inside_one_transaction(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2B01")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2B01")

    with pytest.raises(HTTPException):
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B01", code=_wrong_code(code))
    assert service._otp_repo.txn_log == ["get", "attempt"]

    service._otp_repo.txn_log.clear()
    service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B01", code=code)
    assert service._otp_repo.txn_log == ["get", "consume"]


def test_wrong_codes_burn_attempts_and_then_even_the_right_code_is_refused(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2B02")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2B02")
    max_attempts = service._otp_repo.get(INSTITUTION, "P2B02").max_attempts

    for _ in range(max_attempts):
        with pytest.raises(HTTPException) as exc:
            service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B02", code=_wrong_code(code))
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as locked:
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B02", code=code)
    assert "attempts_exceeded" in locked.value.detail


def test_a_newer_challenge_invalidates_the_older_code(monkeypatch) -> None:
    from app.domain.institution import otp as otp_module

    codes = iter(["111111", "222222"])
    monkeypatch.setattr(otp_module, "generate_otp_code", lambda: next(codes))
    memberships: dict = {}
    _member_in(memberships, "P2B03")
    service = _service(monkeypatch, memberships=memberships)

    first = _seed_and_send_code(service, identity_key="P2B03")
    second = _seed_and_send_code(service, identity_key="P2B03")
    assert (first, second) == ("111111", "222222")
    with pytest.raises(HTTPException) as exc:
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B03", code=first)
    assert "code_mismatch" in exc.value.detail
    assert service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B03", code=second).uid


def test_a_consumed_code_cannot_be_replayed_to_link_an_account(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2B04")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2B04")
    service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B04", code=code)
    with pytest.raises(HTTPException) as exc:
        service.link_existing_account(institution_id=INSTITUTION, identity_key="P2B04", code=code, existing_uid="legacy-1")
    assert "already_consumed" in exc.value.detail


def test_a_code_cannot_be_used_after_the_membership_is_revoked(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2B05")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2B05")
    service.revoke(institution_id=INSTITUTION, identity_key="P2B05", actor_uid="admin")
    with pytest.raises(HTTPException) as exc:
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2B05", code=code)
    assert exc.value.status_code == 400


def test_a_revoked_member_is_sent_no_code(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2B06", status=MembershipStatus.REVOKED)
    service = _service(monkeypatch, memberships=memberships)
    ack = service.challenge(institution_id=INSTITUTION, identity_key="P2B06", contact_email="student@college.edu")
    assert ack["status"] == "ok" and service._delivery.last_sent_code_for("student@college.edu") is None


# ── rate limits ─────────────────────────────────────────────────────────────

def test_challenge_and_verify_are_rate_limited_per_identity(monkeypatch) -> None:
    monkeypatch.setattr("app.core.rate_limit._get_redis", lambda: None)  # in-memory window, whatever the dev env has
    memberships: dict = {}
    _member_in(memberships, "P2RL01")
    _member_in(memberships, "P2RL02")
    service = _service(monkeypatch, memberships=memberships)

    for _ in range(5):
        service.challenge(institution_id=INSTITUTION, identity_key="P2RL01", contact_email="student@college.edu")
    with pytest.raises(HTTPException) as challenge_limit:
        service.challenge(institution_id=INSTITUTION, identity_key="P2RL01", contact_email="student@college.edu")
    assert challenge_limit.value.status_code == 429

    for _ in range(8):
        with pytest.raises(HTTPException) as bad:
            service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2RL02", code="000000")
        assert bad.value.status_code == 400
    with pytest.raises(HTTPException) as verify_limit:
        service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2RL02", code="000000")
    assert verify_limit.value.status_code == 429


def test_formatting_variants_of_one_identity_share_a_single_rate_limit_bucket(monkeypatch) -> None:
    monkeypatch.setattr("app.core.rate_limit._get_redis", lambda: None)
    memberships: dict = {}
    _member_in(memberships, "P2RL03X")
    service = _service(monkeypatch, memberships=memberships, policy=IdentityKeyPolicy(remove_characters="-/ "))
    spellings = ["P2RL03-X", "p2rl03/x", "P2 RL03 X", "p2rl03x", "P2RL-03X"]
    for spelling in spellings:
        service.challenge(institution_id=INSTITUTION, identity_key=spelling, contact_email="student@college.edu")
    with pytest.raises(HTTPException) as exc:
        service.challenge(institution_id=INSTITUTION, identity_key="P2-RL03X", contact_email="student@college.edu")
    assert exc.value.status_code == 429


# ── per-institution identity policy at login ────────────────────────────────

def test_login_normalizes_the_college_id_with_the_institutions_policy(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "21BCE0001")
    service = _service(monkeypatch, memberships=memberships, policy=IdentityKeyPolicy(remove_characters="-/ "))
    service.challenge(institution_id=INSTITUTION, identity_key="21-bce-0001", contact_email="student@college.edu")
    code = service._delivery.last_sent_code_for("student@college.edu")
    assert code
    assert service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="21/BCE/0001", code=code).uid.startswith("exam-")


def test_without_a_policy_the_separators_still_matter(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "21BCE0002")
    service = _service(monkeypatch, memberships=memberships)
    service.challenge(institution_id=INSTITUTION, identity_key="21-bce-0002", contact_email="student@college.edu")
    assert service._delivery.last_sent_code_for("student@college.edu") is None  # a different identity entirely


# ── linking: audited conflicts, no silent overwrite ──────────────────────────

def test_an_identity_linked_elsewhere_is_a_persisted_audited_conflict(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2L01", uid="someone-elses-uid")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2L01")
    with pytest.raises(IdentityAlreadyLinkedError):
        service.link_existing_account(institution_id=INSTITUTION, identity_key="P2L01", code=code, existing_uid="my-uid")

    assert service._memberships.get(INSTITUTION, "P2L01").uid == "someone-elses-uid"
    event = service._audit.events[-1]
    assert event.kind == "identity_link_conflict" and event.uid == "my-uid" and event.institution_id == INSTITUTION
    assert event.detail["reason"] == "identity_already_linked" and event.detail["bound_uid"] == "someone-elses-uid"


def test_one_account_cannot_hold_two_identities_in_the_same_institution(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2L02A", uid="legacy-uid-1")
    _member_in(memberships, "P2L02B")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2L02B")
    with pytest.raises(AccountAlreadyLinkedError):
        service.link_existing_account(institution_id=INSTITUTION, identity_key="P2L02B", code=code, existing_uid="legacy-uid-1")

    assert service._memberships.get(INSTITUTION, "P2L02B").uid is None
    event = service._audit.events[-1]
    assert event.kind == "identity_link_conflict" and event.detail["reason"] == "uid_already_linked_in_institution"
    assert event.detail["other_identity_key"] == "P2L02A"


def test_one_account_may_hold_identities_in_different_institutions(monkeypatch) -> None:
    memberships: dict = {}
    memberships["inst-2::OTHER1"] = _membership("OTHER1", institution_id="inst-2", uid="legacy-uid-1")
    _member_in(memberships, "P2L03")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2L03")
    service.link_existing_account(institution_id=INSTITUTION, identity_key="P2L03", code=code, existing_uid="legacy-uid-1")
    assert service._memberships.get(INSTITUTION, "P2L03").uid == "legacy-uid-1"


def test_a_successful_link_is_audited_and_keeps_the_legacy_uid(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2L04")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2L04")
    service.link_existing_account(institution_id=INSTITUTION, identity_key="P2L04", code=code, existing_uid="legacy-uid-9")

    assert service._memberships.get(INSTITUTION, "P2L04").uid == "legacy-uid-9"
    assert service._audit.kinds() == ["identity_linked"]
    # The legacy account (which never had exam membership) now passes the membership check.
    assert service.check_active_membership(uid="legacy-uid-9", institution_id=INSTITUTION, token_authz_version=None)


def test_relinking_the_same_account_is_silent(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2L05", uid="legacy-uid-1")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2L05")
    service.link_existing_account(institution_id=INSTITUTION, identity_key="P2L05", code=code, existing_uid="legacy-uid-1")
    assert service._audit.events == []


def test_linking_needs_the_emailed_code_not_just_the_college_id(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2L06")
    service = _service(monkeypatch, memberships=memberships)
    code = _seed_and_send_code(service, identity_key="P2L06")
    with pytest.raises(HTTPException) as exc:
        service.link_existing_account(institution_id=INSTITUTION, identity_key="P2L06", code=_wrong_code(code), existing_uid="legacy-1")
    assert exc.value.status_code == 400
    assert service._memberships.get(INSTITUTION, "P2L06").uid is None and service._audit.events == []


def test_first_otp_login_audits_the_provisioned_uid_once(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2L07")
    service = _service(monkeypatch, memberships=memberships)
    first = service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2L07",
                                       code=_seed_and_send_code(service, identity_key="P2L07"))
    second = service.verify_and_sign_in(institution_id=INSTITUTION, identity_key="P2L07",
                                        code=_seed_and_send_code(service, identity_key="P2L07"))
    assert first.uid == second.uid
    assert service._audit.kinds() == ["identity_provisioned"] and service._audit.events[0].uid == first.uid


# ── recovery: revoke / reinstate / unlink, all audited ──────────────────────

def test_revoke_audits_who_and_why_and_is_idempotent(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2V01", uid="u1")
    service = _service(monkeypatch, memberships=memberships)
    service.revoke(institution_id=INSTITUTION, identity_key="P2V01", actor_uid="admin-1", reason="left the college")
    service.revoke(institution_id=INSTITUTION, identity_key="P2V01", actor_uid="admin-1", reason="again")

    member = service._memberships.get(INSTITUTION, "P2V01")
    assert (member.status, member.authorization_version) == (MembershipStatus.REVOKED, 2)  # bumped once
    assert len(service._audit.events) == 1
    event = service._audit.events[0]
    assert event.kind == "membership_revoked" and event.actor_uid == "admin-1" and event.uid == "u1"
    assert event.detail == {"identity_key": "P2V01", "reason": "left the college"}


def test_a_revoked_members_old_token_is_stale_and_a_reinstated_one_needs_a_fresh_login(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2V02", uid="u1", authorization_version=1)
    service = _service(monkeypatch, memberships=memberships)
    service.revoke(institution_id=INSTITUTION, identity_key="P2V02", actor_uid="admin")
    with pytest.raises(MembershipNotActiveError):
        service.check_active_membership(uid="u1", institution_id=INSTITUTION, token_authz_version=1)

    service.reinstate(institution_id=INSTITUTION, identity_key="P2V02", actor_uid="admin", reason="reinstated after appeal")
    with pytest.raises(StaleAuthorizationVersionError):  # a token minted before the revocation never works again
        service.check_active_membership(uid="u1", institution_id=INSTITUTION, token_authz_version=1)
    assert service.check_active_membership(uid="u1", institution_id=INSTITUTION, token_authz_version=3)
    assert service._audit.kinds() == ["membership_revoked", "membership_reinstated"]


def test_reinstating_an_active_member_does_nothing(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2V03", uid="u1")
    service = _service(monkeypatch, memberships=memberships)
    service.reinstate(institution_id=INSTITUTION, identity_key="P2V03", actor_uid="admin", reason="no-op")
    assert service._audit.events == [] and service._memberships.get(INSTITUTION, "P2V03").authorization_version == 1


def test_changing_a_faculty_membership_needs_the_authority_to_grant_that_role(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2V04", role_scope=RoleScope.FACULTY, uid="t1")
    service = _service(monkeypatch, memberships=memberships)
    for action in (service.revoke, service.unlink, service.reinstate):
        with pytest.raises(UnauthorizedRoleGrantError):
            action(institution_id=INSTITUTION, identity_key="P2V04", actor_uid="adm", requester_max_role_scope=RoleScope.STUDENT)
    member = service._memberships.get(INSTITUTION, "P2V04")
    assert (member.status, member.uid, member.authorization_version) == (MembershipStatus.ACTIVE, "t1", 1)
    assert service._audit.events == []

    service.revoke(institution_id=INSTITUTION, identity_key="P2V04", actor_uid="root", requester_max_role_scope=RoleScope.ADMIN)
    assert service._memberships.get(INSTITUTION, "P2V04").status == MembershipStatus.REVOKED


def test_unlink_detaches_the_account_bumps_the_version_and_audits_the_previous_uid(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2V05", uid="wrong-uid", authorization_version=4)
    service = _service(monkeypatch, memberships=memberships)
    service.unlink(institution_id=INSTITUTION, identity_key="P2V05", actor_uid="root", reason="linked to the wrong account")

    member = service._memberships.get(INSTITUTION, "P2V05")
    assert (member.uid, member.authorization_version) == (None, 5)
    event = service._audit.events[0]
    assert event.kind == "identity_unlinked" and event.detail["previous_uid"] == "wrong-uid"
    with pytest.raises(MembershipNotActiveError):  # the detached account lost this membership at once
        service.check_active_membership(uid="wrong-uid", institution_id=INSTITUTION, token_authz_version=4)

    service.unlink(institution_id=INSTITUTION, identity_key="P2V05", actor_uid="root", reason="again")
    assert len(service._audit.events) == 1  # unlinking an unlinked identity is a no-op


def test_unlink_then_relink_recovers_a_conflict_end_to_end(monkeypatch) -> None:
    memberships: dict = {}
    _member_in(memberships, "P2V06", uid="wrong-uid")
    service = _service(monkeypatch, memberships=memberships)

    first_code = _seed_and_send_code(service, identity_key="P2V06")
    with pytest.raises(IdentityAlreadyLinkedError):
        service.link_existing_account(institution_id=INSTITUTION, identity_key="P2V06", code=first_code, existing_uid="rightful-uid")

    service.unlink(institution_id=INSTITUTION, identity_key="P2V06", actor_uid="root", reason="verified in person")
    second_code = _seed_and_send_code(service, identity_key="P2V06")
    service.link_existing_account(institution_id=INSTITUTION, identity_key="P2V06", code=second_code, existing_uid="rightful-uid")

    member = service._memberships.get(INSTITUTION, "P2V06")
    assert member.uid == "rightful-uid" and member.authorization_version == 2
    assert service._audit.kinds() == ["identity_link_conflict", "identity_unlinked", "identity_linked"]


def test_changing_an_unknown_membership_is_a_lookup_error(monkeypatch) -> None:
    service = _service(monkeypatch, memberships={})
    for action in (service.revoke, service.reinstate, service.unlink):
        with pytest.raises(LookupError):
            action(institution_id=INSTITUTION, identity_key="NOPE", actor_uid="admin")

