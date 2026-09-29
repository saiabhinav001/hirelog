"""Pure OTP generation/verification tests — expiry, attempt limits,
single-use consumption, and replay rejection."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.domain.institution.otp import generate_otp_code, hash_otp_code, new_otp_record, verify_otp

SECRET = b"test-secret-key"
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_generated_code_is_six_digits() -> None:
    code = generate_otp_code()
    assert len(code) == 6
    assert code.isdigit()


def test_hash_is_deterministic_for_same_salt_and_secret() -> None:
    h1 = hash_otp_code("123456", salt="abc", secret_key=SECRET)
    h2 = hash_otp_code("123456", salt="abc", secret_key=SECRET)
    assert h1 == h2


def test_hash_differs_for_different_secret() -> None:
    h1 = hash_otp_code("123456", salt="abc", secret_key=SECRET)
    h2 = hash_otp_code("123456", salt="abc", secret_key=b"different-secret")
    assert h1 != h2


def test_verify_succeeds_with_correct_code() -> None:
    code, record = new_otp_record(secret_key=SECRET, now=NOW)
    outcome = verify_otp(record, submitted_code=code, secret_key=SECRET, now=NOW)
    assert outcome.ok


def test_verify_rejects_wrong_code() -> None:
    _code, record = new_otp_record(secret_key=SECRET, now=NOW)
    outcome = verify_otp(record, submitted_code="000000", secret_key=SECRET, now=NOW)
    assert not outcome.ok
    assert outcome.reason == "code_mismatch"


def test_verify_rejects_expired_code() -> None:
    code, record = new_otp_record(secret_key=SECRET, now=NOW, ttl_seconds=60)
    later = NOW + timedelta(seconds=61)
    outcome = verify_otp(record, submitted_code=code, secret_key=SECRET, now=later)
    assert not outcome.ok
    assert outcome.reason == "expired"


def test_verify_rejects_after_max_attempts() -> None:
    code, record = new_otp_record(secret_key=SECRET, now=NOW, max_attempts=3)
    exhausted = replace(record, attempts=3)
    outcome = verify_otp(exhausted, submitted_code=code, secret_key=SECRET, now=NOW)
    assert not outcome.ok
    assert outcome.reason == "attempts_exceeded"


def test_verify_rejects_already_consumed_code_replay() -> None:
    code, record = new_otp_record(secret_key=SECRET, now=NOW)
    consumed = replace(record, consumed=True)
    outcome = verify_otp(consumed, submitted_code=code, secret_key=SECRET, now=NOW)
    assert not outcome.ok
    assert outcome.reason == "already_consumed"


def test_two_generated_records_never_share_a_salt() -> None:
    _c1, r1 = new_otp_record(secret_key=SECRET, now=NOW)
    _c2, r2 = new_otp_record(secret_key=SECRET, now=NOW)
    assert r1.salt != r2.salt
