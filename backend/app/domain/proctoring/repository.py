"""Firestore repositories for the proctoring domain. Private collections —
no client rule grants them; covered by the default-deny catch-all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from firebase_admin import firestore

from app.domain.proctoring.models import (
    Appeal,
    EvidenceAccessLogEntry,
    EvidenceSnapshot,
    IncidentLogEntry,
    ProctoringState,
    UploadTicket,
    ViewTicket,
)

STATES_COLLECTION = "proctoring_states"
LOG_COLLECTION = "proctoring_log"
SNAPSHOTS_COLLECTION = "evidence_snapshots"
UPLOAD_TICKETS_COLLECTION = "evidence_upload_tickets"
VIEW_TICKETS_COLLECTION = "evidence_view_tickets"
ACCESS_LOG_COLLECTION = "evidence_access_log"
APPEALS_COLLECTION = "proctoring_appeals"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class ProctoringStateRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _doc_id(self, run_id: str, uid: str) -> str:
        return f"{run_id}__{uid}"

    def _ref(self, run_id: str, uid: str):
        return self._db.collection(STATES_COLLECTION).document(self._doc_id(run_id, uid))

    def get(self, run_id: str, uid: str) -> Optional[ProctoringState]:
        snap = self._ref(run_id, uid).get()
        if not snap.exists:
            return None
        return ProctoringState.model_validate(snap.to_dict() or {})

    def get_in_transaction(self, transaction, run_id: str, uid: str) -> Optional[ProctoringState]:
        snap = self._ref(run_id, uid).get(transaction=transaction)
        if not snap.exists:
            return None
        return ProctoringState.model_validate(snap.to_dict() or {})

    def upsert_in_transaction(self, transaction, state: ProctoringState) -> None:
        transaction.set(self._ref(state.run_id, state.uid), _model_dict(state))

    def upsert(self, state: ProctoringState) -> None:
        self._ref(state.run_id, state.uid).set(_model_dict(state))

    def list_open_incidents(self, *, limit: int) -> list[ProctoringState]:
        """Sweep target — states with a still-open incident, regardless of
        run/attempt, so one periodic sweep covers every active exam."""
        query = self._db.collection(STATES_COLLECTION).where(
            filter=firestore.FieldFilter("open_incident_started_at", "!=", None)
        ).limit(limit)
        return [ProctoringState.model_validate(doc.to_dict() or {}) for doc in query.stream()]


class IncidentLogRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create_in_transaction(self, transaction, entry: IncidentLogEntry) -> None:
        ref = self._db.collection(LOG_COLLECTION).document(entry.id)
        transaction.set(ref, _model_dict(entry))

    def create(self, entry: IncidentLogEntry) -> None:
        self._db.collection(LOG_COLLECTION).document(entry.id).set(_model_dict(entry))

    def list_for_attempt(self, run_id: str, uid: str) -> list[IncidentLogEntry]:
        query = (
            self._db.collection(LOG_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .where(filter=firestore.FieldFilter("uid", "==", uid))
        )
        return [IncidentLogEntry.model_validate(doc.to_dict() or {}) for doc in query.stream()]


class EvidenceSnapshotRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, snapshot_id: str):
        return self._db.collection(SNAPSHOTS_COLLECTION).document(snapshot_id)

    def create(self, snapshot: EvidenceSnapshot) -> None:
        self._ref(snapshot.id).set(_model_dict(snapshot))

    def get(self, snapshot_id: str) -> Optional[EvidenceSnapshot]:
        snap = self._ref(snapshot_id).get()
        if not snap.exists:
            return None
        return EvidenceSnapshot.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_in_transaction(self, transaction, snapshot_id: str) -> Optional[EvidenceSnapshot]:
        snap = self._ref(snapshot_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return EvidenceSnapshot.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update_in_transaction(self, transaction, snapshot_id: str, update: dict) -> None:
        transaction.update(self._ref(snapshot_id), update)

    def update(self, snapshot_id: str, update: dict) -> None:
        self._ref(snapshot_id).update(update)

    def list_for_attempt(self, run_id: str, uid: str) -> list[EvidenceSnapshot]:
        query = (
            self._db.collection(SNAPSHOTS_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .where(filter=firestore.FieldFilter("uid", "==", uid))
        )
        return [EvidenceSnapshot.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def list_all(self, *, limit: int) -> list[EvidenceSnapshot]:
        """Retention sweep target — bounded batch, safe to call repeatedly."""
        query = self._db.collection(SNAPSHOTS_COLLECTION).limit(limit)
        return [EvidenceSnapshot.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def list_oldest(self, *, limit: int, after_requested_at: Optional[str] = None) -> list[EvidenceSnapshot]:
        """Oldest first, so a bounded retention scan reaches the snapshots that are due before newer ones."""
        query = self._db.collection(SNAPSHOTS_COLLECTION).order_by("requested_at")
        if after_requested_at is not None:
            query = query.start_after({"requested_at": after_requested_at})
        return [EvidenceSnapshot.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.limit(limit).stream()]

    def list_requested(self, *, limit: int) -> list[EvidenceSnapshot]:
        query = self._db.collection(SNAPSHOTS_COLLECTION).where(filter=firestore.FieldFilter("status", "==", "requested")).limit(limit)
        return [EvidenceSnapshot.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    def delete(self, snapshot_id: str) -> None:
        self._ref(snapshot_id).delete()


class UploadTicketRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, ticket_id: str):
        return self._db.collection(UPLOAD_TICKETS_COLLECTION).document(ticket_id)

    def create(self, ticket: UploadTicket) -> None:
        self._ref(ticket.id).set(_model_dict(ticket))

    def get_in_transaction(self, transaction, ticket_id: str) -> Optional[UploadTicket]:
        snap = self._ref(ticket_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return UploadTicket.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def mark_consumed_in_transaction(self, transaction, ticket_id: str, *, consumed_at: datetime) -> None:
        transaction.update(self._ref(ticket_id), {"consumed": True, "consumed_at": consumed_at.isoformat()})


class ViewTicketRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, ticket_id: str):
        return self._db.collection(VIEW_TICKETS_COLLECTION).document(ticket_id)

    def create(self, ticket: ViewTicket) -> None:
        self._ref(ticket.id).set(_model_dict(ticket))

    def get(self, ticket_id: str) -> Optional[ViewTicket]:
        snap = self._ref(ticket_id).get()
        if not snap.exists:
            return None
        return ViewTicket.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_in_transaction(self, transaction, ticket_id: str) -> Optional[ViewTicket]:
        snap = self._ref(ticket_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return ViewTicket.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def mark_consumed_in_transaction(self, transaction, ticket_id: str, *, consumed_at: datetime) -> None:
        transaction.update(self._ref(ticket_id), {"consumed": True, "consumed_at": consumed_at.isoformat()})


class EvidenceAccessLogRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, entry: EvidenceAccessLogEntry) -> None:
        self._db.collection(ACCESS_LOG_COLLECTION).document(entry.id).set(_model_dict(entry))

    def list_for_snapshot(self, snapshot_id: str) -> list[EvidenceAccessLogEntry]:
        query = self._db.collection(ACCESS_LOG_COLLECTION).where(
            filter=firestore.FieldFilter("snapshot_id", "==", snapshot_id)
        )
        return [EvidenceAccessLogEntry.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]


class AppealRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, appeal_id: str):
        return self._db.collection(APPEALS_COLLECTION).document(appeal_id)

    def create(self, appeal: Appeal) -> None:
        self._ref(appeal.id).set(_model_dict(appeal))

    def get(self, appeal_id: str) -> Optional[Appeal]:
        snap = self._ref(appeal_id).get()
        if not snap.exists:
            return None
        return Appeal.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update(self, appeal_id: str, update: dict) -> None:
        self._ref(appeal_id).update(update)

    def create_in_transaction(self, transaction, appeal: Appeal) -> None:
        transaction.set(self._ref(appeal.id), _model_dict(appeal))

    def get_in_transaction(self, transaction, appeal_id: str) -> Optional[Appeal]:
        snap = self._ref(appeal_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return Appeal.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update_in_transaction(self, transaction, appeal_id: str, update: dict) -> None:
        transaction.update(self._ref(appeal_id), update)

    def list_for_attempt_in_transaction(self, transaction, run_id: str, uid: str) -> list[Appeal]:
        query = (
            self._db.collection(APPEALS_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .where(filter=firestore.FieldFilter("uid", "==", uid))
        )
        return [Appeal.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream(transaction=transaction)]

    def list_for_run(self, run_id: str) -> list[Appeal]:
        query = self._db.collection(APPEALS_COLLECTION).where(filter=firestore.FieldFilter("run_id", "==", run_id))
        return [Appeal.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]
