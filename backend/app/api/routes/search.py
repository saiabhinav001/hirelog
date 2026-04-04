from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi import Response as FastAPIResponse
from firebase_admin import firestore

from app.core.config import settings
from app.core.firebase import db
from app.core.rate_limit import SlidingWindowLimiter, client_identifier
from app.services.faiss_store import faiss_store
from app.services.index_queue import search_index_queue
from app.services.nlp import pipeline
from app.services.reranker import search_reranker
from app.services.search_cache import search_cache
from app.services.search_core import (
    bm25_score_documents,
    build_document_terms,
    expand_query_terms,
    expand_query_text,
    keyword_score,
    normalize_text,
    tokenize_terms,
)
from app.services.typesense_store import typesense_store
from app.utils.serialization import serialize_doc


router = APIRouter(prefix="/api/search", tags=["search"])
logger = logging.getLogger(__name__)


def _normalize_topics(topic: Optional[str]) -> list[str]:
    if not topic:
        return []
    return [value.strip().upper() for value in topic.split(",") if value.strip()]


def _cache_key(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.md5(raw.encode()).hexdigest()
    return f"search:{digest}"


def _get_cached(key: str) -> dict | None:
    return search_cache.get(key)


def _set_cached(key: str, data: dict) -> None:
    search_cache.set(key, data)


_query_vector_cache: dict[str, tuple[float, object]] = {}
_QUERY_VECTOR_CACHE_TTL = 600
_QUERY_VECTOR_CACHE_MAX = 256
_query_vector_cache_lock = threading.Lock()
_semantic_slots = threading.BoundedSemaphore(max(1, settings.SEARCH_SEMANTIC_MAX_CONCURRENCY))
_search_limiter = SlidingWindowLimiter(settings.SEARCH_RATE_LIMIT_PER_MINUTE, 60)

_search_metrics_lock = threading.Lock()
_search_mode_counts: Counter[str] = Counter()
_search_fallback_counts: Counter[str] = Counter()
_search_latencies_ms: deque[float] = deque(maxlen=600)
_search_requests_total = 0
_search_cache_hits = 0
_semantic_requested_total = 0
_semantic_success_total = 0
_semantic_failure_events: deque[float] = deque()
_semantic_cooldown_until = 0.0
_search_query_counts: Counter[str] = Counter()
_search_zero_result_counts: Counter[str] = Counter()
_search_filter_usage_counts: Counter[str] = Counter()
_search_long_latency_counts: Counter[str] = Counter()

_facet_cache_lock = threading.Lock()
_facet_cache: dict | None = None
_facet_cache_ts = 0.0
_FACET_CACHE_TTL_SECONDS = 300


def _bounded_counter_increment(counter: Counter[str], key: str, *, max_keys: int) -> None:
    if not key:
        return

    counter[key] += 1
    if len(counter) <= max_keys:
        return

    # Keep the highest-signal keys while preventing unbounded growth.
    for extra_key, _ in counter.most_common()[max_keys:]:
        counter.pop(extra_key, None)


def _extract_filter_keys(
    *,
    company: Optional[str],
    role: Optional[str],
    year: Optional[int],
    topics: list[str],
    difficulty: Optional[str],
) -> list[str]:
    keys: list[str] = []
    if company:
        keys.append("company")
    if role:
        keys.append("role")
    if year is not None:
        keys.append("year")
    if topics:
        keys.append("topics")
    if difficulty:
        keys.append("difficulty")
    return keys


def _get_or_embed_query_vector(normalized_query: str):
    now = time.time()
    with _query_vector_cache_lock:
        entry = _query_vector_cache.get(normalized_query)
        if entry and (now - entry[0]) < _QUERY_VECTOR_CACHE_TTL:
            return entry[1]
        _query_vector_cache.pop(normalized_query, None)

    vector = pipeline.embed(normalized_query)

    with _query_vector_cache_lock:
        if len(_query_vector_cache) >= _QUERY_VECTOR_CACHE_MAX:
            oldest_key = min(_query_vector_cache, key=lambda key: _query_vector_cache[key][0])
            _query_vector_cache.pop(oldest_key, None)
        _query_vector_cache[normalized_query] = (now, vector)

    return vector


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (percentile / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _trim_semantic_failures(now: float) -> None:
    cutoff = now - settings.SEARCH_SEMANTIC_FAILURE_WINDOW_SECONDS
    while _semantic_failure_events and _semantic_failure_events[0] < cutoff:
        _semantic_failure_events.popleft()


def _register_semantic_failure() -> None:
    global _semantic_cooldown_until
    now = time.time()
    with _search_metrics_lock:
        _semantic_failure_events.append(now)
        _trim_semantic_failures(now)
        if len(_semantic_failure_events) >= settings.SEARCH_SEMANTIC_FAILURE_THRESHOLD:
            _semantic_cooldown_until = max(
                _semantic_cooldown_until,
                now + settings.SEARCH_SEMANTIC_COOLDOWN_SECONDS,
            )


def _semantic_circuit_open() -> bool:
    global _semantic_cooldown_until
    now = time.time()
    with _search_metrics_lock:
        _trim_semantic_failures(now)
        if _semantic_cooldown_until and now >= _semantic_cooldown_until:
            _semantic_cooldown_until = 0.0
        return now < _semantic_cooldown_until


def _record_search_observation(
    *,
    served_mode: str,
    latency_ms: float,
    cache_hit: bool,
    semantic_requested: bool,
    fallback_reason: str | None,
    query_text: str,
    total_count: int,
    filter_keys: list[str],
) -> None:
    global _search_requests_total, _search_cache_hits, _semantic_requested_total, _semantic_success_total

    with _search_metrics_lock:
        _search_requests_total += 1
        _search_mode_counts[served_mode] += 1
        _search_latencies_ms.append(latency_ms)

        if cache_hit:
            _search_cache_hits += 1
        if semantic_requested:
            _semantic_requested_total += 1
        if served_mode in {"semantic", "hybrid"}:
            _semantic_success_total += 1
        if fallback_reason:
            _search_fallback_counts[fallback_reason] += 1

        if query_text:
            query_key = query_text[:120]
            _bounded_counter_increment(_search_query_counts, query_key, max_keys=500)
            if total_count <= 0:
                _bounded_counter_increment(_search_zero_result_counts, query_key, max_keys=500)
            if latency_ms >= float(settings.SEARCH_LONG_LATENCY_MS):
                _bounded_counter_increment(_search_long_latency_counts, query_key, max_keys=500)

        for filter_key in filter_keys:
            _bounded_counter_increment(_search_filter_usage_counts, filter_key, max_keys=64)


def get_search_runtime_snapshot() -> dict:
    with _search_metrics_lock:
        now = time.time()
        _trim_semantic_failures(now)
        cooldown_remaining_ms = int(max(0.0, _semantic_cooldown_until - now) * 1000)

        latencies = sorted(_search_latencies_ms)
        request_total = max(_search_requests_total, 1)
        semantic_requested = max(_semantic_requested_total, 1)
        zero_result_total = sum(_search_zero_result_counts.values())

        top_n = max(1, int(settings.SEARCH_ANALYTICS_TOP_QUERIES))

        return {
            "requests_total": _search_requests_total,
            "cache_hits": _search_cache_hits,
            "cache_hit_rate_percent": round((_search_cache_hits / request_total) * 100, 2),
            "cache_backend": search_cache.backend,
            "semantic_requested_total": _semantic_requested_total,
            "semantic_success_total": _semantic_success_total,
            "semantic_success_rate_percent": round((_semantic_success_total / semantic_requested) * 100, 2),
            "mode_counts": dict(_search_mode_counts),
            "fallback_counts": dict(_search_fallback_counts),
            "latency_ms": {
                "avg": round((sum(latencies) / len(latencies)), 2) if latencies else 0.0,
                "p50": round(_percentile(latencies, 50), 2) if latencies else 0.0,
                "p95": round(_percentile(latencies, 95), 2) if latencies else 0.0,
                "p99": round(_percentile(latencies, 99), 2) if latencies else 0.0,
            },
            "semantic_circuit": {
                "open": cooldown_remaining_ms > 0,
                "cooldown_remaining_ms": cooldown_remaining_ms,
                "recent_failure_count": len(_semantic_failure_events),
                "failure_window_seconds": settings.SEARCH_SEMANTIC_FAILURE_WINDOW_SECONDS,
                "failure_threshold": settings.SEARCH_SEMANTIC_FAILURE_THRESHOLD,
            },
            "query_analytics": {
                "tracked_queries": int(sum(_search_query_counts.values())),
                "zero_result_queries": int(zero_result_total),
                "zero_result_rate_percent": round((zero_result_total / request_total) * 100, 2),
                "long_latency_threshold_ms": float(settings.SEARCH_LONG_LATENCY_MS),
                "long_latency_queries": int(sum(_search_long_latency_counts.values())),
                "top_queries": dict(_search_query_counts.most_common(top_n)),
                "top_zero_result_queries": dict(_search_zero_result_counts.most_common(top_n)),
                "top_long_latency_queries": dict(_search_long_latency_counts.most_common(top_n)),
                "filter_usage": dict(_search_filter_usage_counts.most_common(12)),
            },
            "index_queue": search_index_queue.status(),
        }


def reset_search_runtime_snapshot() -> dict:
    global _search_requests_total, _search_cache_hits, _semantic_requested_total, _semantic_success_total, _semantic_cooldown_until

    with _search_metrics_lock:
        _search_mode_counts.clear()
        _search_fallback_counts.clear()
        _search_latencies_ms.clear()
        _search_requests_total = 0
        _search_cache_hits = 0
        _semantic_requested_total = 0
        _semantic_success_total = 0
        _semantic_failure_events.clear()
        _semantic_cooldown_until = 0.0
        _search_query_counts.clear()
        _search_zero_result_counts.clear()
        _search_filter_usage_counts.clear()
        _search_long_latency_counts.clear()

    return get_search_runtime_snapshot()


def clear_search_caches() -> dict:
    cache_result = search_cache.clear()
    with _query_vector_cache_lock:
        cleared = len(_query_vector_cache)
        _query_vector_cache.clear()

    return {
        "status": "cleared",
        "cache": cache_result,
        "search_cache_entries": 0,
        "query_vector_cache_entries": 0,
        "query_vector_entries_cleared": cleared,
    }


def trigger_search_warmup() -> dict:
    warmup_search_runtime()

    with _query_vector_cache_lock:
        vector_cache_size = len(_query_vector_cache)

    return {
        "status": "warmed",
        "query_vector_cache_entries": vector_cache_size,
    }


def warmup_search_runtime() -> None:
    """Warm up FAISS + embedding cache in the background on startup."""
    if not settings.SEARCH_ENABLE_WARMUP:
        return

    try:
        _ = faiss_store.index.ntotal
    except Exception:
        logger.exception("Search warmup could not load FAISS index")

    for query in settings.search_warmup_queries_list():
        expanded = expand_query_text(normalize_text(query))
        if not expanded:
            continue
        try:
            _get_or_embed_query_vector(expanded)
        except Exception:
            logger.exception("Search warmup failed for query: %s", query)


@router.get("/facets")
def search_facets() -> dict:
    """Return precomputed facets and trending filters for search UI chips."""
    return get_precomputed_facets()


def _generate_match_explanation(
    data: dict,
    query: str,
    mode: str,
    filter_company: Optional[str],
    filter_topics: list[str],
    filter_difficulty: Optional[str],
    semantic_score: Optional[float] = None,
    lexical_score: Optional[float] = None,
    matched_question: Optional[str] = None,
) -> str:
    """Generate a human-readable explanation for why a result matched."""
    reasons = []

    if matched_question:
        reasons.append(f"question matches: \"{matched_question[:80]}\"")

    if mode in {"semantic", "hybrid"} and query and semantic_score is not None:
        if semantic_score > 0.7:
            reasons.append(f"highly similar to \"{query}\"")
        elif semantic_score > 0.5:
            reasons.append(f"related to \"{query}\"")
        elif not matched_question:
            reasons.append(f"partial match for \"{query}\"")

    if query and lexical_score and lexical_score > 0:
        reasons.append(f"contains \"{query}\"")

    data_topics = data.get("topics") or []
    if filter_topics:
        matched_topics = [t for t in filter_topics if t in [x.upper() for x in data_topics]]
        if matched_topics:
            reasons.append(f"covers {', '.join(matched_topics)}")
    elif data_topics and not query:
        reasons.append(f"topics: {', '.join(data_topics[:3])}")

    if filter_company and filter_company.lower() in (data.get("company", "")).lower():
        reasons.append(f"from {data.get('company')}")

    if filter_difficulty and data.get("difficulty") == filter_difficulty:
        reasons.append(f"{filter_difficulty} difficulty")

    if not reasons:
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
        company_filter = normalize_text(company)
        company_value = normalize_text(data.get("company", ""))
        if company_filter not in company_value:
            return False
    if role:
        role_filter = normalize_text(role)
        role_value = normalize_text(data.get("role", ""))
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


def _collect_limited_snapshots(query, *, scan_limit: int | None = None) -> list:
    snapshots = []
    for snapshot in query.stream():
        snapshots.append(snapshot)
        if scan_limit is not None and len(snapshots) >= scan_limit:
            break
    return snapshots


def _collect_keyword_snapshots(
    *,
    normalized_query: str,
    query_terms: list[str],
    year: Optional[int],
    topics: list[str],
    difficulty_normalized: Optional[str],
) -> list:
    base_query = db.collection("interview_experiences").where(
        filter=firestore.FieldFilter("is_active", "==", True)
    )
    if year:
        base_query = base_query.where(filter=firestore.FieldFilter("year", "==", year))
    if difficulty_normalized:
        base_query = base_query.where(
            filter=firestore.FieldFilter("difficulty", "==", difficulty_normalized)
        )
    if topics:
        if len(topics) == 1:
            base_query = base_query.where(
                filter=firestore.FieldFilter("topics", "array_contains", topics[0])
            )
        else:
            base_query = base_query.where(
                filter=firestore.FieldFilter("topics", "array_contains_any", topics[:10])
            )

    scan_limit = max(int(settings.SEARCH_KEYWORD_SCAN_LIMIT), 1)
    keyword_prefilter_terms = [term for term in query_terms if len(term) >= 3][:10]
    can_apply_keyword_prefilter = bool(normalized_query and keyword_prefilter_terms and not topics)

    if can_apply_keyword_prefilter:
        try:
            prefiltered_query = base_query.where(
                filter=firestore.FieldFilter("search_terms", "array_contains_any", keyword_prefilter_terms)
            )
            return _collect_limited_snapshots(prefiltered_query, scan_limit=scan_limit)
        except Exception:
            logger.exception("Keyword prefilter query failed; falling back to standard keyword scan")

    return _collect_limited_snapshots(base_query, scan_limit=scan_limit)


def _build_lexical_rows_from_snapshots(
    snapshots: list,
    *,
    normalized_query: str,
    query_terms: list[str],
    company: Optional[str],
    role: Optional[str],
    year: Optional[int],
    topics: list[str],
    difficulty_normalized: Optional[str],
) -> tuple[list[dict], dict[str, float]]:
    staged_rows: list[dict] = []
    doc_terms: dict[str, list[str]] = {}

    for snapshot in snapshots:
        data = serialize_doc(snapshot, include_contributor=True)
        if not data.get("is_active", True):
            continue
        if not _apply_filters(data, company, role, year, topics, difficulty_normalized):
            continue

        keyword_value, matched_q = keyword_score(data, normalized_query, query_terms)
        staged_rows.append(
            {
                "doc_id": snapshot.id,
                "data": data,
                "keyword_score": float(keyword_value),
                "matched_question": matched_q,
            }
        )
        doc_terms[snapshot.id] = build_document_terms(data)

    bm25_scores = bm25_score_documents(query_terms, doc_terms) if normalized_query else {}
    lexical_rows: list[dict] = []
    lexical_scores: dict[str, float] = {}

    for row in staged_rows:
        doc_id = row["doc_id"]
        keyword_value = float(row["keyword_score"])
        bm25_value = float(bm25_scores.get(doc_id, 0.0))
        lexical_value = ((0.65 * bm25_value) + (0.35 * keyword_value)) if normalized_query else keyword_value

        if normalized_query and lexical_value <= 0:
            continue

        row["lexical_score"] = lexical_value
        lexical_scores[doc_id] = lexical_value
        lexical_rows.append(row)

    if normalized_query:
        lexical_rows.sort(key=lambda item: float(item.get("lexical_score") or 0.0), reverse=True)
    else:
        lexical_rows.sort(key=lambda item: str(item["data"].get("created_at") or ""), reverse=True)

    return lexical_rows, lexical_scores


def _keyword_response_from_rows(
    rows: list[dict],
    *,
    normalized_query: str,
    company: Optional[str],
    topics: list[str],
    difficulty_normalized: Optional[str],
    offset: int,
    limit: int,
) -> dict:
    results = []
    for row in rows:
        data = row["data"]
        lexical = float(row.get("lexical_score") or row.get("keyword_score") or 0.0)
        data["score"] = round(lexical, 4)
        data["match_reason"] = _generate_match_explanation(
            data,
            normalized_query,
            "keyword",
            company,
            topics,
            difficulty_normalized,
            None,
            lexical,
            row.get("matched_question"),
        )
        results.append(data)

    if normalized_query:
        results = _apply_rerank(results, normalized_query)

    total_count = len(results)
    final = results[offset:offset + limit]
    for record in final:
        record.pop("raw_text", None)
    next_cursor = str(offset + limit) if (offset + limit) < total_count else None
    return {
        "results": final,
        "total": total_count,
        "total_count": total_count,
        "returned_count": len(final),
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
        "served_mode": "keyword",
    }


def _compute_precomputed_facets() -> dict:
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    stats_doc = db.collection("metadata").document("dashboard_stats").get()
    stats_data = stats_doc.to_dict() if stats_doc.exists else {}
    topic_totals = dict(stats_data.get("topic_totals") or {})
    difficulty_distribution = dict(stats_data.get("difficulty_distribution") or {})
    company_topic_counts = dict(stats_data.get("company_topic_counts") or {})

    company_totals: dict[str, int] = {}
    for company, topics in company_topic_counts.items():
        if isinstance(topics, dict):
            company_totals[company] = int(sum(int(value) for value in topics.values()))

    top_companies = [
        company
        for company, _ in sorted(company_totals.items(), key=lambda item: item[1], reverse=True)[:12]
    ]
    top_topics = [
        topic
        for topic, _ in sorted(topic_totals.items(), key=lambda item: item[1], reverse=True)[:12]
    ]

    runtime = get_search_runtime_snapshot()
    query_analytics = runtime.get("query_analytics") or {}

    return {
        "generated_at": generated_at,
        "top_topics": top_topics,
        "top_companies": top_companies,
        "difficulty_distribution": difficulty_distribution,
        "popular_filters": query_analytics.get("filter_usage") or {},
        "trending_queries": query_analytics.get("top_queries") or {},
    }


def get_precomputed_facets() -> dict:
    global _facet_cache, _facet_cache_ts

    now = time.time()
    with _facet_cache_lock:
        if _facet_cache is not None and (now - _facet_cache_ts) < _FACET_CACHE_TTL_SECONDS:
            return _facet_cache

    payload = _compute_precomputed_facets()

    with _facet_cache_lock:
        _facet_cache = payload
        _facet_cache_ts = now
    return payload


def _rank_positions(scores: dict[str, float], *, include_zero: bool = True) -> dict[str, int]:
    ranked: list[tuple[str, float]] = []
    for doc_id, score in scores.items():
        value = float(score)
        if not include_zero and value <= 0:
            continue
        ranked.append((doc_id, value))

    ranked.sort(key=lambda item: item[1], reverse=True)
    return {doc_id: index + 1 for index, (doc_id, _) in enumerate(ranked)}


def _rrf_fuse(vector_scores: dict[str, float], lexical_scores: dict[str, float]) -> dict[str, float]:
    vector_ranks = _rank_positions(vector_scores, include_zero=True)
    lexical_ranks = _rank_positions(lexical_scores, include_zero=False)
    if not vector_ranks and not lexical_ranks:
        return {}

    rrf_k = max(int(settings.SEARCH_RRF_K), 1)
    vector_weight = max(float(settings.SEARCH_RRF_VECTOR_WEIGHT), 0.0)
    lexical_weight = max(float(settings.SEARCH_RRF_LEXICAL_WEIGHT), 0.0)
    if vector_weight <= 0 and lexical_weight <= 0:
        vector_weight = 1.0
        lexical_weight = 1.0

    fused_raw: dict[str, float] = {}
    for doc_id in set(vector_ranks.keys()) | set(lexical_ranks.keys()):
        total = 0.0
        vector_rank = vector_ranks.get(doc_id)
        if vector_rank is not None:
            total += vector_weight * (1.0 / (rrf_k + vector_rank))

        lexical_rank = lexical_ranks.get(doc_id)
        if lexical_rank is not None:
            total += lexical_weight * (1.0 / (rrf_k + lexical_rank))

        fused_raw[doc_id] = total

    if not fused_raw:
        return {}

    max_score = max(fused_raw.values())
    if max_score <= 0:
        return {doc_id: 0.0 for doc_id in fused_raw}

    return {doc_id: (score / max_score) for doc_id, score in fused_raw.items()}


def _apply_rerank(results: list[dict], normalized_query: str) -> list[dict]:
    if not normalized_query or not results:
        return results

    rerank_scores = search_reranker.rerank(normalized_query, results)
    if not rerank_scores:
        return results

    weight = min(max(float(settings.SEARCH_RERANK_WEIGHT), 0.0), 0.9)
    for item in results:
        doc_id = str(item.get("id") or "")
        rerank_score = rerank_scores.get(doc_id)
        if rerank_score is None:
            continue
        base_score = float(item.get("score") or 0.0)
        blended = (base_score * (1.0 - weight)) + (float(rerank_score) * weight)
        item["score"] = round(blended, 4)
        item["rerank_score"] = round(float(rerank_score), 4)

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results


def _semantic_branch(
    *,
    normalized_query: str,
    embedding_query: str,
    query_terms: list[str],
    offset: int,
    limit: int,
    company: Optional[str],
    role: Optional[str],
    year: Optional[int],
    topics: list[str],
    difficulty_normalized: Optional[str],
) -> tuple[dict | None, str | None]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        lexical_future = executor.submit(
            lambda: _build_lexical_rows_from_snapshots(
                _collect_keyword_snapshots(
                    normalized_query=normalized_query,
                    query_terms=query_terms,
                    year=year,
                    topics=topics,
                    difficulty_normalized=difficulty_normalized,
                ),
                normalized_query=normalized_query,
                query_terms=query_terms,
                company=company,
                role=role,
                year=year,
                topics=topics,
                difficulty_normalized=difficulty_normalized,
            )
        )

        acquired = _semantic_slots.acquire(timeout=settings.SEARCH_SEMANTIC_SLOT_WAIT_SECONDS)
        if not acquired:
            try:
                lexical_rows, _ = lexical_future.result()
            except Exception:
                logger.exception("Keyword branch failed while semantic slot was saturated")
                return None, "slot_saturated"

            if lexical_rows:
                return _keyword_response_from_rows(
                    lexical_rows,
                    normalized_query=normalized_query,
                    company=company,
                    topics=topics,
                    difficulty_normalized=difficulty_normalized,
                    offset=offset,
                    limit=limit,
                ), "slot_saturated"
            return None, "slot_saturated"

        semantic_error: str | None = None
        semantic_rows: list[dict] = []
        vector_scores: dict[str, float] = {}

        try:
            query_vector = _get_or_embed_query_vector(embedding_query)
            candidates = faiss_store.search(query_vector, k=max((offset + limit) * 8, 80))

            score_map: dict[str, float] = {}
            for doc_id, score in candidates:
                score_map[doc_id] = max(float(score), score_map.get(doc_id, -1.0))

            if score_map:
                doc_refs = [
                    db.collection("interview_experiences").document(doc_id)
                    for doc_id in score_map.keys()
                ]
                snapshots = list(db.get_all(doc_refs))

                for snapshot in snapshots:
                    if not snapshot.exists:
                        continue
                    data = serialize_doc(snapshot, include_contributor=True)
                    if not data.get("is_active", True):
                        continue
                    if not _apply_filters(data, company, role, year, topics, difficulty_normalized):
                        continue

                    semantic_score = float(score_map.get(snapshot.id, 0.0))
                    vector_scores[snapshot.id] = semantic_score
                    semantic_rows.append(
                        {
                            "doc_id": snapshot.id,
                            "data": data,
                            "semantic_score": semantic_score,
                            "matched_question": None,
                        }
                    )
        except Exception:
            logger.exception("Semantic branch failed; falling back to keyword mode")
            semantic_error = "semantic_error"
        finally:
            _semantic_slots.release()

        try:
            lexical_rows, lexical_scores = lexical_future.result()
        except Exception:
            logger.exception("Keyword branch failed during hybrid retrieval")
            lexical_rows, lexical_scores = [], {}

        if semantic_error:
            if lexical_rows:
                return _keyword_response_from_rows(
                    lexical_rows,
                    normalized_query=normalized_query,
                    company=company,
                    topics=topics,
                    difficulty_normalized=difficulty_normalized,
                    offset=offset,
                    limit=limit,
                ), semantic_error
            return None, semantic_error

        if not semantic_rows and lexical_rows:
            return _keyword_response_from_rows(
                lexical_rows,
                normalized_query=normalized_query,
                company=company,
                topics=topics,
                difficulty_normalized=difficulty_normalized,
                offset=offset,
                limit=limit,
            ), None

        candidate_rows: dict[str, dict] = {}
        for row in semantic_rows:
            candidate_rows[row["doc_id"]] = row

        for row in lexical_rows:
            doc_id = row["doc_id"]
            existing = candidate_rows.get(doc_id)
            if existing is None:
                candidate_rows[doc_id] = {
                    "doc_id": doc_id,
                    "data": row["data"],
                    "semantic_score": 0.0,
                    "lexical_score": float(row.get("lexical_score") or 0.0),
                    "matched_question": row.get("matched_question"),
                }
                continue

            existing["lexical_score"] = float(row.get("lexical_score") or 0.0)
            if not existing.get("matched_question"):
                existing["matched_question"] = row.get("matched_question")

        fused_scores = _rrf_fuse(vector_scores, lexical_scores)
        results = []
        for row in candidate_rows.values():
            data = row["data"]
            semantic_score = float(row.get("semantic_score") or 0.0)
            lexical = float(row.get("lexical_score") or 0.0)
            fused = float(fused_scores.get(row["doc_id"], max(semantic_score, lexical)))

            data["score"] = round(fused, 4)
            data["match_reason"] = _generate_match_explanation(
                data,
                normalized_query,
                "hybrid",
                company,
                topics,
                difficulty_normalized,
                semantic_score,
                lexical,
                row.get("matched_question"),
            )
            results.append(data)

        results.sort(key=lambda item: item.get("score", 0), reverse=True)
        results = _apply_rerank(results, normalized_query)

        total_count = len(results)
        final = results[offset:offset + limit]
        for record in final:
            record.pop("raw_text", None)
        next_cursor = str(offset + limit) if (offset + limit) < total_count else None
        return {
            "results": final,
            "total": total_count,
            "total_count": total_count,
            "returned_count": len(final),
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
            "served_mode": "hybrid",
        }, None


def _typesense_branch(
    *,
    normalized_query: str,
    embedding_query: str,
    query_terms: list[str],
    offset: int,
    limit: int,
    company: Optional[str],
    role: Optional[str],
    year: Optional[int],
    topics: list[str],
    difficulty_normalized: Optional[str],
) -> tuple[dict | None, str | None]:
    if not typesense_store.enabled:
        return None, "typesense_disabled"

    query_vector: list[float] | None = None
    if embedding_query:
        try:
            vector = _get_or_embed_query_vector(embedding_query)
            query_vector = vector.astype("float32").tolist()
        except Exception:
            logger.exception("Typesense embedding generation failed; trying lexical only")

    try:
        payload = typesense_store.search_hybrid(
            query=embedding_query or normalized_query,
            offset=offset,
            limit=limit,
            company=company,
            role=role,
            year=year,
            topics=topics,
            difficulty=difficulty_normalized,
            query_vector=query_vector,
            alpha=float(settings.SEARCH_VECTOR_ALPHA),
        )
    except Exception:
        logger.exception("Typesense branch failed")
        return None, "typesense_error"

    if payload is None:
        return None, "typesense_unavailable"

    total_count = int(payload.get("total_count") or 0)
    doc_ids = payload.get("doc_ids") or []
    score_map = payload.get("score_map") or {}

    if not doc_ids:
        return {
            "results": [],
            "total": total_count,
            "total_count": total_count,
            "returned_count": 0,
            "has_more": False,
            "next_cursor": None,
        }, None

    doc_refs = [db.collection("interview_experiences").document(doc_id) for doc_id in doc_ids]
    snapshots = list(db.get_all(doc_refs))
    by_id = {snapshot.id: snapshot for snapshot in snapshots if snapshot.exists}

    vector_scores: dict[str, float] = {}
    lexical_scores: dict[str, float] = {}
    candidate_rows: list[dict] = []
    for doc_id in doc_ids:
        snapshot = by_id.get(doc_id)
        if snapshot is None:
            continue

        data = serialize_doc(snapshot, include_contributor=True)
        if not data.get("is_active", True):
            continue
        if not _apply_filters(data, company, role, year, topics, difficulty_normalized):
            continue

        lexical, matched_q = keyword_score(data, normalized_query, query_terms)
        engine_score = float(score_map.get(doc_id, 0.0))
        vector_scores[doc_id] = engine_score
        lexical_scores[doc_id] = lexical
        candidate_rows.append(
            {
                "doc_id": doc_id,
                "data": data,
                "engine_score": engine_score,
                "lexical_score": lexical,
                "matched_question": matched_q,
            }
        )

    fused_scores = _rrf_fuse(vector_scores, lexical_scores) if normalized_query else {}
    results = []
    for row in candidate_rows:
        data = row["data"]
        engine_score = float(row["engine_score"])
        lexical = float(row["lexical_score"])
        if normalized_query:
            score = float(fused_scores.get(row["doc_id"], 0.0))
            match_mode = "hybrid"
        else:
            score = engine_score
            match_mode = "keyword"

        data["score"] = round(score, 4)
        data["match_reason"] = _generate_match_explanation(
            data,
            normalized_query,
            match_mode,
            company,
            topics,
            difficulty_normalized,
            engine_score,
            lexical,
            row.get("matched_question"),
        )
        results.append(data)

    results = _apply_rerank(results, normalized_query)

    effective_total = max(total_count, offset + len(results)) if results else total_count
    next_cursor = str(offset + limit) if (offset + limit) < effective_total else None

    for record in results:
        record.pop("raw_text", None)

    return {
        "results": results,
        "total": effective_total,
        "total_count": effective_total,
        "returned_count": len(results),
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
    }, None


@router.get("")
def search(
    request: Request,
    q: str = "",
    mode: str = Query("auto", pattern="^(auto|semantic|keyword)$"),
    company: Optional[str] = None,
    role: Optional[str] = None,
    year: Optional[int] = None,
    topic: Optional[str] = None,
    difficulty: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=50),
    response: FastAPIResponse = None,
) -> dict:
    started_at = time.perf_counter()
    served_mode = "unknown"
    served_engine = "faiss"
    fallback_reason: str | None = None
    cache_hit = False

    _search_limiter.check(
        client_identifier(request),
        detail="Search rate limit exceeded. Please retry shortly.",
    )

    requested_mode = mode.lower()
    limit = min(limit, settings.MAX_SEARCH_RESULTS)
    company = company.strip() if company else None
    role = role.strip() if role else None
    topics = _normalize_topics(topic)
    difficulty_normalized = difficulty.strip().title() if difficulty else None
    normalized_query = normalize_text(q)
    embedding_query = expand_query_text(normalized_query)
    base_terms = tokenize_terms(normalized_query, max_terms=10)
    query_terms = expand_query_terms(base_terms, max_terms=20)
    cursor_value = (cursor or "").strip()

    if cursor_value:
        if not cursor_value.isdigit():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid cursor value.",
            )
        offset = int(cursor_value)
    else:
        offset = 0

    semantic_requested = bool(requested_mode in {"auto", "semantic"} and normalized_query)
    cacheable = True
    request_id = str(getattr(request.state, "request_id", "unknown"))
    filter_keys = _extract_filter_keys(
        company=company,
        role=role,
        year=year,
        topics=topics,
        difficulty=difficulty_normalized,
    )

    if response is not None:
        response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"

    cache_key = _cache_key(
        settings.SEARCH_ENGINE,
        normalized_query,
        embedding_query,
        requested_mode,
        company,
        role,
        year,
        topic,
        difficulty_normalized,
        offset,
        limit,
    )
    cached = _get_cached(cache_key)
    if cached is not None:
        cache_hit = True
        served_mode = str(cached.get("served_mode") or "cache")
        served_engine = str(cached.get("served_engine") or settings.SEARCH_ENGINE)
        total_count = int(cached.get("total_count") or cached.get("total") or 0)

        if response is not None:
            response.headers["X-Search-Mode"] = served_mode
            response.headers["X-Search-Engine"] = served_engine
            response.headers["X-Request-ID"] = request_id

        latency_ms = (time.perf_counter() - started_at) * 1000.0
        _record_search_observation(
            served_mode=served_mode,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            semantic_requested=semantic_requested,
            fallback_reason=fallback_reason,
            query_text=normalized_query,
            total_count=total_count,
            filter_keys=filter_keys,
        )
        return cached

    if typesense_store.enabled:
        typesense_result, fallback_reason = _typesense_branch(
            normalized_query=normalized_query,
            embedding_query=embedding_query,
            query_terms=query_terms,
            offset=offset,
            limit=limit,
            company=company,
            role=role,
            year=year,
            topics=topics,
            difficulty_normalized=difficulty_normalized,
        )
        if typesense_result is not None:
            served_mode = "hybrid" if normalized_query else "keyword"
            served_engine = "typesense"
            typesense_result["served_mode"] = served_mode
            typesense_result["served_engine"] = served_engine
            if response is not None:
                response.headers["X-Search-Mode"] = served_mode
                response.headers["X-Search-Engine"] = served_engine
                response.headers["X-Request-ID"] = request_id
            if cacheable:
                _set_cached(cache_key, typesense_result)

            latency_ms = (time.perf_counter() - started_at) * 1000.0
            _record_search_observation(
                served_mode=served_mode,
                latency_ms=latency_ms,
                cache_hit=cache_hit,
                semantic_requested=semantic_requested,
                fallback_reason=fallback_reason,
                query_text=normalized_query,
                total_count=int(typesense_result.get("total_count") or 0),
                filter_keys=filter_keys,
            )
            return typesense_result

        cacheable = False

    mode = requested_mode
    if semantic_requested:
        if _semantic_circuit_open():
            fallback_reason = "circuit_open"
            mode = "keyword"
            cacheable = False
        else:
            semantic_result, fallback_reason = _semantic_branch(
                normalized_query=normalized_query,
                embedding_query=embedding_query,
                query_terms=query_terms,
                offset=offset,
                limit=limit,
                company=company,
                role=role,
                year=year,
                topics=topics,
                difficulty_normalized=difficulty_normalized,
            )
            if semantic_result is not None:
                served_mode = str(semantic_result.get("served_mode") or "hybrid")
                served_engine = "faiss"
                semantic_result["served_mode"] = served_mode
                semantic_result["served_engine"] = served_engine
                if response is not None:
                    response.headers["X-Search-Mode"] = served_mode
                    response.headers["X-Search-Engine"] = served_engine
                    response.headers["X-Request-ID"] = request_id
                    if fallback_reason:
                        response.headers["X-Search-Fallback"] = fallback_reason
                if cacheable:
                    _set_cached(cache_key, semantic_result)
                latency_ms = (time.perf_counter() - started_at) * 1000.0
                _record_search_observation(
                    served_mode=served_mode,
                    latency_ms=latency_ms,
                    cache_hit=cache_hit,
                    semantic_requested=semantic_requested,
                    fallback_reason=fallback_reason,
                    query_text=normalized_query,
                    total_count=int(semantic_result.get("total_count") or 0),
                    filter_keys=filter_keys,
                )
                return semantic_result

            if fallback_reason == "semantic_error":
                _register_semantic_failure()

            mode = "keyword"
            cacheable = False

    snapshots = _collect_keyword_snapshots(
        normalized_query=normalized_query,
        query_terms=query_terms,
        year=year,
        topics=topics,
        difficulty_normalized=difficulty_normalized,
    )
    lexical_rows, _ = _build_lexical_rows_from_snapshots(
        snapshots,
        normalized_query=normalized_query,
        query_terms=query_terms,
        company=company,
        role=role,
        year=year,
        topics=topics,
        difficulty_normalized=difficulty_normalized,
    )

    result = _keyword_response_from_rows(
        lexical_rows,
        normalized_query=normalized_query,
        company=company,
        topics=topics,
        difficulty_normalized=difficulty_normalized,
        offset=offset,
        limit=limit,
    )
    result["served_engine"] = "faiss"
    served_mode = "keyword"
    served_engine = "faiss"
    total_count = int(result.get("total_count") or 0)

    if response is not None:
        response.headers["X-Search-Mode"] = served_mode
        response.headers["X-Search-Engine"] = served_engine
        response.headers["X-Request-ID"] = request_id
        if fallback_reason:
            response.headers["X-Search-Fallback"] = fallback_reason
    if cacheable:
        _set_cached(cache_key, result)

    latency_ms = (time.perf_counter() - started_at) * 1000.0
    logger.info(
        "search request_id=%s requested_mode=%s served_mode=%s served_engine=%s q=%s offset=%s limit=%s total_count=%s latency_ms=%.2f fallback=%s cache_hit=%s",
        request_id,
        requested_mode,
        served_mode,
        served_engine,
        normalized_query,
        offset,
        limit,
        total_count,
        latency_ms,
        fallback_reason,
        cache_hit,
    )
    _record_search_observation(
        served_mode=served_mode,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
        semantic_requested=semantic_requested,
        fallback_reason=fallback_reason,
        query_text=normalized_query,
        total_count=total_count,
        filter_keys=filter_keys,
    )
    return result
