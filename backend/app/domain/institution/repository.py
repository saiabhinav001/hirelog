"""Firestore repositories for institution/membership/roster/OTP. Private
collections — covered by the default-deny catch-all in firestore.rules.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.domain.institution.models import Institution, Membership, RosterImportJob
from app.domain.institution.normalization import make_membership_doc_id
from app.domain.institution.otp import OtpRecord

INSTITUTIONS_COLLECTION = "institutions"
MEMBERSHIPS_COLLECTION = "institution_memberships"
ROSTER_IMPORTS_COLLECTION = "institution_roster_imports"
OTP_CHALLENGES_COLLECTION = "institution_otp_challenges"


class _UidMembershipCache:
    """Short-lived uid -> memberships cache. Every membership write made through
    `MembershipRepository` invalidates the affected uid immediately, so within
    one process a revocation is visible on the very next request; the TTL only
    bounds staleness for writes made by another process."""

    def __init__(self, max_entries: int = 4096) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, list[Membership]]] = {}
        self._max = max_entries

    def get(self, uid: str, ttl: float) -> Optional[list[Membership]]:
        with self._lock:
            hit = self._entries.get(uid)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return list(hit[1])
            return None

    def put(self, uid: str, memberships: list[Membership]) -> None:
        with self._lock:
            if len(self._entries) >= self._max:
                self._entries.pop(next(iter(self._entries)))
            self._entries[uid] = (time.monotonic(), list(memberships))

    def invalidate(self, uid: Optional[str]) -> None:
        if uid:
            with self._lock:
                self._entries.pop(uid, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


membership_cache = _UidMembershipCache()


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class InstitutionRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, institution: Institution) -> None:
        self._db.collection(INSTITUTIONS_COLLECTION).document(institution.id).set(_model_dict(institution))

    def get(self, institution_id: str) -> Optional[Institution]:
        snap = self._db.collection(INSTITUTIONS_COLLECTION).document(institution_id).get()
        if not snap.exists:
            return None
        return Institution.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def list_all(self, limit: int = 100) -> list[Institution]:
        query = self._db.collection(INSTITUTIONS_COLLECTION).limit(limit)
        return [Institution.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]


class MembershipRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, institution_id: str, identity_key: str):
        doc_id = make_membership_doc_id(institution_id, identity_key)
        return self._db.collection(MEMBERSHIPS_COLLECTION).document(doc_id)

    def get(self, institution_id: str, identity_key: str) -> Optional[Membership]:
        snap = self._ref(institution_id, identity_key).get()
        if not snap.exists:
            return None
        return Membership.model_validate(snap.to_dict() or {})

    def get_in_transaction(self, transaction, institution_id: str, identity_key: str) -> Optional[Membership]:
        snap = self._ref(institution_id, identity_key).get(transaction=transaction)
        if not snap.exists:
            return None
        return Membership.model_validate(snap.to_dict() or {})

    def upsert(self, membership: Membership) -> None:
        self._ref(membership.institution_id, membership.identity_key).set(_model_dict(membership))
        membership_cache.invalidate(membership.uid)

    def update_in_transaction(self, transaction, institution_id: str, identity_key: str, update: dict) -> None:
        transaction.update(self._ref(institution_id, identity_key), update)
        membership_cache.invalidate(update.get("uid"))

    def existing_identity_keys(self, institution_id: str) -> set[str]:
        from firebase_admin import firestore

        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(
            filter=firestore.FieldFilter("institution_id", "==", institution_id)
        )
        return {(doc.to_dict() or {}).get("identity_key", "") for doc in query.stream()}

    def list_by_institution(self, institution_id: str) -> list[Membership]:
        """One query, one read per member (vs `existing_identity_keys` + a get per key)."""
        from firebase_admin import firestore

        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(
            filter=firestore.FieldFilter("institution_id", "==", institution_id)
        )
        return [Membership.model_validate(doc.to_dict() or {}) for doc in query.stream()]

    def find_by_uid(self, uid: str) -> list[Membership]:
        from firebase_admin import firestore

        ttl = settings.EXAM_MEMBERSHIP_CACHE_TTL_SECONDS
        if ttl > 0:
            cached = membership_cache.get(uid, ttl)
            if cached is not None:
                return cached
        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(filter=firestore.FieldFilter("uid", "==", uid))
        memberships = [Membership.model_validate(doc.to_dict() or {}) for doc in query.stream()]
        if ttl > 0:
            membership_cache.put(uid, memberships)
        return memberships


class RosterImportRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, job_id: str):
        return self._db.collection(ROSTER_IMPORTS_COLLECTION).document(job_id)

    def create(self, job: RosterImportJob) -> None:
        self._ref(job.id).set(_model_dict(job))

    def get(self, job_id: str) -> Optional[RosterImportJob]:
        snap = self._ref(job_id).get()
        if not snap.exists:
            return None
        return RosterImportJob.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update(self, job_id: str, update: dict) -> None:
        self._ref(job_id).update(update)


class OtpRepository:
    """One active challenge per (institution_id, identity_key) — a new
    challenge request overwrites the previous one (invalidating it)."""

    def __init__(self, db) -> None:
        self._db = db

    def _doc_id(self, institution_id: str, identity_key: str) -> str:
        return make_membership_doc_id(institution_id, identity_key)

    def save(self, institution_id: str, identity_key: str, record: OtpRecord) -> None:
        self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key)).set(
            {
                "code_hash": record.code_hash,
                "salt": record.salt,
                "expires_at": record.expires_at.isoformat(),
                "attempts": record.attempts,
                "max_attempts": record.max_attempts,
                "consumed": record.consumed,
            }
        )

    def get(self, institution_id: str, identity_key: str) -> Optional[OtpRecord]:
        snap = self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key)).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        return OtpRecord(
            code_hash=data["code_hash"],
            salt=data["salt"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 5)),
            consumed=bool(data.get("consumed", False)),
        )

    def increment_attempts(self, institution_id: str, identity_key: str) -> None:
        from firebase_admin import firestore

        self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key)).update(
            {"attempts": firestore.Increment(1)}
        )

    def mark_consumed(self, institution_id: str, identity_key: str) -> None:
        self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key)).update(
            {"consumed": True}
        )

    def consume_once(self, institution_id: str, identity_key: str) -> bool:
        """Atomically flips `consumed` False -> True. Exactly one of any number
        of concurrent verifications of the same code can succeed."""
        from firebase_admin import firestore

        ref = self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key))
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> bool:
            snap = ref.get(transaction=transaction)
            if not snap.exists or (snap.to_dict() or {}).get("consumed"):
                return False
            transaction.update(ref, {"consumed": True})
            return True

        return _txn(transaction)
