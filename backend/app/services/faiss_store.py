from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from typing import List, Tuple

import numpy as np

try:
    import faiss
except Exception:  # pragma: no cover - exercised only when FAISS native bindings are unavailable
    faiss = None

from app.core.config import settings

logger = logging.getLogger(__name__)

_INDEX_PATH = settings.faiss_index_path
_MAPPING_PATH = settings.faiss_mapping_path

# Batched persistence: writing the *entire* index + mapping to disk on every
# insertion serializes all submissions behind multi-MB file writes. Instead we
# flush to disk once enough vectors have accumulated OR enough time has elapsed,
# and always flush on interpreter shutdown. Recent unflushed vectors stay in the
# in-memory index (fully searchable); on a hard crash they are recoverable by
# rebuilding from Firestore, so no data is lost — only re-derivable disk state.
_PERSIST_EVERY_N = 50           # flush after this many pending inserts
_PERSIST_MAX_INTERVAL = 30.0    # ...or after this many seconds, whichever first


def _normalize_l2(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


class _NumpyIndexFlatIP:
    """Small fallback index used when FAISS native bindings are unavailable."""

    def __init__(self, dimension: int) -> None:
        self.dimension = int(dimension)
        self._vectors = np.empty((0, self.dimension), dtype="float32")

    @property
    def ntotal(self) -> int:
        return int(self._vectors.shape[0])

    def add(self, vectors: np.ndarray) -> None:
        vectors = np.asarray(vectors, dtype="float32")
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.size == 0:
            return
        self._vectors = np.vstack([self._vectors, vectors])

    def search(self, query: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        query = np.asarray(query, dtype="float32")
        if query.ndim == 1:
            query = query.reshape(1, -1)
        if self.ntotal == 0:
            return (
                np.empty((1, 0), dtype="float32"),
                np.empty((1, 0), dtype="int64"),
            )

        scores = np.dot(self._vectors, query[0])
        top_k = max(1, min(int(k), self.ntotal))
        top_indices = np.argsort(-scores)[:top_k]
        top_scores = scores[top_indices]
        return top_scores.reshape(1, -1).astype("float32"), top_indices.reshape(1, -1).astype("int64")


class FaissStore:
    def __init__(self, dimension: int) -> None:
        self.index_path = _INDEX_PATH
        self.mapping_path = _MAPPING_PATH
        self.dimension = dimension
        self._lock = threading.Lock()
        self._index: faiss.IndexFlatIP | None = None
        self._mapping: List[str] | None = None
        # Batched-persistence bookkeeping (guarded by self._lock).
        self._pending_since_persist = 0
        self._last_persist_ts = time.monotonic()
        atexit.register(self.flush)

    def _ensure_loaded(self) -> None:
        """Lazy-load FAISS index on first use."""
        if self._index is None:
            self._index = self._load_or_create_index()
            self._mapping = self._load_or_create_mapping()
            logger.info(
                "FAISS store initialised — index=%s  vectors=%d",
                self.index_path,
                self._index.ntotal,
            )

    @property
    def index(self) -> faiss.IndexFlatIP:
        self._ensure_loaded()
        return self._index  # type: ignore

    @index.setter
    def index(self, value: faiss.IndexFlatIP) -> None:
        self._index = value

    @property
    def mapping(self) -> List[str]:
        self._ensure_loaded()
        return self._mapping  # type: ignore

    @mapping.setter
    def mapping(self, value: List[str]) -> None:
        self._mapping = value

    def _load_or_create_index(self) -> faiss.IndexFlatIP:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if faiss is None:
            logger.warning("FAISS not available; using numpy similarity fallback index")
            return _NumpyIndexFlatIP(self.dimension)

        if self.index_path.exists():
            return faiss.read_index(str(self.index_path))

        # FAISS is purpose-built for semantic vector search, which is expensive in pure Python.
        # It stays fast by using optimized native (C++/SIMD) routines for similarity computation.
        index = faiss.IndexFlatIP(self.dimension)
        faiss.write_index(index, str(self.index_path))
        return index

    def _load_or_create_mapping(self) -> List[str]:
        self.mapping_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mapping_path.exists():
            with self.mapping_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
                return data.get("mapping", [])
        self._persist_mapping([])
        return []

    def _persist_mapping(self, mapping: List[str]) -> None:
        with self.mapping_path.open("w", encoding="utf-8") as file:
            json.dump({"mapping": mapping}, file)

    def _persist_index(self) -> None:
        if faiss is None:
            return
        faiss.write_index(self.index, str(self.index_path))

    def _flush_locked(self) -> None:
        """Persist index + mapping to disk. Caller must hold self._lock."""
        self._persist_index()
        self._persist_mapping(self.mapping)
        self._pending_since_persist = 0
        self._last_persist_ts = time.monotonic()

    def _should_persist_locked(self) -> bool:
        """Whether accumulated inserts warrant a disk flush. Caller holds lock."""
        return (
            self._pending_since_persist >= _PERSIST_EVERY_N
            or (time.monotonic() - self._last_persist_ts) >= _PERSIST_MAX_INTERVAL
        )

    def flush(self) -> None:
        """Force-persist any unflushed vectors (e.g. on shutdown)."""
        with self._lock:
            if self._index is not None and self._pending_since_persist > 0:
                self._flush_locked()

    def add_vector(self, vector: np.ndarray, doc_id: str) -> int:
        with self._lock:
            vector = np.asarray(vector, dtype="float32").reshape(1, -1)
            vector = _normalize_l2(vector)
            self.index.add(vector)
            self.mapping.append(doc_id)
            # Batched persistence: keep the in-memory add fast and only touch
            # disk once a threshold of pending inserts or elapsed time is hit.
            self._pending_since_persist += 1
            if self._should_persist_locked():
                self._flush_locked()
            return len(self.mapping) - 1

    def search(self, vector: np.ndarray, k: int) -> List[Tuple[str, float]]:
        if self.index.ntotal == 0:
            return []
        with self._lock:
            query = np.asarray(vector, dtype="float32").reshape(1, -1)
            query = _normalize_l2(query)
            scores, indices = self.index.search(query, k)

        results = []
        for idx, score in zip(indices[0].tolist(), scores[0].tolist()):
            if idx == -1 or idx >= len(self.mapping):
                continue
            results.append((self.mapping[idx], float(score)))
        return results

    def rebuild(self, vectors: List[np.ndarray], doc_ids: List[str]) -> None:
        with self._lock:
            # Rebuild keeps vectors contiguous for fast similarity search.
            if faiss is not None:
                self.index = faiss.IndexFlatIP(self.dimension)
            else:
                self.index = _NumpyIndexFlatIP(self.dimension)
            if vectors:
                matrix = np.asarray(vectors, dtype="float32")
                matrix = _normalize_l2(matrix)
                self.index.add(matrix)
            self.mapping = doc_ids
            self._flush_locked()


faiss_store = FaissStore(dimension=settings.EMBEDDING_DIM)
