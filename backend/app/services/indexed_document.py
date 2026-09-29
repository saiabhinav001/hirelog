from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_topics(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        topic = _clean_text(item)
        if not topic:
            continue
        topic = topic.upper()
        if topic in seen:
            continue
        seen.add(topic)
        cleaned.append(topic)
    return cleaned


def _normalize_search_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        term = _clean_text(item).lower()
        if not term:
            continue
        if term in seen:
            continue
        seen.add(term)
        cleaned.append(term)
        if len(cleaned) >= 120:
            break
    return cleaned


def _normalize_questions(value: Any) -> tuple[list[str], int, int]:
    if not isinstance(value, list):
        return [], 0, 0

    questions: list[str] = []
    user_count = 0
    ai_count = 0

    for item in value:
        if isinstance(item, dict):
            text = _clean_text(item.get("question_text") or item.get("question") or "")
            source = str(item.get("source") or "ai").lower()
            if source == "user":
                user_count += 1
            else:
                ai_count += 1
        else:
            text = _clean_text(item)
            ai_count += 1

        if not text:
            continue
        questions.append(text)

    return questions, user_count, ai_count


def _to_unix_millis(value: Any) -> int:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return 0
        try:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0

    return 0


def build_index_document(
    *,
    doc_id: str,
    source: dict,
    embedding: list[float] | None,
) -> dict:
    company = _clean_text(source.get("company"))
    role = _clean_text(source.get("role"))
    round_name = _clean_text(source.get("round"))
    difficulty = _clean_text(source.get("difficulty")).title()
    summary = _clean_text(source.get("summary"))
    raw_text = _clean_text(source.get("raw_text"))
    topics = _normalize_topics(source.get("topics"))
    search_terms = _normalize_search_terms(source.get("search_terms"))
    questions, user_question_count, ai_question_count = _normalize_questions(
        source.get("extracted_questions")
    )
    is_anonymous = bool(source.get("is_anonymous", False))
    show_name = bool(source.get("show_name", False))
    contributor_visibility = "anonymous" if is_anonymous else ("named" if show_name else "hidden")

    # Keep indexed payload bounded for predictable memory and query costs.
    question_blob = " | ".join(questions[:40])
    raw_preview = raw_text[:2200]
    text_blob = " ".join(
        value
        for value in [
            company,
            role,
            round_name,
            difficulty,
            summary,
            " ".join(topics),
            " ".join(search_terms),
            question_blob,
            raw_preview,
        ]
        if value
    )

    year_value = source.get("year")
    year = int(year_value) if isinstance(year_value, int) else 0

    return {
        "id": doc_id,
        "company": company,
        "role": role,
        "year": year,
        "round": round_name,
        "topics": topics,
        "difficulty": difficulty,
        "summary": summary,
        "question_text": question_blob,
        "search_terms": search_terms,
        "raw_text": raw_preview,
        "text_blob": text_blob,
        "is_active": bool(source.get("is_active", True)),
        "is_anonymous": is_anonymous,
        "show_name": show_name,
        "contributor_visibility": contributor_visibility,
        "allow_contact": bool(source.get("allow_contact", False)),
        "nlp_status": str(source.get("nlp_status") or "unknown").lower(),
        "has_user_questions": user_question_count > 0,
        "has_ai_questions": ai_question_count > 0,
        "created_at": _to_unix_millis(source.get("created_at")),
        "embedding": embedding or [],
    }
