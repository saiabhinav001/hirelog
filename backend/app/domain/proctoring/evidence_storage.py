"""Evidence storage adapter. `FirebaseStorageAdapter` is a genuine, complete
implementation using `firebase_admin.storage` (already a core part of the
`firebase-admin` dependency already pinned in requirements.txt — no new
package) — it would work against a real configured bucket, but has NOT been
exercised against one in this environment (no bucket is provisioned here).
`InMemoryEvidenceStorage` is the real, working default for tests/dev: it
genuinely stores and returns bytes, it does not merely pretend to.
"""

from __future__ import annotations

from typing import Optional, Protocol


class EvidenceStorage(Protocol):
    def put(self, ref: str, content: bytes, *, content_type: str) -> None: ...

    def get(self, ref: str) -> Optional[bytes]: ...

    def delete(self, ref: str) -> None: ...

    def exists(self, ref: str) -> bool: ...


class InMemoryEvidenceStorage:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, ref: str, content: bytes, *, content_type: str) -> None:
        self._objects[ref] = content

    def get(self, ref: str) -> Optional[bytes]:
        return self._objects.get(ref)

    def delete(self, ref: str) -> None:
        self._objects.pop(ref, None)

    def exists(self, ref: str) -> bool:
        return ref in self._objects


class FirebaseStorageAdapter:
    """Real adapter over `firebase_admin.storage.bucket()`. Requires
    `storageBucket` to have been passed when the Firebase app was
    initialized (see `app.core.firebase`) — raises clearly if not, rather
    than silently writing nowhere."""

    def __init__(self, *, bucket_name: Optional[str] = None) -> None:
        self._bucket_name = bucket_name

    def _bucket(self):
        from firebase_admin import storage

        return storage.bucket(self._bucket_name) if self._bucket_name else storage.bucket()

    def put(self, ref: str, content: bytes, *, content_type: str) -> None:
        blob = self._bucket().blob(ref)
        blob.upload_from_string(content, content_type=content_type)

    def get(self, ref: str) -> Optional[bytes]:
        blob = self._bucket().blob(ref)
        if not blob.exists():
            return None
        return blob.download_as_bytes()

    def delete(self, ref: str) -> None:
        blob = self._bucket().blob(ref)
        if blob.exists():
            blob.delete()

    def exists(self, ref: str) -> bool:
        return self._bucket().blob(ref).exists()
