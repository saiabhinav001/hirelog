"""Firestore repositories for KT ingestion. Private collections — covered by
the default-deny catch-all in firestore.rules (Admin-SDK-only), same as the
exam collections (see docs/adr/0008 pattern)."""

from __future__ import annotations

from typing import Optional

from app.domain.kt.models import ApprovedSource, ExtractedVersion, UploadedSource

UPLOADS_COLLECTION = "kt_uploads"
EXTRACTED_VERSIONS_SUBCOLLECTION = "extracted_versions"
APPROVED_SOURCES_COLLECTION = "kt_approved_sources"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class UploadedSourceRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, upload_id: str):
        return self._db.collection(UPLOADS_COLLECTION).document(upload_id)

    def get(self, upload_id: str) -> Optional[UploadedSource]:
        snap = self._ref(upload_id).get()
        if not snap.exists:
            return None
        return UploadedSource.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def create(self, upload: UploadedSource) -> None:
        self._ref(upload.id).set(_model_dict(upload))

    def update_status(self, upload_id: str, *, status, reason: Optional[str], now) -> None:
        update = {"status": status.value, "updated_at": now.isoformat()}
        if reason is not None:
            update["rejection_reason"] = reason
        self._ref(upload_id).update(update)

    def _versions(self, upload_id: str):
        return self._ref(upload_id).collection(EXTRACTED_VERSIONS_SUBCOLLECTION)

    def create_extracted_version(self, version: ExtractedVersion) -> None:
        self._versions(version.source_id).document(version.id).set(_model_dict(version))

    def get_extracted_version(self, upload_id: str, version_id: str) -> Optional[ExtractedVersion]:
        snap = self._versions(upload_id).document(version_id).get()
        if not snap.exists:
            return None
        return ExtractedVersion.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def latest_version_number(self, upload_id: str) -> int:
        docs = list(self._versions(upload_id).stream())
        if not docs:
            return 0
        return max(int((d.to_dict() or {}).get("version_number", 0)) for d in docs)


class ApprovedSourceRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, source_id: str):
        return self._db.collection(APPROVED_SOURCES_COLLECTION).document(source_id)

    def get(self, source_id: str) -> Optional[ApprovedSource]:
        snap = self._ref(source_id).get()
        if not snap.exists:
            return None
        return ApprovedSource.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def create(self, source: ApprovedSource) -> None:
        self._ref(source.id).set(_model_dict(source))

    def save(self, source: ApprovedSource) -> None:
        self._ref(source.id).set(_model_dict(source), merge=True)
