"""Thin wiring for institutional auth: OTP challenge/verify, verified account
linking, active-membership/authorization-version checks, and the audited
recovery actions (revoke / reinstate / unlink). Firebase custom sign-in is
issued ONLY after a successful OTP proof — a token signature alone never
establishes institutional faculty/student membership (master prompt).

An e-mail OTP proves control of a mailbox; it is not multi-factor authentication.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from uuid import uuid4

from fastapi import HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin import firestore

from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.institution.errors import (
    AccountAlreadyLinkedError,
    IdentityAlreadyLinkedError,
    MembershipNotActiveError,
    StaleAuthorizationVersionError,
    UnauthorizedRoleGrantError,
)
from app.domain.institution.identity_provider import OtpDelivery, UnconfiguredOtpDelivery, build_otp_delivery
from app.domain.institution.models import IdentityKeyPolicy, Membership, MembershipStatus, RoleScope
from app.domain.institution.normalization import normalize_email, normalize_identity_key
from app.domain.institution.otp import OtpVerifyOutcome, new_otp_record, verify_otp
from app.domain.institution.repository import InstitutionRepository, MembershipRepository, OtpRepository
from app.domain.institution.roster_import import can_grant_role

logger = logging.getLogger("hirelog.institution.auth")

_challenge_limiter = SlidingWindowLimiter(5, 300, namespace="institution_otp_challenge")  # 5 / 5 min per identity
_verify_limiter = SlidingWindowLimiter(8, 300, namespace="institution_otp_verify")

# Ephemeral fallback secret — single-process only. Set EXAM_OTP_HMAC_SECRET
# for any multi-instance/production deployment (documented setup requirement;
# in production OTP sign-in is refused without it).
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


def default_otp_delivery() -> OtpDelivery:
    return build_otp_delivery(provider=settings.EXAM_OTP_DELIVERY_PROVIDER, production=settings.is_production)


_GENERIC_CHALLENGE_ACK = {"status": "ok", "detail": "If this identifier is valid, a code was sent."}


@dataclass(frozen=True)
class VerifyResult:
    custom_token: str
    uid: str


class InstitutionAuthService:
    def __init__(self, db, *, otp_delivery: Optional[OtpDelivery] = None) -> None:
        self._db = db
        self._memberships = MembershipRepository(db)
        self._institutions = InstitutionRepository(db)
        self._audit = AuthzAuditRepository(db)
        self._otp_repo = OtpRepository(db)
        self._delivery = otp_delivery or default_otp_delivery()

    def _policy(self, institution_id: str) -> Optional[IdentityKeyPolicy]:
        institution = self._institutions.get(institution_id)
        return institution.identity_policy if institution else None

    def _key(self, institution_id: str, identity_key: str) -> str:
        return normalize_identity_key(identity_key, self._policy(institution_id))

    def _require_otp_available(self) -> None:
        """Uniform for every caller (so it reveals nothing about any identity):
        no delivery provider, or production without a shared HMAC secret, means
        OTP sign-in is simply not available on this deployment."""
        if isinstance(self._delivery, UnconfiguredOtpDelivery) or (
            settings.is_production and not settings.EXAM_OTP_HMAC_SECRET
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "otp_delivery_not_configured", "message": "OTP sign-in is not available on this deployment."},
            )

    def challenge(self, *, institution_id: str, identity_key: str, contact_email: str) -> dict:
        """Always returns the same generic ack — never reveals whether the
        identifier exists (no user enumeration), including when delivery fails."""
        self._require_otp_available()
        key = self._key(institution_id, identity_key)
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
        except Exception:  # noqa: BLE001 - a delivery failure must look identical to success
            logger.exception("OTP delivery failed for an active identity")
        return _GENERIC_CHALLENGE_ACK

    def _consume_otp(self, institution_id: str, key: str, code: str) -> OtpVerifyOutcome:
        """Check-and-consume in ONE transaction, so two concurrent submissions of the
        same code cannot both succeed."""
        transaction = self._db.transaction()
        secret = _otp_secret_key()

        @firestore.transactional
        def _txn(transaction) -> OtpVerifyOutcome:
            record = self._otp_repo.get_in_transaction(transaction, institution_id, key)
            if record is None:
                return OtpVerifyOutcome(False, "no_challenge")
            outcome = verify_otp(record, submitted_code=code, secret_key=secret, now=datetime.now(timezone.utc))
            if outcome.ok:
                self._otp_repo.mark_consumed_in_transaction(transaction, institution_id, key)
            else:
                self._otp_repo.increment_attempts_in_transaction(transaction, institution_id, key)
            return outcome

        return _txn(transaction)

    def _verify_code(self, *, institution_id: str, identity_key: str, code: str) -> Membership:
        key = self._key(institution_id, identity_key)
        _verify_limiter.check(f"{institution_id}:{key}", detail="Too many verification attempts. Try again later.")

        membership = self._memberships.get(institution_id, key)
        if membership is None or membership.status != MembershipStatus.ACTIVE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_code")

        outcome = self._consume_otp(institution_id, key, code)
        if not outcome.ok:
            suffix = "" if outcome.reason == "no_challenge" else f":{outcome.reason}"
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid_or_expired_code{suffix}")
        return membership

    def verify_and_sign_in(self, *, institution_id: str, identity_key: str, code: str) -> VerifyResult:
        """Fresh institutional sign-in (no pre-existing legacy session). If
        the membership has no linked uid yet, one is provisioned now,
        transactionally, so a concurrent double-verify never provisions two
        different uids for the same identity."""
        membership = self._verify_code(institution_id=institution_id, identity_key=identity_key, code=code)
        key = membership.identity_key
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> str:
            current = self._memberships.get_in_transaction(transaction, institution_id, key)
            if current is None or current.status != MembershipStatus.ACTIVE:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_code")
            if current.uid:
                return current.uid
            new_uid = f"exam-{uuid4().hex}"
            self._memberships.update_in_transaction(transaction, institution_id, key, {"uid": new_uid})
            self._audit.append_in_transaction(
                transaction,
                build_audit_event(
                    "identity_provisioned", uid=new_uid, institution_id=institution_id, detail={"identity_key": key}
                ),
            )
            return new_uid

        uid = _txn(transaction)
        refreshed = self._memberships.get(institution_id, key)
        custom_token = firebase_auth.create_custom_token(
            uid, developer_claims={"exam_authz_version": refreshed.authorization_version, "exam_institution_id": institution_id}
        )
        return VerifyResult(custom_token=custom_token.decode("utf-8"), uid=uid)

    def link_existing_account(self, *, institution_id: str, identity_key: str, code: str, existing_uid: str) -> None:
        """Requires proof of BOTH: caller already authenticated as
        `existing_uid` (checked by the route dependency before this is
        called) AND a fresh OTP proof of institutional identity. Preserves
        the existing uid and legacy ownership. Two conflicts are refused, never
        resolved silently, and each is audited: the identity is already linked
        to a DIFFERENT uid, or this uid already holds a DIFFERENT identity in the
        same institution (its membership would be ambiguous). Recovery is the
        audited `unlink` action."""
        membership = self._verify_code(institution_id=institution_id, identity_key=identity_key, code=code)
        key = membership.identity_key

        other = next(
            (m for m in self._memberships.find_by_uid(existing_uid) if m.institution_id == institution_id and m.identity_key != key),
            None,
        )
        if other is not None:
            self._audit.append(
                build_audit_event(
                    "identity_link_conflict", uid=existing_uid, institution_id=institution_id,
                    detail={"identity_key": key, "reason": "uid_already_linked_in_institution", "other_identity_key": other.identity_key},
                )
            )
            raise AccountAlreadyLinkedError("this account is already linked to a different identity in this institution")

        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> tuple[str, Optional[str]]:
            current = self._memberships.get_in_transaction(transaction, institution_id, key)
            if current is None or current.status != MembershipStatus.ACTIVE:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_or_expired_code")
            if current.uid == existing_uid:
                return "already_linked", None
            if current.uid is not None:
                return "conflict", current.uid
            self._memberships.update_in_transaction(transaction, institution_id, key, {"uid": existing_uid})
            self._audit.append_in_transaction(
                transaction,
                build_audit_event("identity_linked", uid=existing_uid, institution_id=institution_id, detail={"identity_key": key}),
            )
            return "linked", None

        outcome, bound_uid = _txn(transaction)
        if outcome == "conflict":
            logger.warning(
                "Account-link conflict: identity_key=%s already linked to uid=%s, attempted by uid=%s",
                key, bound_uid, existing_uid,
            )
            self._audit.append(
                build_audit_event(
                    "identity_link_conflict", uid=existing_uid, institution_id=institution_id,
                    detail={"identity_key": key, "reason": "identity_already_linked", "bound_uid": bound_uid},
                )
            )
            raise IdentityAlreadyLinkedError(f"identity {key!r} is already linked to a different account")

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

    def revoke(
        self,
        *,
        institution_id: str,
        identity_key: str,
        actor_uid: Optional[str] = None,
        reason: Optional[str] = None,
        requester_max_role_scope: Optional[RoleScope] = None,
    ) -> Membership:
        """Admin action — bumps authorization_version so any already-issued
        token/session is rejected on its next request, even though the
        Firebase token itself remains cryptographically valid until it
        expires (contract: revocation during a session must be observed).
        Idempotent: revoking a revoked membership changes and audits nothing."""
        return self._admin_change(
            institution_id=institution_id, identity_key=identity_key, kind="membership_revoked",
            actor_uid=actor_uid, reason=reason, requester_max_role_scope=requester_max_role_scope,
            decide=lambda m: None if m.status == MembershipStatus.REVOKED else ({"status": MembershipStatus.REVOKED.value}, {}),
        )

    def reinstate(
        self,
        *,
        institution_id: str,
        identity_key: str,
        actor_uid: Optional[str] = None,
        reason: Optional[str] = None,
        requester_max_role_scope: Optional[RoleScope] = None,
    ) -> Membership:
        """The only way back from a revocation — a roster import never undoes one."""
        return self._admin_change(
            institution_id=institution_id, identity_key=identity_key, kind="membership_reinstated",
            actor_uid=actor_uid, reason=reason, requester_max_role_scope=requester_max_role_scope,
            decide=lambda m: None if m.status == MembershipStatus.ACTIVE else ({"status": MembershipStatus.ACTIVE.value}, {}),
        )

    def unlink(
        self,
        *,
        institution_id: str,
        identity_key: str,
        actor_uid: Optional[str] = None,
        reason: Optional[str] = None,
        requester_max_role_scope: Optional[RoleScope] = None,
    ) -> Membership:
        """Conflict recovery: detaches the linked account so the rightful owner can
        link (or sign in) again. The bound account loses this membership at once
        (its next request finds none). Enrollment snapshots of runs already
        created still hold the old uid; they need re-enrolling."""
        return self._admin_change(
            institution_id=institution_id, identity_key=identity_key, kind="identity_unlinked",
            actor_uid=actor_uid, reason=reason, requester_max_role_scope=requester_max_role_scope,
            decide=lambda m: None if m.uid is None else ({"uid": None}, {"previous_uid": m.uid}),
        )

    def _admin_change(
        self,
        *,
        institution_id: str,
        identity_key: str,
        kind: str,
        actor_uid: Optional[str],
        reason: Optional[str],
        requester_max_role_scope: Optional[RoleScope],
        decide: Callable[[Membership], Optional[tuple[dict, dict]]],
    ) -> Membership:
        """One transaction: read, authorize against the membership's CURRENT role,
        change it (always bumping authorization_version) and write the audit event.
        Changing a membership requires being allowed to grant its role, so an
        institution admin cannot touch faculty/admin memberships."""
        key = self._key(institution_id, identity_key)
        now = datetime.now(timezone.utc)
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> Membership:
            current = self._memberships.get_in_transaction(transaction, institution_id, key)
            if current is None:
                raise LookupError(f"membership {key!r} not found in institution {institution_id}")
            if requester_max_role_scope is not None and not can_grant_role(requester_max_role_scope, current.role_scope):
                raise UnauthorizedRoleGrantError(f"not permitted to change a {current.role_scope.value} membership")
            change = decide(current)
            if change is None:
                return current
            fields, detail = change
            update = {**fields, "authorization_version": current.authorization_version + 1, "updated_at": now.isoformat()}
            self._memberships.update_in_transaction(transaction, institution_id, key, update)
            self._audit.append_in_transaction(
                transaction,
                build_audit_event(
                    kind, uid=current.uid or "", actor_uid=actor_uid, institution_id=institution_id,
                    detail={"identity_key": key, "reason": reason, **detail}, now=now,
                ),
            )
            return Membership.model_validate({**current.model_dump(), **update})

        return _txn(transaction)
