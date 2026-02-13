from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import List, Tuple

import faiss
import numpy as np

from app.core.config import BASE_DIR, settings

logger = logging.getLogger(__name__)


def _resolve_data_path(raw: str) -> Path:
    """Resolve a data path.

    Absolute paths (e.g. /data/faiss/index.faiss on Render) are used as-is.
    Relative paths are resolved against the backend root dir (local dev).
    """
    p = Path(raw)
    if p.is_absolute():
        return p
    return (BASE_DIR / p).resolve()


class FaissStore:
    def __init__(self, index_path: str, mapping_path: str, dimension: int) -> None:
        self.index_path = _resolve_data_path(index_path)
        self.mapping_path = _resolve_data_path(mapping_path)
        self.dimension = dimension
        self._lock = threading.Lock()
        self.index = self._load_or_create_index()
        self.mapping = self._load_or_create_mapping()
        logger.info(
            "FAISS store initialised — index=%s  vectors=%d",
            self.index_path,
            self.index.ntotal,
        )

    def _load_or_create_index(self) -> faiss.IndexFlatIP:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
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
        faiss.write_index(self.index, str(self.index_path))

    def add_vector(self, vector: np.ndarray, doc_id: str) -> int:
        with self._lock:
            vector = np.asarray(vector, dtype="float32").reshape(1, -1)
            faiss.normalize_L2(vector)
            self.index.add(vector)
            self.mapping.append(doc_id)
            self._persist_index()
            self._persist_mapping(self.mapping)
            return len(self.mapping) - 1

    def search(self, vector: np.ndarray, k: int) -> List[Tuple[str, float]]:
        if self.index.ntotal == 0:
            return []
        with self._lock:
            query = np.asarray(vector, dtype="float32").reshape(1, -1)
            faiss.normalize_L2(query)
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
            self.index = faiss.IndexFlatIP(self.dimension)
            if vectors:
                matrix = np.asarray(vectors, dtype="float32")
                faiss.normalize_L2(matrix)
                self.index.add(matrix)
            self.mapping = doc_ids
            self._persist_index()
            self._persist_mapping(self.mapping)


faiss_store = FaissStore(
    index_path=settings.FAISS_INDEX_PATH,
    mapping_path=settings.FAISS_MAPPING_PATH,
    dimension=settings.EMBEDDING_DIM,
)
