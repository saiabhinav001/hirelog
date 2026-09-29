"""Firestore repositories for institution/membership/roster/OTP. Private
collections — covered by the default-deny catch-all in firestore.rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.domain.institution.models import Institution, Membership, RosterImportJob
from app.domain.institution.normalization import make_membership_doc_id
from app.domain.institution.otp import OtpRecord

INSTITUTIONS_COLLECTION = "institutions"
MEMBERSHIPS_COLLECTION = "institution_memberships"
ROSTER_IMPORTS_COLLECTION = "institution_roster_imports"
OTP_CHALLENGES_COLLECTION = "institution_otp_challenges"


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

    def update_in_transaction(self, transaction, institution_id: str, identity_key: str, update: dict) -> None:
        transaction.update(self._ref(institution_id, identity_key), update)

    def existing_identity_keys(self, institution_id: str) -> set[str]:
        from firebase_admin import firestore

        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(
            filter=firestore.FieldFilter("institution_id", "==", institution_id)
        )
        return {(doc.to_dict() or {}).get("identity_key", "") for doc in query.stream()}

    def find_by_uid(self, uid: str) -> list[Membership]:
        from firebase_admin import firestore

        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(filter=firestore.FieldFilter("uid", "==", uid))
        return [Membership.model_validate(doc.to_dict() or {}) for doc in query.stream()]


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
