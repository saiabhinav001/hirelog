"""One-time-code generation/verification — pure, no I/O, no delivery.

The code itself is NEVER stored in plaintext; only an HMAC-SHA256 digest
(keyed by a server secret + a random per-code salt) is persisted. This is a
short-lived proof-of-institutional-identity, not a password — there is no
homegrown password store here, and the code is single-use and bounded by
attempts/expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


def generate_otp_code() -> str:
    """6-digit, cryptographically random — never derived from anything
    guessable (uid, time, identity_key, etc.)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp_code(code: str, *, salt: str, secret_key: bytes) -> str:
    return hmac.new(secret_key, f"{salt}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class OtpRecord:
    code_hash: str
    salt: str
    expires_at: datetime
    attempts: int = 0
    max_attempts: int = 5
    consumed: bool = False


def new_otp_record(
    *, secret_key: bytes, now: datetime, ttl_seconds: int = 300, max_attempts: int = 5
) -> tuple[str, OtpRecord]:
    """Returns (plaintext_code_for_delivery_only, record_to_persist). The
    plaintext code must be handed straight to a delivery sink and never
    logged/stored anywhere else."""
    code = generate_otp_code()
    salt = secrets.token_hex(8)
    record = OtpRecord(
        code_hash=hash_otp_code(code, salt=salt, secret_key=secret_key),
        salt=salt,
        expires_at=now + timedelta(seconds=ttl_seconds),
        max_attempts=max_attempts,
    )
    return code, record


@dataclass(frozen=True)
class OtpVerifyOutcome:
    ok: bool
    reason: Optional[str] = None


def verify_otp(record: OtpRecord, *, submitted_code: str, secret_key: bytes, now: datetime) -> OtpVerifyOutcome:
    """Checks consumed/expired/attempts-exceeded/mismatch, in that order.
    Callers are responsible for incrementing `attempts` and setting
    `consumed=True` on success — this function only decides, never mutates."""
    if record.consumed:
        return OtpVerifyOutcome(False, "already_consumed")
    if now > record.expires_at:
        return OtpVerifyOutcome(False, "expired")
    if record.attempts >= record.max_attempts:
        return OtpVerifyOutcome(False, "attempts_exceeded")

    candidate_hash = hash_otp_code(submitted_code, salt=record.salt, secret_key=secret_key)
    if not hmac.compare_digest(candidate_hash, record.code_hash):
        return OtpVerifyOutcome(False, "code_mismatch")
    return OtpVerifyOutcome(True)
