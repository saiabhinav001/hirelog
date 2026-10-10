"""Firestore repositories for the gradebook domain. Private collections —
no client rule grants them; covered by the default-deny catch-all.
"""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

from app.domain.gradebook.models import (
    DefectiveQuestionResolution,
    ExportAccessLogEntry,
    ExportDownloadTicket,
    ExportJob,
    GradeAppeal,
    ReleaseRecord,
    SimilarityMatch,
    StudentGradebookRow,
)

RELEASES_COLLECTION = "gradebook_releases"
RELEASE_ROWS_COLLECTION = "gradebook_release_rows"
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

    def create_if_absent(self, release: ReleaseRecord) -> bool:
        """False when that release id already exists (a concurrent release took this version number)."""
        try:
            self._db.collection(RELEASES_COLLECTION).document(release.id).create(_model_dict(release))
            return True
        except AlreadyExists:
            return False

    def list_for_run(self, run_id: str) -> list[ReleaseRecord]:
        query = self._db.collection(RELEASES_COLLECTION).where(filter=firestore.FieldFilter("run_id", "==", run_id))
        return [ReleaseRecord.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def latest_for_run(self, run_id: str) -> Optional[ReleaseRecord]:
        releases = self.list_for_run(run_id)
        if not releases:
            return None
        return max(releases, key=lambda r: r.version)


class ReleaseSnapshotRepository:
    """The frozen per-student rows a release shows. Immutable once written: a corrected release writes a new
    snapshot under a new snapshot id, it never edits an old one."""

    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, snapshot_id: str, uid: str):
        return self._db.collection(RELEASE_ROWS_COLLECTION).document(f"{snapshot_id}__{uid}")

    def put(self, snapshot_id: str, run_id: str, row: StudentGradebookRow) -> None:
        self._ref(snapshot_id, row.uid).set({"snapshot_id": snapshot_id, "run_id": run_id, "uid": row.uid, "row": _model_dict(row)})

    def get(self, snapshot_id: str, uid: str) -> Optional[StudentGradebookRow]:
        snap = self._ref(snapshot_id, uid).get()
        if not snap.exists:
            return None
        return StudentGradebookRow.model_validate((snap.to_dict() or {}).get("row") or {})


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

    def create_if_absent(self, appeal: GradeAppeal) -> bool:
        try:
            self._ref(appeal.id).create(_model_dict(appeal))
            return True
        except AlreadyExists:
            return False

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

    def list_expired(self, now) -> list[ExportJob]:
        """Jobs whose downloadable content is past its retention and not yet deleted."""
        query = self._db.collection(EXPORT_JOBS_COLLECTION).where(filter=firestore.FieldFilter("expires_at", "<=", now.isoformat()))
        jobs = [ExportJob.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]
        return [j for j in jobs if j.expired_at is None]


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
