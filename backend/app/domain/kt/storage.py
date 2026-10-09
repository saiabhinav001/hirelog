"""Private object storage for uploaded KT bytes (ADR 0020).

Reuses the evidence storage adapters (`FirebaseStorageAdapter` /
`InMemoryEvidenceStorage`) and the same bucket setting - no second storage
integration. Object names are generated server-side and never derive from the
client's filename. In production a missing bucket is a hard 503: the in-memory
store would lose the bytes on restart and break retry.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.config import settings
from app.domain.proctoring.evidence_storage import EvidenceStorage, FirebaseStorageAdapter, InMemoryEvidenceStorage

QUARANTINE_PREFIX = "kt-quarantine/"

_storage: EvidenceStorage | None = None


class StorageNotConfiguredError(Exception):
    pass


def new_object_key() -> str:
    return f"{QUARANTINE_PREFIX}{uuid4().hex}"


def kt_storage() -> EvidenceStorage:
    global _storage
    if _storage is None:
        if settings.EXAM_EVIDENCE_STORAGE_BUCKET:
            _storage = FirebaseStorageAdapter(bucket_name=settings.EXAM_EVIDENCE_STORAGE_BUCKET)
        elif settings.ENV.strip().lower() == "production":
            raise StorageNotConfiguredError("EXAM_EVIDENCE_STORAGE_BUCKET is required for source uploads in production")
        else:
            _storage = InMemoryEvidenceStorage()
    return _storage
