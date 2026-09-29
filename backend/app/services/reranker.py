from __future__ import annotations

import logging
import threading

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)


class SearchReranker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._disabled = False

    @property
    def enabled(self) -> bool:
        return bool(settings.SEARCH_RERANK_ENABLED) and not self._disabled

    def _ensure_loaded(self) -> None:
        if not self.enabled:
            return

        with self._lock:
            if self._model is not None or self._disabled:
                return

            try:
                from sentence_transformers import CrossEncoder

                # CrossEncoder in sentence-transformers 3.x does not accept a backend kwarg.
                # With torch installed, CPU inference remains stable in production.
                self._model = CrossEncoder(settings.SEARCH_RERANK_MODEL)
                logger.info("Search reranker loaded: %s", settings.SEARCH_RERANK_MODEL)
            except Exception:
                self._disabled = True
                logger.exception("Search reranker unavailable; using base rank only")

    @staticmethod
    def _candidate_text(candidate: dict) -> str:
        question_bits = []
        for question in candidate.get("extracted_questions") or []:
            if isinstance(question, dict):
                text = str(question.get("question_text") or question.get("question") or "").strip()
            else:
                text = str(question).strip()
            if text:
                question_bits.append(text)
            if len(question_bits) >= 8:
                break

        return " ".join(
            value
            for value in [
                str(candidate.get("company") or ""),
                str(candidate.get("role") or ""),
                str(candidate.get("round") or ""),
                str(candidate.get("difficulty") or ""),
                str(candidate.get("summary") or ""),
                " ".join(candidate.get("topics") or []),
                " ".join(question_bits),
            ]
            if value
        )[:1600]

    def rerank(self, query: str, candidates: list[dict]) -> dict[str, float]:
        if not query or not candidates or not self.enabled:
            return {}

        self._ensure_loaded()
        if self._model is None:
            return {}

        top_k = max(1, int(settings.SEARCH_RERANK_TOP_K))
        subset = candidates[:top_k]

        pairs = []
        ids = []
        for candidate in subset:
            doc_id = str(candidate.get("id") or "").strip()
            if not doc_id:
                continue
            text = self._candidate_text(candidate)
            if not text:
                continue
            ids.append(doc_id)
            pairs.append((query, text))

        if not pairs:
            return {}

        try:
            raw_scores = self._model.predict(pairs)
        except Exception:
            logger.exception("Search reranker inference failed; using base rank only")
            return {}

        scores = np.asarray(raw_scores, dtype="float32")
        if scores.size == 0:
            return {}

        minimum = float(scores.min())
        maximum = float(scores.max())
        if abs(maximum - minimum) < 1e-9:
            normalized = np.full_like(scores, fill_value=0.5)
        else:
            normalized = (scores - minimum) / (maximum - minimum)

        return {doc_id: float(score) for doc_id, score in zip(ids, normalized.tolist())}


search_reranker = SearchReranker()
