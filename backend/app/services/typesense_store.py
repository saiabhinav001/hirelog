from __future__ import annotations

import importlib
import logging
import threading
from typing import Any

from app.core.config import settings

try:
    typesense = importlib.import_module("typesense")
except Exception:  # pragma: no cover - optional dependency in some local setups
    typesense = None

logger = logging.getLogger(__name__)


class TypesenseStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client = None
        self._collection_ready = False

    @property
    def enabled(self) -> bool:
        wants_typesense = settings.SEARCH_ENGINE.strip().lower() == "typesense" or settings.TYPESENSE_ENABLED
        has_deps = typesense is not None
        has_api_key = bool(settings.TYPESENSE_API_KEY)
        return wants_typesense and has_deps and has_api_key

    @property
    def collection_name(self) -> str:
        return settings.TYPESENSE_COLLECTION

    def _build_client(self):
        if typesense is None:
            raise RuntimeError("typesense dependency is not installed")
        if not settings.TYPESENSE_API_KEY:
            raise RuntimeError("TYPESENSE_API_KEY is required when Typesense is enabled")

        return typesense.Client(
            {
                "nodes": [
                    {
                        "host": settings.TYPESENSE_HOST,
                        "port": settings.TYPESENSE_PORT,
                        "protocol": settings.TYPESENSE_PROTOCOL,
                    }
                ],
                "api_key": settings.TYPESENSE_API_KEY,
                "connection_timeout_seconds": 2,
            }
        )

    def client(self):
        if not self.enabled:
            return None
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def ensure_collection(self) -> None:
        if not self.enabled:
            return

        with self._lock:
            if self._collection_ready:
                return

            client = self.client()
            if client is None:
                return

            try:
                client.collections[self.collection_name].retrieve()
                self._collection_ready = True
                return
            except Exception:
                pass

            schema = {
                "name": self.collection_name,
                "default_sorting_field": "created_at",
                "fields": [
                    {"name": "id", "type": "string"},
                    {"name": "company", "type": "string", "infix": True},
                    {"name": "role", "type": "string", "infix": True},
                    {"name": "year", "type": "int32", "facet": True},
                    {"name": "round", "type": "string"},
                    {"name": "topics", "type": "string[]", "facet": True},
                    {"name": "difficulty", "type": "string", "facet": True},
                    {"name": "summary", "type": "string", "optional": True},
                    {"name": "question_text", "type": "string", "optional": True},
                    {"name": "search_terms", "type": "string[]", "optional": True},
                    {"name": "raw_text", "type": "string", "optional": True},
                    {"name": "text_blob", "type": "string", "optional": True},
                    {"name": "is_active", "type": "bool", "facet": True},
                    {"name": "is_anonymous", "type": "bool", "facet": True},
                    {"name": "show_name", "type": "bool", "facet": True},
                    {"name": "contributor_visibility", "type": "string", "facet": True},
                    {"name": "allow_contact", "type": "bool", "facet": True},
                    {"name": "nlp_status", "type": "string", "facet": True},
                    {"name": "has_user_questions", "type": "bool", "facet": True},
                    {"name": "has_ai_questions", "type": "bool", "facet": True},
                    {"name": "created_at", "type": "int64"},
                    {
                        "name": "embedding",
                        "type": "float[]",
                        "num_dim": settings.EMBEDDING_DIM,
                        "optional": True,
                    },
                ],
            }

            client.collections.create(schema)
            self._collection_ready = True
            logger.info("Typesense collection created: %s", self.collection_name)

    def upsert_document(self, doc: dict) -> None:
        if not self.enabled:
            return

        self.ensure_collection()
        client = self.client()
        if client is None:
            return

        client.collections[self.collection_name].documents.upsert(doc)

    def delete_document(self, doc_id: str) -> None:
        if not self.enabled:
            return

        self.ensure_collection()
        client = self.client()
        if client is None:
            return

        try:
            client.collections[self.collection_name].documents[doc_id].delete()
        except Exception:
            # Delete should be idempotent for missing docs.
            pass

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        escaped = value.replace("`", "\\`")
        return f"`{escaped}`"

    def search_hybrid(
        self,
        *,
        query: str,
        offset: int,
        limit: int,
        company: str | None,
        role: str | None,
        year: int | None,
        topics: list[str],
        difficulty: str | None,
        query_vector: list[float] | None,
        alpha: float,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        self.ensure_collection()
        client = self.client()
        if client is None:
            return None

        skip = max(offset % max(limit, 1), 0)
        page = (max(offset, 0) // max(limit, 1)) + 1
        per_page = min(250, max(limit + skip, 1))

        filter_parts = ["is_active:=true"]
        if year is not None:
            filter_parts.append(f"year:={year}")
        if difficulty:
            filter_parts.append(f"difficulty:={self._escape_filter_value(difficulty)}")
        if company:
            filter_parts.append(f"company:={self._escape_filter_value(company)}")
        if role:
            filter_parts.append(f"role:={self._escape_filter_value(role)}")
        if topics:
            topic_values = ",".join(self._escape_filter_value(topic) for topic in topics[:10])
            filter_parts.append(f"topics:=[{topic_values}]")

        params: dict[str, Any] = {
            "q": query if query else "*",
            "query_by": "company,role,round,difficulty,topics,summary,search_terms,question_text,raw_text,text_blob",
            "per_page": per_page,
            "page": page,
            "filter_by": " && ".join(filter_parts),
            "sort_by": "_text_match:desc,created_at:desc",
            "num_typos": 2,
            "drop_tokens_threshold": 1,
            "prefix": False,
        }

        if query and query_vector:
            vector = ",".join(f"{float(value):.6f}" for value in query_vector)
            k = max((offset + limit) * 6, 80)
            params["vector_query"] = f"embedding:([{vector}], k:{k}, alpha:{alpha:.2f})"

        response = client.collections[self.collection_name].documents.search(params)
        hits = response.get("hits") or []

        sliced_hits = hits[skip:skip + limit]
        doc_ids: list[str] = []
        score_map: dict[str, float] = {}

        for hit in sliced_hits:
            document = hit.get("document") or {}
            doc_id = str(document.get("id") or "").strip()
            if not doc_id:
                continue
            doc_ids.append(doc_id)

            vector_distance = hit.get("vector_distance")
            text_match = hit.get("text_match")
            if isinstance(vector_distance, (int, float)):
                score = max(0.0, 1.0 - float(vector_distance))
            elif isinstance(text_match, (int, float)):
                score = min(float(text_match) / 1000.0, 1.0)
            else:
                score = 0.0
            score_map[doc_id] = float(score)

        return {
            "doc_ids": doc_ids,
            "score_map": score_map,
            "total_count": int(response.get("found") or 0),
        }


# Shared singleton for API routes and background indexing workers.
typesense_store = TypesenseStore()
