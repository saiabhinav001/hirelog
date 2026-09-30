"""Firestore repositories for the managed-lockdown domain. Private
collections — no client rule grants them; covered by the default-deny
catch-all.
"""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore

from app.domain.lockdown.models import LockdownAdmissionLogEntry, LockdownProfile

PROFILES_COLLECTION = "exam_lockdown_profiles"
ADMISSION_LOG_COLLECTION = "exam_lockdown_admission_log"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class LockdownProfileRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, run_id: str):
        return self._db.collection(PROFILES_COLLECTION).document(run_id)

    def get(self, run_id: str) -> Optional[LockdownProfile]:
        snap = self._ref(run_id).get()
        if not snap.exists:
            return None
        return LockdownProfile.model_validate(snap.to_dict() or {})

    def upsert(self, profile: LockdownProfile) -> None:
        self._ref(profile.run_id).set(_model_dict(profile))


class LockdownAdmissionLogRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, entry: LockdownAdmissionLogEntry) -> None:
        self._db.collection(ADMISSION_LOG_COLLECTION).document(entry.id).set(_model_dict(entry))

    def list_for_run(self, run_id: str) -> list[LockdownAdmissionLogEntry]:
        query = self._db.collection(ADMISSION_LOG_COLLECTION).where(
            filter=firestore.FieldFilter("run_id", "==", run_id)
        )
        return [
            LockdownAdmissionLogEntry.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()
        ]
