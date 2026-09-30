"""Firestore repositories for the gradebook domain. Private collections —
no client rule grants them; covered by the default-deny catch-all.
"""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore

from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    ExportAccessLogEntry,
    ExportDownloadTicket,
    ExportJob,
    GradeAppeal,
    ReleaseRecord,
    SimilarityMatch,
)

RELEASES_COLLECTION = "gradebook_releases"
DEFECTIVE_QUESTIONS_COLLECTION = "gradebook_defective_questions"
GRADE_APPEALS_COLLECTION = "gradebook_grade_appeals"
EXPORT_JOBS_COLLECTION = "gradebook_export_jobs"
EXPORT_TICKETS_COLLECTION = "gradebook_export_tickets"
EXPORT_ACCESS_LOG_COLLECTION = "gradebook_export_access_log"
SIMILARITY_MATCHES_COLLECTION = "similarity_matches"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class ReleaseRecordRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, release: ReleaseRecord) -> None:
        self._db.collection(RELEASES_COLLECTION).document(release.id).set(_model_dict(release))

    def list_for_run(self, run_id: str) -> list[ReleaseRecord]:
        query = self._db.collection(RELEASES_COLLECTION).where(filter=firestore.FieldFilter("run_id", "==", run_id))
        return [ReleaseRecord.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def latest_for_run(self, run_id: str) -> Optional[ReleaseRecord]:
        releases = self.list_for_run(run_id)
        if not releases:
            return None
        return max(releases, key=lambda r: r.version)


class DefectiveQuestionResolutionRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, resolution: DefectiveQuestionResolution) -> None:
        self._db.collection(DEFECTIVE_QUESTIONS_COLLECTION).document(resolution.id).set(_model_dict(resolution))

    def list_for_run(self, run_id: str) -> list[DefectiveQuestionResolution]:
        query = self._db.collection(DEFECTIVE_QUESTIONS_COLLECTION).where(
            filter=firestore.FieldFilter("run_id", "==", run_id)
        )
        return [
            DefectiveQuestionResolution.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()
        ]


class GradeAppealRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, appeal_id: str):
        return self._db.collection(GRADE_APPEALS_COLLECTION).document(appeal_id)

    def create(self, appeal: GradeAppeal) -> None:
        self._ref(appeal.id).set(_model_dict(appeal))

    def get(self, appeal_id: str) -> Optional[GradeAppeal]:
        snap = self._ref(appeal_id).get()
        if not snap.exists:
            return None
        return GradeAppeal.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update(self, appeal_id: str, update: dict) -> None:
        self._ref(appeal_id).update(update)

    def list_for_run(self, run_id: str) -> list[GradeAppeal]:
        query = self._db.collection(GRADE_APPEALS_COLLECTION).where(filter=firestore.FieldFilter("run_id", "==", run_id))
        return [GradeAppeal.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def list_for_student(self, run_id: str, uid: str) -> list[GradeAppeal]:
        return [a for a in self.list_for_run(run_id) if a.uid == uid]


class ExportJobRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, job_id: str):
        return self._db.collection(EXPORT_JOBS_COLLECTION).document(job_id)

    def create(self, job: ExportJob) -> None:
        self._ref(job.id).set(_model_dict(job))

    def get(self, job_id: str) -> Optional[ExportJob]:
        snap = self._ref(job_id).get()
        if not snap.exists:
            return None
        return ExportJob.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update(self, job_id: str, update: dict) -> None:
        self._ref(job_id).update(update)


class ExportDownloadTicketRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, ticket_id: str):
        return self._db.collection(EXPORT_TICKETS_COLLECTION).document(ticket_id)

    def create(self, ticket: ExportDownloadTicket) -> None:
        self._ref(ticket.id).set(_model_dict(ticket))

    def get_in_transaction(self, transaction, ticket_id: str) -> Optional[ExportDownloadTicket]:
        snap = self._ref(ticket_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return ExportDownloadTicket.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def mark_consumed_in_transaction(self, transaction, ticket_id: str, *, consumed_at) -> None:
        transaction.update(self._ref(ticket_id), {"consumed": True, "consumed_at": consumed_at.isoformat()})


class ExportAccessLogRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, entry: ExportAccessLogEntry) -> None:
        self._db.collection(EXPORT_ACCESS_LOG_COLLECTION).document(entry.id).set(_model_dict(entry))

    def list_for_job(self, export_job_id: str) -> list[ExportAccessLogEntry]:
        query = self._db.collection(EXPORT_ACCESS_LOG_COLLECTION).where(
            filter=firestore.FieldFilter("export_job_id", "==", export_job_id)
        )
        return [ExportAccessLogEntry.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]


class SimilarityMatchRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, match: SimilarityMatch) -> None:
        self._db.collection(SIMILARITY_MATCHES_COLLECTION).document(match.id).set(_model_dict(match))

    def replace_for_question(self, run_id: str, question_id: str, matches: list[SimilarityMatch]) -> None:
        """Re-running similarity for a question supersedes its prior
        matches entirely (a stale match from a previously-submitted-then-
        resubmitted answer must not linger)."""
        for existing in self.list_for_question(run_id, question_id):
            self._db.collection(SIMILARITY_MATCHES_COLLECTION).document(existing.id).delete()
        for match in matches:
            self.create(match)

    def list_for_question(self, run_id: str, question_id: str) -> list[SimilarityMatch]:
        query = (
            self._db.collection(SIMILARITY_MATCHES_COLLECTION)
            .where(filter=firestore.FieldFilter("run_id", "==", run_id))
            .where(filter=firestore.FieldFilter("question_id", "==", question_id))
        )
        return [SimilarityMatch.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def list_for_run(self, run_id: str) -> list[SimilarityMatch]:
        query = self._db.collection(SIMILARITY_MATCHES_COLLECTION).where(filter=firestore.FieldFilter("run_id", "==", run_id))
        return [SimilarityMatch.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]
