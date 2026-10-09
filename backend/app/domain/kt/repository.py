"""Firestore repositories for KT ingestion. Private collections — covered by
the default-deny catch-all in firestore.rules (Admin-SDK-only), same as the
exam collections (see docs/adr/0008 pattern)."""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists

from app.domain.kt.models import ApprovedSource, ExtractedAnchor, ExtractedVersion, UploadedSource

UPLOADS_COLLECTION = "kt_uploads"
EXTRACTED_VERSIONS_SUBCOLLECTION = "extracted_versions"
ANCHOR_CHUNKS_SUBCOLLECTION = "anchor_chunks"
APPROVED_SOURCES_COLLECTION = "kt_approved_sources"
ANCHOR_CHUNK_BYTES = 600_000  # comfortably under Firestore's 1 MiB document limit


def _split_anchor_chunks(anchors: list[ExtractedAnchor]) -> list[list[ExtractedAnchor]]:
    chunks: list[list[ExtractedAnchor]] = []
    current: list[ExtractedAnchor] = []
    size = 0
    for anchor in anchors:
        weight = len(anchor.text.encode("utf-8")) + 300  # text + ids/keys overhead
        if current and size + weight > ANCHOR_CHUNK_BYTES:
            chunks.append(current)
            current, size = [], 0
        current.append(anchor)
        size += weight
    if current:
        chunks.append(current)
    return chunks


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

    def create_in_transaction(self, transaction, upload: UploadedSource) -> None:
        transaction.create(self._ref(upload.id), _model_dict(upload))

    def get_in_transaction(self, transaction, upload_id: str) -> Optional[UploadedSource]:
        snap = self._ref(upload_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return UploadedSource.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update_in_transaction(self, transaction, upload_id: str, update: dict) -> None:
        transaction.update(self._ref(upload_id), update)

    def update(self, upload_id: str, update: dict) -> None:
        self._ref(upload_id).update(update)

    def list_by_owner(self, owner_uid: str) -> list[UploadedSource]:
        query = self._db.collection(UPLOADS_COLLECTION).where(
            filter=firestore.FieldFilter("uploaded_by", "==", owner_uid)
        )
        return [UploadedSource.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def find_by_sha256(self, sha256: str) -> list[UploadedSource]:
        query = self._db.collection(UPLOADS_COLLECTION).where(filter=firestore.FieldFilter("sha256", "==", sha256))
        return [UploadedSource.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def update_status(self, upload_id: str, *, status, reason: Optional[str], now) -> None:
        update = {"status": status.value, "updated_at": now.isoformat()}
        if reason is not None:
            update["rejection_reason"] = reason
        self._ref(upload_id).update(update)

    def _versions(self, upload_id: str):
        return self._ref(upload_id).collection(EXTRACTED_VERSIONS_SUBCOLLECTION)

    # An extracted version can be up to MAX_EXTRACTED_TEXT_BYTES, far over Firestore's 1 MiB document
    # limit, so the anchors live in `anchor_chunks` and the version document (written LAST, create-only)
    # is the commit marker: a version without its document does not exist. Legacy versions keep their
    # anchors inline (chunk_count == 0) and read as before.
    def _chunks(self, upload_id: str, version_id: str):
        return self._versions(upload_id).document(version_id).collection(ANCHOR_CHUNKS_SUBCOLLECTION)

    def create_extracted_version(self, version: ExtractedVersion) -> None:
        self._versions(version.source_id).document(version.id).set(_model_dict(version))

    def create_extracted_version_once(self, version: ExtractedVersion) -> bool:
        """False when this version already exists (a redelivered job) - nothing more is written."""
        ref = self._versions(version.source_id).document(version.id)
        if ref.get().exists:
            return False
        chunks = _split_anchor_chunks(version.anchors)
        for index, chunk in enumerate(chunks):
            self._chunks(version.source_id, version.id).document(f"{index:05d}").set(
                {"index": index, "anchors": [a.model_dump(mode="json") for a in chunk]}
            )
        header = version.model_copy(update={"anchors": [], "anchor_count": len(version.anchors), "chunk_count": len(chunks)})
        try:
            ref.create(_model_dict(header))
        except AlreadyExists:
            return False
        return True

    def _hydrate(self, upload_id: str, version: ExtractedVersion) -> ExtractedVersion:
        if not version.chunk_count:
            return version
        anchors = []
        docs = sorted(self._chunks(upload_id, version.id).stream(), key=lambda d: int((d.to_dict() or {}).get("index", 0)))
        for doc in docs:
            anchors.extend(ExtractedAnchor.model_validate(a) for a in (doc.to_dict() or {}).get("anchors", []))
        return version.model_copy(update={"anchors": anchors})

    def list_extracted_versions(self, upload_id: str, *, include_anchors: bool = False) -> list[ExtractedVersion]:
        versions = [
            ExtractedVersion.model_validate({**(d.to_dict() or {}), "id": d.id})
            for d in self._versions(upload_id).stream()
        ]
        versions.sort(key=lambda v: v.version_number)
        return [self._hydrate(upload_id, v) for v in versions] if include_anchors else versions

    def delete_extracted_versions(self, upload_id: str) -> int:
        count = 0
        for doc in self._versions(upload_id).stream():
            for chunk in self._chunks(upload_id, doc.id).stream():
                chunk.reference.delete()
            doc.reference.delete()
            count += 1
        return count

    def get_extracted_version(self, upload_id: str, version_id: str) -> Optional[ExtractedVersion]:
        snap = self._versions(upload_id).document(version_id).get()
        if not snap.exists:
            return None
        return self._hydrate(upload_id, ExtractedVersion.model_validate({**(snap.to_dict() or {}), "id": snap.id}))
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

    def list_by_upload(self, upload_id: str) -> list[ApprovedSource]:
        query = self._db.collection(APPROVED_SOURCES_COLLECTION).where(
            filter=firestore.FieldFilter("upload_id", "==", upload_id)
        )
        return [ApprovedSource.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def list_all(self) -> list[ApprovedSource]:
        """KT/source review screen data. Sources are a shared institutional
        registry, not scoped to one uploader — any faculty member reviews
        any pending source."""
        return [
            ApprovedSource.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            for doc in self._db.collection(APPROVED_SOURCES_COLLECTION).stream()
        ]
