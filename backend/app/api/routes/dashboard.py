from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from firebase_admin import firestore

from app.api.dependencies import get_current_user, require_placement_cell
from app.core.config import settings
from app.core.firebase import db
from app.core.rate_limit import SlidingWindowLimiter, client_identifier
from app.api.routes.search import (
    clear_search_caches,
    get_search_runtime_snapshot,
    reset_search_runtime_snapshot,
    trigger_search_warmup,
)
from app.utils.serialization import serialize_doc


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ─────────────────────────────────────────────────────────────────────────────
# Pre-computed stats helpers
# ─────────────────────────────────────────────────────────────────────────────

# Fields that MUST exist in the cache for it to be considered valid.
_REQUIRED_CACHE_FIELDS = {
    "total_experiences",
    "frequent_questions",
    "interview_progression",
}

# ── In-memory cache (avoids 4× Firestore reads per page load) ────────────
_mem_cache: dict = {}
_mem_cache_ts: float = 0.0
_MEM_CACHE_TTL: float = 300.0  # 5 minutes
_admin_cache: dict = {}
_admin_cache_ts: float = 0.0
_ADMIN_CACHE_TTL: float = 120.0  # 2 minutes
_dashboard_limiter = SlidingWindowLimiter(settings.DASHBOARD_RATE_LIMIT_PER_MINUTE, 60)
_admin_dashboard_limiter = SlidingWindowLimiter(max(20, settings.DASHBOARD_RATE_LIMIT_PER_MINUTE // 2), 60)


def _benchmark_doc_ref():
    return db.collection("metadata").document("search_relevance_benchmark")


def _enforce_dashboard_rate_limit(request: Request, user: dict, *, admin: bool = False) -> None:
    limiter = _admin_dashboard_limiter if admin else _dashboard_limiter
    limiter.check(
        client_identifier(request, str(user.get("uid") or "unknown")),
        detail="Dashboard rate limit exceeded. Please retry shortly.",
    )


def _get_or_compute_stats() -> dict:
    """Get pre-computed stats from cache or compute fresh.

    Resolution order:
      1. In-memory cache (hot path, < 1 µs)
      2. Firestore metadata doc
      3. Full recompute + cache write

    If the cached document is missing any required analytics fields
    (e.g. written by an older code version), it is treated as stale
    and recomputed immediately.
    """
    global _mem_cache, _mem_cache_ts

    # 1. In-memory cache — serves all 4 endpoints from one Firestore read
    now = time.time()
    if _mem_cache and (now - _mem_cache_ts) < _MEM_CACHE_TTL:
        return _mem_cache

    # 2. Firestore cache
    stats_ref = db.collection("metadata").document("dashboard_stats")
    stats_doc = stats_ref.get()

    if stats_doc.exists:
        data = stats_doc.to_dict()
        # Validate cache has all required fields
        if _REQUIRED_CACHE_FIELDS.issubset(data.keys()):
            _mem_cache = data
            _mem_cache_ts = now
            return data
        # Stale cache — fall through to recompute

    result = _compute_and_cache_stats()
    _mem_cache = result
    _mem_cache_ts = now
    return result


def _compute_and_cache_stats() -> dict:
    """Compute aggregated stats and cache them."""
    snapshots = list(
        db.collection("interview_experiences").limit(settings.DASHBOARD_SAMPLE_LIMIT).stream()
    )
    
    # Filter out soft-deleted contributions
    active_snapshots = [
        s for s in snapshots
        if (s.to_dict() or {}).get("is_active", True)
    ]

    if not active_snapshots:
        stats = {
            "total_experiences": 0,
            "top_company": None,
            "top_topic": None,
            "topic_totals": {},
            "difficulty_distribution": {},
            "company_topic_counts": {},
            "frequent_questions": {},
            "interview_progression": {},
        }
    else:
        topic_counter = Counter()
        difficulty_counter = Counter()
        company_topic_counts = defaultdict(lambda: Counter())
        company_counter = Counter()
        
        for snapshot in active_snapshots:
            data = serialize_doc(snapshot)
            company = data.get("company", "Unknown")
            topics = data.get("topics") or []
            difficulty = data.get("difficulty", "Unknown")
            
            company_counter[company] += 1
            difficulty_counter[difficulty] += 1
            for topic in topics:
                topic_counter[topic] += 1
                company_topic_counts[company][topic] += 1
        
        top_company = company_counter.most_common(1)[0][0] if company_counter else None
        top_topic = topic_counter.most_common(1)[0][0] if topic_counter else None
        
        # Pre-compute frequent questions from the same snapshot
        frequent_questions = _compute_question_frequencies(active_snapshots, limit=10)
        
        # Pre-compute interview progression from the same snapshot
        interview_progression = _compute_interview_progression(active_snapshots, limit=6)
        
        stats = {
            "total_experiences": len(active_snapshots),
            "top_company": top_company,
            "top_topic": top_topic,
            "topic_totals": dict(topic_counter),
            "difficulty_distribution": dict(difficulty_counter),
            "company_topic_counts": {
                company: dict(counter) for company, counter in company_topic_counts.items()
            },
            "frequent_questions": frequent_questions,
            "interview_progression": interview_progression,
        }
    
    # Cache the stats
    stats["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        db.collection("metadata").document("dashboard_stats").set(stats)
    except Exception:
        pass  # Don't fail if caching fails
    
    return stats


def update_dashboard_stats_async():
    """Called after new experience submission to refresh cached stats."""
    global _mem_cache, _mem_cache_ts, _admin_cache, _admin_cache_ts
    try:
        result = _compute_and_cache_stats()
        _mem_cache = result
        _mem_cache_ts = time.time()
        _admin_cache = {}
        _admin_cache_ts = 0.0
    except Exception:
        pass  # Non-blocking


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions for expensive operations
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_COLLAPSE_WS_RE = re.compile(r"\s+")


def _normalize_question(q: str) -> str:
    """Normalize question text for O(1) dedup: lowercase, strip punctuation, collapse whitespace."""
    q = q.lower().strip()
    q = _STRIP_PUNCT_RE.sub("", q)
    q = _COLLAPSE_WS_RE.sub(" ", q).strip()
    return q


def _compute_question_frequencies(snapshots: list, limit: int = 5) -> dict:
    """Compute frequently repeated questions from a list of snapshots (no DB call).

    Uses hash-based O(n) dedup instead of O(n²) pairwise comparison.
    Supports both new (question_text) and legacy (question) field names.
    Only counts questions with confidence >= 0.7 (if confidence is present).
    """
    # normalized_text → first-seen original text
    norm_to_original: dict[str, str] = {}
    # normalized_text → set of experience IDs
    question_ids: dict[str, set] = defaultdict(set)

    for snapshot in snapshots:
        data = snapshot.to_dict() if hasattr(snapshot, 'to_dict') else snapshot
        experience_id = snapshot.id if hasattr(snapshot, 'id') else data.get('id')
        questions = data.get("extracted_questions") or []

        for q in questions:
            if isinstance(q, dict):
                q_text = q.get("question_text") or q.get("question", "")
                confidence = q.get("confidence", 1.0)
            else:
                q_text = str(q)
                confidence = 1.0

            if not q_text or confidence < 0.7:
                continue

            norm = _normalize_question(q_text)
            if not norm:
                continue

            if norm not in norm_to_original:
                norm_to_original[norm] = q_text
            question_ids[norm].add(experience_id)

    frequent = {
        norm_to_original[norm]: len(ids)
        for norm, ids in question_ids.items()
        if len(ids) >= 2
    }
    return dict(sorted(frequent.items(), key=lambda x: x[1], reverse=True)[:limit])


def _compute_interview_progression(snapshots: list, limit: int = 4) -> dict:
    """Derive common interview progressions from a list of snapshots (no DB call)."""
    company_rounds: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)

    for snapshot in snapshots:
        data = snapshot.to_dict() if hasattr(snapshot, 'to_dict') else snapshot
        company = data.get("company", "Unknown")
        round_name = data.get("round", "").strip()
        topics = data.get("topics") or []

        if round_name:
            company_rounds[company].append((round_name, topics[:3]))

    result: dict[str, dict] = {}
    for company, rounds in sorted(
        company_rounds.items(), key=lambda kv: len(kv[1]), reverse=True
    )[:limit]:
        round_freq: dict[str, int] = Counter()
        round_topics: dict[str, Counter] = defaultdict(Counter)

        for round_name, topics in rounds:
            round_freq[round_name] += 1
            for topic in topics:
                round_topics[round_name][topic] += 1

        sorted_rounds = round_freq.most_common()

        stages = {}
        for round_name, freq in sorted_rounds:
            top_topics = [t for t, _ in round_topics[round_name].most_common(5)]
            stages[round_name] = {
                "topics": top_topics,
                "frequency": freq,
            }

        total_experiences = len(rounds)
        result[company] = {
            "stages": stages,
            "total_experiences": total_experiences,
        }

    return result


def _build_insights(stats: dict) -> list:
    """Build actionable insights from stats."""
    insights = []
    topic_totals = stats.get("topic_totals", {})
    difficulty_dist = stats.get("difficulty_distribution", {})
    company_topic_counts = stats.get("company_topic_counts", {})
    
    if topic_totals:
        top_topics = ", ".join(sorted(topic_totals, key=topic_totals.get, reverse=True)[:3])
        insights.append(f"Focus revision on {top_topics}; these show up the most across interviews.")
    
    if difficulty_dist:
        hardest = max(difficulty_dist.items(), key=lambda item: item[1])[0]
        insights.append(f"Most experiences are labeled '{hardest}'. Plan prep depth accordingly.")
    
    if company_topic_counts:
        company, topics = max(company_topic_counts.items(), key=lambda item: sum(item[1].values()))
        if topics:
            top_company_topic = max(topics.items(), key=lambda item: item[1])[0]
            insights.append(
                f"{company} emphasizes {top_company_topic}. Tailor your prep for that company accordingly."
            )
    
    return insights


# ─────────────────────────────────────────────────────────────────────────────
# TIER 1: Instant stats (cached/pre-computed)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_dashboard_stats(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Tier 1: Instant stats from cache - < 100ms target."""
    _enforce_dashboard_rate_limit(request, user)
    stats = _get_or_compute_stats()
    
    # Get user contribution impact
    user_uid = user.get("uid", "")
    user_experiences = list(
        db.collection("interview_experiences")
        .where(filter=firestore.FieldFilter("created_by", "==", user_uid))
        .limit(100)
        .stream()
    )
    
    user_experience_count = len(user_experiences)
    total_questions = sum(
        (doc.to_dict().get("stats") or {}).get("total_question_count", 0)
        or len(doc.to_dict().get("extracted_questions", []))
        for doc in user_experiences
    )
    
    generated_at = str(stats.get("generated_at") or "")
    freshness_seconds = None
    if generated_at:
        try:
            generated_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated_dt.tzinfo is None:
                generated_dt = generated_dt.replace(tzinfo=timezone.utc)
            freshness_seconds = max(0, int((datetime.now(timezone.utc) - generated_dt).total_seconds()))
        except ValueError:
            freshness_seconds = None

    return {
        "total_experiences": stats.get("total_experiences", 0),
        "top_company": stats.get("top_company"),
        "top_topic": stats.get("top_topic"),
        "data_freshness": {
            "generated_at": generated_at or None,
            "freshness_seconds": freshness_seconds,
        },
        "contribution_impact": {
            "experiences_submitted": user_experience_count,
            "questions_extracted": total_questions,
            "archive_size": stats.get("total_experiences", 0),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# TIER 2: Charts and analytics (lazy-loaded)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/charts")
def get_dashboard_charts(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Tier 2: Charts data - loaded after initial render."""
    _enforce_dashboard_rate_limit(request, user)
    stats = _get_or_compute_stats()
    insights = _build_insights(stats)
    
    return {
        "topic_totals": stats.get("topic_totals", {}),
        "difficulty_distribution": stats.get("difficulty_distribution", {}),
        "company_topic_counts": stats.get("company_topic_counts", {}),
        "insights": insights,
    }


@router.get("/questions")
def get_frequent_questions(
    request: Request,
    limit: int = 5,
    user: dict = Depends(get_current_user),
) -> dict:
    """Tier 2: Frequently asked questions - served from cache."""
    _enforce_dashboard_rate_limit(request, user)
    stats = _get_or_compute_stats()
    cached = stats.get("frequent_questions", {})
    # Apply limit (cache stores up to 10)
    limited = dict(list(cached.items())[:limit])
    return {
        "frequent_questions": limited,
    }


@router.get("/flows")
def get_interview_flows(
    request: Request,
    limit: int = 4,
    user: dict = Depends(get_current_user),
) -> dict:
    """Tier 2: Common interview progressions – served from cache."""
    _enforce_dashboard_rate_limit(request, user)
    stats = _get_or_compute_stats()
    cached = stats.get("interview_progression", {})
    # Apply limit (cache stores up to 6)
    limited = dict(list(cached.items())[:limit])
    return {
        "interview_progression": limited,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Legacy endpoint (backwards compatibility)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("")
def get_dashboard(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Legacy: Full dashboard data in one call (kept for backwards compatibility)."""
    _enforce_dashboard_rate_limit(request, user)
    stats = _get_or_compute_stats()
    insights = _build_insights(stats)
    
    user_uid = user.get("uid", "")
    user_experiences = list(
        db.collection("interview_experiences")
        .where(filter=firestore.FieldFilter("created_by", "==", user_uid))
        .limit(100)
        .stream()
    )
    
    user_experience_count = len(user_experiences)
    total_questions = sum(
        (doc.to_dict().get("stats") or {}).get("total_question_count", 0)
        or len(doc.to_dict().get("extracted_questions", []))
        for doc in user_experiences
    )
    
    return {
        "total_experiences": stats.get("total_experiences", 0),
        "topic_frequency": stats.get("company_topic_counts", {}),
        "difficulty_distribution": stats.get("difficulty_distribution", {}),
        "frequent_questions": stats.get("frequent_questions", {}),
        "interview_progression": stats.get("interview_progression", {}),
        "contribution_impact": {
            "experiences_submitted": user_experience_count,
            "questions_extracted": total_questions,
            "archive_size": stats.get("total_experiences", 0),
        },
        "insights": insights,
    }


@router.get("/admin")
def get_admin_dashboard(
    request: Request,
    user: dict = Depends(require_placement_cell),
) -> dict:
    """Placement-cell analytics with moderation and quality metrics."""
    global _admin_cache, _admin_cache_ts
    _enforce_dashboard_rate_limit(request, user, admin=True)

    now_ts = time.time()
    if _admin_cache and (now_ts - _admin_cache_ts) < _ADMIN_CACHE_TTL:
        return _admin_cache

    def _coerce_datetime(value) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                return None
        return None

    snapshots = list(
        db.collection("interview_experiences")
        .limit(settings.DASHBOARD_SAMPLE_LIMIT)
        .stream()
    )

    active_count = 0
    hidden_count = 0
    anonymous_count = 0
    public_count = 0

    nlp_status = Counter()
    year_distribution = Counter()
    submission_role_distribution = Counter()
    company_distribution = Counter()
    difficulty_distribution = Counter()
    contributor_counter = Counter()

    total_questions = 0
    total_user_questions = 0
    contact_opt_in = 0
    last_30_days = 0
    last_90_days = 0
    nlp_failed_samples = []
    now = datetime.now(timezone.utc)

    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        is_active = bool(data.get("is_active", True))
        if is_active:
            active_count += 1
        else:
            hidden_count += 1

        is_anonymous = bool(data.get("is_anonymous", False))
        if is_anonymous:
            anonymous_count += 1
        else:
            public_count += 1

        nlp_status[str(data.get("nlp_status") or "unknown")] += 1

        if isinstance(data.get("year"), int):
            year_distribution[str(data["year"])] += 1

        role_value = str(data.get("role") or "unknown").strip()
        if role_value:
            submission_role_distribution[role_value] += 1

        company = str(data.get("company") or "Unknown").strip() or "Unknown"
        company_distribution[company] += 1

        difficulty = str(data.get("difficulty") or "Unknown").strip() or "Unknown"
        difficulty_distribution[difficulty] += 1

        contributor_uid = str(data.get("created_by") or "").strip()
        if contributor_uid and is_active:
            contributor_counter[contributor_uid] += 1

        stats_data = data.get("stats") or {}
        extracted = data.get("extracted_questions") or []
        question_total = int(stats_data.get("total_question_count") or len(extracted))
        question_user = int(stats_data.get("user_question_count") or 0)
        total_questions += max(question_total, 0)
        total_user_questions += max(question_user, 0)

        if bool(data.get("allow_contact", False)):
            contact_opt_in += 1

        created_at = _coerce_datetime(data.get("created_at"))
        if created_at:
            age = now - created_at
            if age <= timedelta(days=30):
                last_30_days += 1
            if age <= timedelta(days=90):
                last_90_days += 1

        if str(data.get("nlp_status") or "").lower() == "failed" and len(nlp_failed_samples) < 8:
            nlp_failed_samples.append(
                {
                    "id": snapshot.id,
                    "company": company,
                    "year": data.get("year"),
                    "created_at": created_at.isoformat() if created_at else None,
                }
            )

    top_contributors = []
    for contributor_uid, submissions in contributor_counter.most_common(8):
        user_doc = db.collection("users").document(contributor_uid).get()
        user_data = user_doc.to_dict() or {}
        display_name = str(user_data.get("display_name") or user_data.get("name") or contributor_uid[:8])
        top_contributors.append(
            {
                "uid": contributor_uid,
                "display_name": display_name,
                "submissions": submissions,
            }
        )

    total = len(snapshots)
    active_base = max(active_count, 1)
    total_base = max(total, 1)
    response = {
        "archive_overview": {
            "total_sampled": total,
            "active": active_count,
            "hidden": hidden_count,
        },
        "privacy_breakdown": {
            "anonymous": anonymous_count,
            "public": public_count,
        },
        "quality_metrics": {
            "avg_questions_per_experience": round(total_questions / active_base, 2),
            "avg_user_questions_per_experience": round(total_user_questions / active_base, 2),
            "contact_opt_in_rate_percent": round((contact_opt_in / total_base) * 100, 1),
            "nlp_done_rate_percent": round(((nlp_status.get("done", 0)) / total_base) * 100, 1),
        },
        "freshness": {
            "last_30_days": last_30_days,
            "last_90_days": last_90_days,
        },
        "nlp_pipeline": dict(nlp_status),
        "year_distribution": dict(year_distribution),
        "submission_role_distribution": dict(submission_role_distribution),
        "company_distribution": dict(company_distribution.most_common(12)),
        "difficulty_distribution": dict(difficulty_distribution),
        "moderation": {
            "hidden_count": hidden_count,
            "nlp_failed_count": nlp_status.get("failed", 0),
            "failed_examples": nlp_failed_samples,
        },
        "top_contributors": top_contributors,
        "search_runtime": get_search_runtime_snapshot(),
    }

    _admin_cache = response
    _admin_cache_ts = now_ts
    return response


@router.post("/admin/search/runtime/reset")
def admin_reset_search_runtime(
    request: Request,
    user: dict = Depends(require_placement_cell),
) -> dict:
    _enforce_dashboard_rate_limit(request, user, admin=True)
    return {
        "status": "ok",
        "search_runtime": reset_search_runtime_snapshot(),
    }


@router.post("/admin/search/cache/clear")
def admin_clear_search_cache(
    request: Request,
    user: dict = Depends(require_placement_cell),
) -> dict:
    _enforce_dashboard_rate_limit(request, user, admin=True)
    result = clear_search_caches()
    return {
        "status": "ok",
        "cache": result,
    }


@router.post("/admin/search/warmup")
def admin_run_search_warmup(
    request: Request,
    user: dict = Depends(require_placement_cell),
) -> dict:
    _enforce_dashboard_rate_limit(request, user, admin=True)
    result = trigger_search_warmup()
    return {
        "status": "ok",
        "warmup": result,
        "search_runtime": get_search_runtime_snapshot(),
    }


@router.get("/admin/search/benchmark")
def admin_get_search_benchmark(
    request: Request,
    user: dict = Depends(require_placement_cell),
) -> dict:
    _enforce_dashboard_rate_limit(request, user, admin=True)
    snapshot = _benchmark_doc_ref().get()
    if not snapshot.exists:
        return {
            "status": "empty",
            "entries": [],
            "updated_at": None,
            "updated_by": None,
        }

    data = snapshot.to_dict() or {}
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    return {
        "status": "ok",
        "entries": entries,
        "updated_at": data.get("updated_at"),
        "updated_by": data.get("updated_by"),
        "note": data.get("note"),
    }


@router.put("/admin/search/benchmark")
def admin_upsert_search_benchmark(
    request: Request,
    payload: dict = Body(...),
    user: dict = Depends(require_placement_cell),
) -> dict:
    _enforce_dashboard_rate_limit(request, user, admin=True)

    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=400, detail="'entries' must be a non-empty list.")

    if len(entries) > 400:
        raise HTTPException(status_code=400, detail="Maximum 400 benchmark entries allowed.")

    sanitized_entries: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="Each benchmark entry must be an object.")

        query = str(entry.get("query") or "").strip()
        filters = entry.get("filters") or {}
        relevance = entry.get("relevance") or {}

        if len(query) < 2:
            raise HTTPException(status_code=400, detail="Each benchmark entry requires a query.")
        if not isinstance(filters, dict):
            raise HTTPException(status_code=400, detail="'filters' must be an object.")
        if not isinstance(relevance, dict) or not relevance:
            raise HTTPException(status_code=400, detail="'relevance' must be a non-empty object.")

        cleaned_relevance: dict[str, float] = {}
        for doc_id, score in relevance.items():
            key = str(doc_id).strip()
            if not key:
                continue
            try:
                cleaned_relevance[key] = float(score)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid relevance score for doc '{key}'.") from exc

        if not cleaned_relevance:
            raise HTTPException(status_code=400, detail="Each entry needs at least one valid relevance label.")

        sanitized_entries.append(
            {
                "query": query,
                "filters": filters,
                "relevance": cleaned_relevance,
            }
        )

    note = str(payload.get("note") or "").strip()
    if len(note) > 280:
        raise HTTPException(status_code=400, detail="'note' must be at most 280 characters.")

    now = datetime.now(timezone.utc).isoformat()
    _benchmark_doc_ref().set(
        {
            "entries": sanitized_entries,
            "updated_at": now,
            "updated_by": str(user.get("uid") or "placement_cell"),
            "note": note or None,
        },
        merge=True,
    )

    return {
        "status": "ok",
        "entries_count": len(sanitized_entries),
        "updated_at": now,
    }
