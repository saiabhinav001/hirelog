"""Firestore repositories for institution/membership/roster/OTP. Private
collections — covered by the default-deny catch-all in firestore.rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from google.api_core.exceptions import AlreadyExists

from app.domain.institution.models import Institution, Membership, RosterImportJob
from app.domain.institution.normalization import make_membership_doc_id
from app.domain.institution.otp import OtpRecord

INSTITUTIONS_COLLECTION = "institutions"
MEMBERSHIPS_COLLECTION = "institution_memberships"
ROSTER_IMPORTS_COLLECTION = "institution_roster_imports"
OTP_CHALLENGES_COLLECTION = "institution_otp_challenges"

_GET_ALL_CHUNK = 300


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

    def list_all(self) -> list[Institution]:
        return [
            Institution.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            for doc in self._db.collection(INSTITUTIONS_COLLECTION).stream()
        ]


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

    def get_many(self, institution_id: str, identity_keys: Sequence[str]) -> dict[str, Membership]:
        """Point reads (batched `get_all`) for just the keys asked for, keyed by
        normalized identity key — so a roster batch never scans the institution."""
        unique = list(dict.fromkeys(identity_keys))
        found: dict[str, Membership] = {}
        for start in range(0, len(unique), _GET_ALL_CHUNK):
            refs = [self._ref(institution_id, key) for key in unique[start : start + _GET_ALL_CHUNK]]
            for snap in self._db.get_all(refs):
                if snap.exists:
                    membership = Membership.model_validate(snap.to_dict() or {})
                    found[membership.identity_key] = membership
        return found

    def create_if_absent(self, membership: Membership) -> bool:
        """Atomic: False when the identity already has a membership."""
        try:
            self._ref(membership.institution_id, membership.identity_key).create(_model_dict(membership))
        except AlreadyExists:
            return False
        return True

    def create_in_transaction(self, transaction, membership: Membership) -> None:
        transaction.create(self._ref(membership.institution_id, membership.identity_key), _model_dict(membership))

    def update_in_transaction(self, transaction, institution_id: str, identity_key: str, update: dict) -> None:
        transaction.update(self._ref(institution_id, identity_key), update)

    def existing_identity_keys(self, institution_id: str) -> set[str]:
        from firebase_admin import firestore

        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(
            filter=firestore.FieldFilter("institution_id", "==", institution_id)
        )
        return {(doc.to_dict() or {}).get("identity_key", "") for doc in query.stream()}

    def list_by_institution(self, institution_id: str) -> list[Membership]:
        from firebase_admin import firestore

        query = self._db.collection(MEMBERSHIPS_COLLECTION).where(
            filter=firestore.FieldFilter("institution_id", "==", institution_id)
        )
        return [Membership.model_validate(doc.to_dict() or {}) for doc in query.stream()]

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
        # None-valued fields are omitted: a 5000-row job must stay under Firestore's 1 MiB document limit.
        self._ref(job.id).set(job.model_dump(mode="json", exclude_none=True))

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
        return self._record_from_snapshot(snap)

    def get_in_transaction(self, transaction, institution_id: str, identity_key: str) -> Optional[OtpRecord]:
        ref = self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key))
        return self._record_from_snapshot(ref.get(transaction=transaction))

    @staticmethod
    def _record_from_snapshot(snap) -> Optional[OtpRecord]:
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

    def increment_attempts_in_transaction(self, transaction, institution_id: str, identity_key: str) -> None:
        from firebase_admin import firestore

        ref = self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key))
        transaction.update(ref, {"attempts": firestore.Increment(1)})

    def mark_consumed_in_transaction(self, transaction, institution_id: str, identity_key: str) -> None:
        ref = self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key))
        transaction.update(ref, {"consumed": True})

    def increment_attempts(self, institution_id: str, identity_key: str) -> None:
        from firebase_admin import firestore

        self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key)).update(
            {"attempts": firestore.Increment(1)}
        )

    def mark_consumed(self, institution_id: str, identity_key: str) -> None:
        self._db.collection(OTP_CHALLENGES_COLLECTION).document(self._doc_id(institution_id, identity_key)).update(
            {"consumed": True}
        )
