"""Firestore repository for blueprints, generation requests, and candidates.
Private collections — covered by the default-deny catch-all."""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore

from app.domain.generation.models import ExamBlueprint, GenerationCandidate, GenerationRequest

BLUEPRINTS_COLLECTION = "exam_blueprints"
REQUESTS_COLLECTION = "generation_requests"
CANDIDATES_COLLECTION = "generation_candidates"
MALFORMED_COLLECTION = "generation_malformed_candidates"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class GenerationRepository:
    def __init__(self, db) -> None:
        self._db = db

    # ── Blueprints ─────────────────────────────────────────────────────────

    def create_blueprint(self, blueprint: ExamBlueprint) -> None:
        self._db.collection(BLUEPRINTS_COLLECTION).document(blueprint.id).set(_model_dict(blueprint))

    def get_blueprint(self, blueprint_id: str) -> Optional[ExamBlueprint]:
        snap = self._db.collection(BLUEPRINTS_COLLECTION).document(blueprint_id).get()
        if not snap.exists:
            return None
        return ExamBlueprint.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def list_blueprints_by_definition(self, exam_definition_id: str) -> list[ExamBlueprint]:
        query = self._db.collection(BLUEPRINTS_COLLECTION).where(
            filter=firestore.FieldFilter("exam_definition_id", "==", exam_definition_id)
        )
        return [ExamBlueprint.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()]

    # ── Requests ───────────────────────────────────────────────────────────

    def create_request(self, request: GenerationRequest) -> None:
        self._db.collection(REQUESTS_COLLECTION).document(request.id).set(_model_dict(request))

    def get_request(self, request_id: str) -> Optional[GenerationRequest]:
        snap = self._db.collection(REQUESTS_COLLECTION).document(request_id).get()
        if not snap.exists:
            return None
        return GenerationRequest.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update_request(self, request_id: str, update: dict) -> None:
        self._db.collection(REQUESTS_COLLECTION).document(request_id).update(update)

    # ── Candidates ─────────────────────────────────────────────────────────

    def save_candidate(self, candidate: GenerationCandidate) -> None:
        self._db.collection(CANDIDATES_COLLECTION).document(candidate.id).set(_model_dict(candidate))

    def get_candidate(self, candidate_id: str) -> Optional[GenerationCandidate]:
        snap = self._db.collection(CANDIDATES_COLLECTION).document(candidate_id).get()
        if not snap.exists:
            return None
        return GenerationCandidate.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_candidate_in_transaction(self, transaction, candidate_id: str) -> Optional[GenerationCandidate]:
        snap = self._db.collection(CANDIDATES_COLLECTION).document(candidate_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return GenerationCandidate.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update_candidate_in_transaction(self, transaction, candidate_id: str, update: dict) -> None:
        ref = self._db.collection(CANDIDATES_COLLECTION).document(candidate_id)
        transaction.update(ref, update)

    def approved_candidates_for_slot(self, blueprint_id: str, slot_index: int) -> list[GenerationCandidate]:
        query = (
            self._db.collection(CANDIDATES_COLLECTION)
            .where(filter=firestore.FieldFilter("blueprint_id", "==", blueprint_id))
            .where(filter=firestore.FieldFilter("slot_index", "==", slot_index))
            .where(filter=firestore.FieldFilter("status", "==", "approved"))
        )
        return [
            GenerationCandidate.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            for doc in query.stream()
        ]

    def list_candidates_by_blueprint(self, blueprint_id: str) -> list[GenerationCandidate]:
        """Question/test review screen data — every candidate regardless of
        status or which generation request produced it."""
        query = self._db.collection(CANDIDATES_COLLECTION).where(
            filter=firestore.FieldFilter("blueprint_id", "==", blueprint_id)
        )
        return [
            GenerationCandidate.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            for doc in query.stream()
        ]

    def previously_used_signatures(self, blueprint_id: str) -> set[str]:
        """All uniqueness_signatures already used across this blueprint's
        candidates (any status) — a fresh generation job must not repeat one."""
        query = self._db.collection(CANDIDATES_COLLECTION).where(
            filter=firestore.FieldFilter("blueprint_id", "==", blueprint_id)
        )
        return {
            str((doc.to_dict() or {}).get("uniqueness_signature"))
            for doc in query.stream()
            if (doc.to_dict() or {}).get("uniqueness_signature")
        }

    def record_malformed_candidate(self, request_id: str, raw: dict, issues) -> None:
        self._db.collection(MALFORMED_COLLECTION).add(
            {
                "request_id": request_id,
                "raw": raw,
                "issues": [i.model_dump(mode="json") for i in issues],
            }
        )


VARIANT_ASSIGNMENTS_COLLECTION = "exam_variant_assignments"


class VariantAssignmentRepository:
    """Private, Admin-SDK-only. A later phase reads this when delivering
    real per-student question content for a slot (see docs/adr/0009)."""

    def __init__(self, db) -> None:
        self._db = db

    def _doc_id(self, run_id: str, uid: str, slot_index: int) -> str:
        return f"{run_id}__{uid}__{slot_index}"

    def save_assignment(self, *, run_id: str, uid: str, slot_index: int, candidate_id: str, now) -> None:
        self._db.collection(VARIANT_ASSIGNMENTS_COLLECTION).document(
            self._doc_id(run_id, uid, slot_index)
        ).set(
            {
                "run_id": run_id,
                "uid": uid,
                "slot_index": slot_index,
                "candidate_id": candidate_id,
                "assigned_at": now.isoformat(),
            }
        )

    def get_assignment(self, *, run_id: str, uid: str, slot_index: int) -> Optional[str]:
        snap = self._db.collection(VARIANT_ASSIGNMENTS_COLLECTION).document(
            self._doc_id(run_id, uid, slot_index)
        ).get()
        if not snap.exists:
            return None
        return (snap.to_dict() or {}).get("candidate_id")
