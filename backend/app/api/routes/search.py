from __future__ import annotations

import hashlib
import threading
import time
from typing import Optional

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.firebase import db
from app.services.faiss_store import faiss_store
from app.services.nlp import pipeline
from app.utils.serialization import serialize_doc


router = APIRouter(prefix="/api/search", tags=["search"])


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _normalize_topics(topic: Optional[str]) -> list[str]:
    if not topic:
        return []
    return [value.strip().upper() for value in topic.split(",") if value.strip()]


# ─── In-memory search result cache ────────────────────────────────────────────
_search_cache: dict[str, tuple[float, dict]] = {}
_SEARCH_CACHE_TTL = 120  # 2 minutes
_SEARCH_CACHE_MAX = 100
_search_cache_lock = threading.Lock()


def _cache_key(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode()).hexdigest()


def _get_cached(key: str) -> dict | None:
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry and time.time() - entry[0] < _SEARCH_CACHE_TTL:
            return entry[1]
        _search_cache.pop(key, None)
    return None


def _set_cached(key: str, data: dict) -> None:
    with _search_cache_lock:
        if len(_search_cache) >= _SEARCH_CACHE_MAX:
            oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
            del _search_cache[oldest]
        _search_cache[key] = (time.time(), data)


def _generate_match_explanation(
    data: dict,
    query: str,
    mode: str,
    filter_company: Optional[str],
    filter_topics: list[str],
    filter_difficulty: Optional[str],
    score: Optional[float] = None,
    matched_question: Optional[str] = None,
) -> str:
    """Generate a human-readable explanation for why a result matched."""
    reasons = []
    
    # Question-level match takes priority
    if matched_question:
        reasons.append(f"question matches: \"{matched_question[:80]}\"")
    
    # Semantic similarity explanation
    if mode == "semantic" and query and score:
        if score > 0.7:
            reasons.append(f"highly similar to \"{query}\"")
        elif score > 0.5:
            reasons.append(f"related to \"{query}\"")
        elif not matched_question:
            reasons.append(f"partial match for \"{query}\"")
    
    # Keyword match explanation
    if mode == "keyword" and query:
        reasons.append(f"contains \"{query}\"")
    
    # Topic matches
    data_topics = data.get("topics") or []
    if filter_topics:
        matched_topics = [t for t in filter_topics if t in [x.upper() for x in data_topics]]
        if matched_topics:
            reasons.append(f"covers {', '.join(matched_topics)}")
    elif data_topics and not query:
        reasons.append(f"topics: {', '.join(data_topics[:3])}")
    
    # Company match
    if filter_company and filter_company.lower() in (data.get("company", "")).lower():
        reasons.append(f"from {data.get('company')}")
    
    # Difficulty match
    if filter_difficulty and data.get("difficulty") == filter_difficulty:
        reasons.append(f"{filter_difficulty} difficulty")
    
    if not reasons:
        # Fallback based on available metadata
        company = data.get("company", "")
        if company:
            reasons.append(f"experience at {company}")
        if data_topics:
            reasons.append(f"covers {', '.join(data_topics[:2])}")
    
    return "Matched: " + "; ".join(reasons) if reasons else ""


def _apply_filters(
    data: dict,
    company: Optional[str],
    role: Optional[str],
    year: Optional[int],
    topics: list[str],
    difficulty: Optional[str],
) -> bool:
    if company:
        company_filter = _normalize_text(company)
        company_value = _normalize_text(data.get("company", ""))
        if company_filter not in company_value:
            return False
    if role:
        role_filter = _normalize_text(role)
        role_value = _normalize_text(data.get("role", ""))
        if role_filter not in role_value:
            return False
    if year and data.get("year") != year:
        return False
    if difficulty and (data.get("difficulty") or "").title() != difficulty:
        return False
    if topics:
        data_topics = [topic.upper() for topic in (data.get("topics") or [])]
        if not any(topic in data_topics for topic in topics):
            return False
    return True


from fastapi import Response as FastAPIResponse


@router.get("")
def search(
    q: str = "",
    mode: str = Query("semantic", pattern="^(semantic|keyword)$"),
    company: Optional[str] = None,
    role: Optional[str] = None,
    year: Optional[int] = None,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=50),
    response: FastAPIResponse = None,
) -> dict:
    limit = min(limit, settings.MAX_SEARCH_RESULTS)
    company = company.strip() if company else None
    role = role.strip() if role else None
    topics = _normalize_topics(topic)
    difficulty_normalized = difficulty.strip().title() if difficulty else None

    # CDN / browser cache header — public search data, safe to cache at edge
    if response is not None:
        response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"

    # Check in-memory cache
    ck = _cache_key(q, mode, company, role, year, topic, difficulty_normalized, limit)
    cached = _get_cached(ck)
    if cached is not None:
        return cached

    if mode == "semantic" and q.strip():
        query_vector = pipeline.embed(q)
        candidates = faiss_store.search(query_vector, k=max(limit * 5, 50))
        
        # Batch fetch documents for better performance
        doc_ids = [doc_id for doc_id, _ in candidates]
        score_map = {doc_id: score for doc_id, score in candidates}
        
        if not doc_ids:
            result = {"results": [], "total": 0}
            _set_cached(ck, result)
            return result
        
        # Fetch all documents in batch (Firestore batches up to 30 at a time internally)
        doc_refs = [db.collection("interview_experiences").document(doc_id) for doc_id in doc_ids]
        snapshots = db.get_all(doc_refs)
        
        results = []
        q_lower = q.lower()
        for snapshot in snapshots:
            if not snapshot.exists:
                continue
            data = serialize_doc(snapshot, include_contributor=True)
            # Skip soft-deleted contributions
            if not data.get("is_active", True):
                continue
            if not _apply_filters(data, company, role, year, topics, difficulty_normalized):
                continue
            score = score_map.get(snapshot.id, 0)
            data["score"] = round(score, 4)

            # Check for question-level match to explain why this result matched
            matched_q = None
            for eq in (data.get("extracted_questions") or []):
                eq_text = eq.get("question_text", "") if isinstance(eq, dict) else str(eq)
                if eq_text and q_lower in eq_text.lower():
                    matched_q = eq_text
                    break

            data["match_reason"] = _generate_match_explanation(
                data, q, mode, company, topics, difficulty_normalized, score, matched_q
            )
            results.append(data)
            if len(results) >= limit:
                break
        
        # Sort by score descending (highest similarity first)
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        final = results[:limit]
        for r in final:
            r.pop("raw_text", None)
        result = {"results": final, "total": len(results)}
        _set_cached(ck, result)
        return result

    query = db.collection("interview_experiences")
    if year:
        query = query.where("year", "==", year)
    if difficulty_normalized:
        query = query.where("difficulty", "==", difficulty_normalized)
    if topics:
        if len(topics) == 1:
            query = query.where("topics", "array_contains", topics[0])
        else:
            query = query.where("topics", "array_contains_any", topics[:10])

    snapshots = list(query.stream())
    results = []
    q_lower = q.lower().strip()

    for snapshot in snapshots:
        data = serialize_doc(snapshot, include_contributor=True)

        # Skip soft-deleted contributions
        if not data.get("is_active", True):
            continue

        # Apply company and role filters (not supported by Firestore query)
        if not _apply_filters(data, company, role, None, [], None):
            continue

        if q_lower:
            haystack = " ".join(
                [
                    data.get("company", ""),
                    data.get("role", ""),
                    data.get("summary", ""),
                    data.get("raw_text", ""),
                    " ".join(data.get("topics") or []),
                ]
            ).lower()
            if q_lower not in haystack:
                continue
        data["match_reason"] = _generate_match_explanation(
            data, q, mode, company, topics, difficulty_normalized
        )
        results.append(data)
        if len(results) >= limit:
            break

    for r in results:
        r.pop("raw_text", None)
    result = {"results": results, "total": len(results)}
    _set_cached(ck, result)
    return result
