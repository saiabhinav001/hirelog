"""Firestore repository for blueprints, generation requests, and candidates.
Private collections — covered by the default-deny catch-all."""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

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

    def claim_request(self, request_id: str, *, now, stale_after_seconds: int = 600) -> Optional[GenerationRequest]:
        """Atomically move a request to `running` and count the attempt. None when it already
        succeeded or another worker holds it (a hold older than `stale_after_seconds` is a
        crashed worker and may be taken over)."""
        transaction = self._db.transaction()
        ref = self._db.collection(REQUESTS_COLLECTION).document(request_id)

        @firestore.transactional
        def _txn(transaction) -> Optional[GenerationRequest]:
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise LookupError(f"generation request {request_id} not found")
            request = GenerationRequest.model_validate({**(snap.to_dict() or {}), "id": snap.id})
            if request.status == "succeeded":
                return None
            if request.status == "running" and request.running_since is not None:
                if (now - request.running_since).total_seconds() < stale_after_seconds:
                    return None
            transaction.update(ref, {"status": "running", "attempts": request.attempts + 1, "running_since": now.isoformat()})
            return request.model_copy(update={"status": "running", "attempts": request.attempts + 1, "running_since": now})

        return _txn(transaction)

    def list_requests_by_blueprint(self, blueprint_id: str) -> list[GenerationRequest]:
        query = self._db.collection(REQUESTS_COLLECTION).where(
            filter=firestore.FieldFilter("blueprint_id", "==", blueprint_id)
        )
        return [GenerationRequest.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def update_blueprint(self, blueprint_id: str, update: dict) -> None:
        self._db.collection(BLUEPRINTS_COLLECTION).document(blueprint_id).update(update)

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

    def create_candidate_in_transaction(self, transaction, candidate: GenerationCandidate) -> None:
        transaction.create(self._db.collection(CANDIDATES_COLLECTION).document(candidate.id), _model_dict(candidate))

    def set_candidate_in_transaction(self, transaction, candidate: GenerationCandidate) -> None:
        transaction.set(self._db.collection(CANDIDATES_COLLECTION).document(candidate.id), _model_dict(candidate))

    def approved_candidates_for_slot(self, blueprint_id: str, slot_index: int) -> list[GenerationCandidate]:
        """Candidates eligible to be served: approved, or published in a revision (published stays eligible)."""
        query = (
            self._db.collection(CANDIDATES_COLLECTION)
            .where(filter=firestore.FieldFilter("blueprint_id", "==", blueprint_id))
            .where(filter=firestore.FieldFilter("slot_index", "==", slot_index))
            .where(filter=firestore.FieldFilter("status", "in", ["approved", "published"]))
        )
        return sorted(
            (GenerationCandidate.model_validate({**(doc.to_dict() or {}), "id": doc.id}) for doc in query.stream()),
            key=lambda c: c.id,
        )

    def live_prompts_for_slot(self, blueprint_id: str, slot_index: int) -> dict[str, str]:
        """id -> public prompt of every candidate still in play for a slot (for near-duplicate checks)."""
        from app.domain.generation.models import CandidateStatus

        dead = {CandidateStatus.REJECTED, CandidateStatus.SUPERSEDED}
        return {
            c.id: c.public_prompt
            for c in self.list_candidates_by_blueprint(blueprint_id)
            if c.slot_index == slot_index and c.status not in dead
        }

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
    """Private, Admin-SDK-only. Assignments are CREATE-ONLY: once a student has been given a
    variant (and its option order) for a slot it never changes, so a later reservation cannot
    swap the question under a student who has started, or orphan their grading."""

    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, run_id: str, uid: str, slot_index: int):
        return self._db.collection(VARIANT_ASSIGNMENTS_COLLECTION).document(f"{run_id}__{uid}__{slot_index}")

    def _doc_id(self, run_id: str, uid: str, slot_index: int) -> str:
        return f"{run_id}__{uid}__{slot_index}"

    def save_assignment(
        self, *, run_id: str, uid: str, slot_index: int, candidate_id: str, now, option_order: Optional[list[str]] = None
    ) -> bool:
        """False when an assignment already exists (it is left untouched)."""
        data = {
            "run_id": run_id, "uid": uid, "slot_index": slot_index, "candidate_id": candidate_id,
            "assigned_at": now.isoformat(),
        }
        if option_order is not None:
            data["option_order"] = list(option_order)
        try:
            self._ref(run_id, uid, slot_index).create(data)
        except AlreadyExists:
            return False
        return True

    def get_assignment(self, *, run_id: str, uid: str, slot_index: int) -> Optional[str]:
        detail = self.get_assignment_detail(run_id=run_id, uid=uid, slot_index=slot_index)
        return detail.get("candidate_id") if detail else None

    def get_assignment_detail(self, *, run_id: str, uid: str, slot_index: int) -> Optional[dict]:
        snap = self._ref(run_id, uid, slot_index).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def list_for_run_slot(self, run_id: str, slot_index: int) -> list[dict]:
        query = (
            self._db.collection(VARIANT_ASSIGNMENTS_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .where(filter=firestore.FieldFilter("slot_index", "==", slot_index))
        )
        return [d.to_dict() or {} for d in query.stream()]