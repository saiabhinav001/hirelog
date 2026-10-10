"""Thin wiring for institutional auth: OTP challenge/verify, verified account
linking, and active-membership/authorization-version checks. Firebase custom
sign-in is issued ONLY after a successful OTP proof — a token signature alone
never establishes institutional faculty/student membership (master prompt).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin import firestore

from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.institution.errors import (
    IdentityAlreadyLinkedError,
    MembershipNotActiveError,
    StaleAuthorizationVersionError,
)
from app.domain.institution.identity_provider import OtpDelivery, OtpDeliveryError, default_otp_delivery
from app.domain.institution.models import Membership, MembershipStatus
from app.domain.institution.normalization import normalize_email, normalize_identity_key
from app.domain.institution.otp import new_otp_record, verify_otp
from app.domain.institution.repository import MembershipRepository

logger = logging.getLogger("hirelog.institution.auth")

_challenge_limiter = SlidingWindowLimiter(5, 300, namespace="institution_otp_challenge")  # 5 / 5 min per identity
_verify_limiter = SlidingWindowLimiter(8, 300, namespace="institution_otp_verify")

# Ephemeral fallback secret — single-process only. Set EXAM_OTP_HMAC_SECRET
# for any multi-instance/production deployment (documented setup requirement).
_fallback_secret = uuid4().hex.encode("utf-8")
if not settings.EXAM_OTP_HMAC_SECRET:
    logger.warning(
        "EXAM_OTP_HMAC_SECRET is not set — using a random per-process secret. "
        "OTP verification will fail across process restarts/multiple instances "
        "until a shared secret is configured."
    )


def _otp_secret_key() -> bytes:
    if settings.EXAM_OTP_HMAC_SECRET:
        return settings.EXAM_OTP_HMAC_SECRET.encode("utf-8")
    return _fallback_secret


_GENERIC_CHALLENGE_ACK = {"status": "ok", "detail": "If this identifier is valid, a code was sent."}
_INVALID_CODE = "invalid_or_expired_code"


@dataclass(frozen=True)
class VerifyResult:
    custom_token: str
    uid: str


class InstitutionAuthService:
    def __init__(self, db, *, otp_delivery: Optional[OtpDelivery] = None) -> None:
        self._db = db
        self._memberships = MembershipRepository(db)
        from app.domain.institution.repository import OtpRepository

        self._otp_repo = OtpRepository(db)
        self._delivery = otp_delivery or default_otp_delivery()

    def challenge(self, *, institution_id: str, identity_key: str, contact_email: str) -> dict:
        """Always returns the same generic ack — never reveals whether the
        identifier exists (no user enumeration)."""
        if not getattr(self._delivery, "configured", True):
            # Checked before any identity lookup so it cannot act as an oracle.
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="otp_delivery_not_configured")
        key = normalize_identity_key(identity_key)
        email = normalize_email(contact_email)
        _challenge_limiter.check(f"{institution_id}:{key}", detail="Too many code requests. Try again later.")

        membership = self._memberships.get(institution_id, key)
        if membership is None or membership.status != MembershipStatus.ACTIVE or membership.contact_email != email:
            logger.info("OTP challenge for unknown/inactive/email-mismatched identity — no code sent (generic ack)")
            return _GENERIC_CHALLENGE_ACK

        now = datetime.now(timezone.utc)
        code, record = new_otp_record(
            secret_key=_otp_secret_key(), now=now,
            ttl_seconds=settings.EXAM_OTP_TTL_SECONDS, max_attempts=settings.EXAM_OTP_MAX_ATTEMPTS,
        )
        self._otp_repo.save(institution_id, key, record)
        try:
            self._delivery.send(contact_email=email, code=code)
        except OtpDeliveryError:
            # Same ack as success: a visible failure would reveal the identity exists.
            logger.exception("OTP delivery failed for institution %s", institution_id)
        return _GENERIC_CHALLENGE_ACK

    def _verify_code(self, *, institution_id: str, identity_key: str, code: str) -> Membership:
        key = normalize_identity_key(identity_key)
        _verify_limiter.check(f"{institution_id}:{key}", detail="Too many verification attempts. Try again later.")

        membership = self._memberships.get(institution_id, key)
        if membership is None or membership.status != MembershipStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_CODE)

        record = self._otp_repo.get(institution_id, key)
        if record is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_CODE)

        now = datetime.now(timezone.utc)
        outcome = verify_otp(record, submitted_code=code, secret_key=_otp_secret_key(), now=now)
        if not outcome.ok:
            logger.info("OTP verification rejected (%s)", outcome.reason)  # reason stays server-side
            self._otp_repo.increment_attempts(institution_id, key)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_CODE)

        if not self._otp_repo.consume_once(institution_id, key):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_CODE)
        return membership

    def verify_and_sign_in(self, *, institution_id: str, identity_key: str, code: str) -> VerifyResult:
        """Fresh institutional sign-in (no pre-existing legacy session). If
        the membership has no linked uid yet, one is provisioned now,
        transactionally, so a concurrent double-verify never provisions two
        different uids for the same identity."""
        membership = self._verify_code(institution_id=institution_id, identity_key=identity_key, code=code)

        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> str:
            current = self._memberships.get_in_transaction(transaction, institution_id, membership.identity_key)
            if current and current.uid:
                return current.uid
            new_uid = f"exam-{uuid4().hex}"
            self._memberships.update_in_transaction(
                transaction, institution_id, membership.identity_key, {"uid": new_uid}
            )
            return new_uid

        uid = _txn(transaction)
        refreshed = self._memberships.get(institution_id, membership.identity_key)
        custom_token = firebase_auth.create_custom_token(
            uid, developer_claims={"exam_authz_version": refreshed.authorization_version, "exam_institution_id": institution_id}
        )
        return VerifyResult(custom_token=custom_token.decode("utf-8"), uid=uid)

    def link_existing_account(self, *, institution_id: str, identity_key: str, code: str, existing_uid: str) -> None:
        """Requires proof of BOTH: caller already authenticated as
        `existing_uid` (checked by the route dependency before this is
        called) AND a fresh OTP proof of institutional identity. Preserves
        the existing uid and legacy ownership; a membership already linked
        to a DIFFERENT uid is an explicit conflict, never silently
        overwritten."""
        membership = self._verify_code(institution_id=institution_id, identity_key=identity_key, code=code)

        if membership.uid == existing_uid:
            return  # already linked to this exact account — idempotent no-op

        if membership.uid is not None:
            logger.warning(
                "Account-link conflict: identity_key=%s already linked to uid=%s, attempted by uid=%s",
                membership.identity_key, membership.uid, existing_uid,
            )
            raise IdentityAlreadyLinkedError(
                f"identity {membership.identity_key!r} is already linked to a different account"
            )

        self._memberships.upsert(membership.model_copy(update={"uid": existing_uid}))

    def check_active_membership(self, *, uid: str, institution_id: str, token_authz_version: Optional[int]) -> Membership:
        """Used by the exam-auth dependency on every request — re-reads
        Firestore fresh (no cache), so revocation is observed immediately."""
        candidates = [m for m in self._memberships.find_by_uid(uid) if m.institution_id == institution_id]
        if not candidates:
            raise MembershipNotActiveError(f"uid {uid} has no membership in institution {institution_id}")
        membership = candidates[0]
        if membership.status != MembershipStatus.ACTIVE:
            raise MembershipNotActiveError(f"membership for uid {uid} is not active")
        if token_authz_version is not None and token_authz_version != membership.authorization_version:
            raise StaleAuthorizationVersionError(
                f"token authz_version={token_authz_version} != current={membership.authorization_version}"
            )
        return membership

    def revoke(self, *, institution_id: str, identity_key: str) -> None:
        """Admin action — bumps authorization_version so any already-issued
        token/session is rejected on its next request, even though the
        Firebase token itself remains cryptographically valid until it
        expires (contract: revocation during a session must be observed)."""
        key = normalize_identity_key(identity_key)
        membership = self._memberships.get(institution_id, key)
        if membership is None:
            raise LookupError(f"membership {key!r} not found in institution {institution_id}")
        self._memberships.upsert(
            membership.model_copy(
                update={
                    "status": MembershipStatus.REVOKED,
                    "authorization_version": membership.authorization_version + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
        )
