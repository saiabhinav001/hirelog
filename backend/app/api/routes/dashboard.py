from __future__ import annotations

import re
import time
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends

from app.api.dependencies import get_current_user
from app.core.config import settings
from app.core.firebase import db
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
    try:
        db.collection("metadata").document("dashboard_stats").set(stats)
    except Exception:
        pass  # Don't fail if caching fails
    
    return stats


def update_dashboard_stats_async():
    """Called after new experience submission to refresh cached stats."""
    global _mem_cache, _mem_cache_ts
    try:
        result = _compute_and_cache_stats()
        _mem_cache = result
        _mem_cache_ts = time.time()
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
def get_dashboard_stats(user: dict = Depends(get_current_user)) -> dict:
    """Tier 1: Instant stats from cache - < 100ms target."""
    stats = _get_or_compute_stats()
    
    # Get user contribution impact
    user_uid = user.get("uid", "")
    user_experiences = list(
        db.collection("interview_experiences")
        .where("created_by", "==", user_uid)
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
        "top_company": stats.get("top_company"),
        "top_topic": stats.get("top_topic"),
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
def get_dashboard_charts(user: dict = Depends(get_current_user)) -> dict:
    """Tier 2: Charts data - loaded after initial render."""
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
    limit: int = 5,
    user: dict = Depends(get_current_user),
) -> dict:
    """Tier 2: Frequently asked questions - served from cache."""
    stats = _get_or_compute_stats()
    cached = stats.get("frequent_questions", {})
    # Apply limit (cache stores up to 10)
    limited = dict(list(cached.items())[:limit])
    return {
        "frequent_questions": limited,
    }


@router.get("/flows")
def get_interview_flows(
    limit: int = 4,
    user: dict = Depends(get_current_user),
) -> dict:
    """Tier 2: Common interview progressions – served from cache."""
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
def get_dashboard(user: dict = Depends(get_current_user)) -> dict:
    """Legacy: Full dashboard data in one call (kept for backwards compatibility)."""
    stats = _get_or_compute_stats()
    insights = _build_insights(stats)
    
    user_uid = user.get("uid", "")
    user_experiences = list(
        db.collection("interview_experiences")
        .where("created_by", "==", user_uid)
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
