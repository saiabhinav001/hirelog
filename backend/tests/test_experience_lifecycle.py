from __future__ import annotations

from app.api.routes.experiences import (
    _build_user_question_objects,
    _collect_user_questions_for_reprocess,
    _compute_search_terms,
)


def test_build_user_question_objects_preserves_source() -> None:
    rows = _build_user_question_objects([
        "What is CAP theorem?",
        "Explain process vs thread",
    ], now="2026-04-04T00:00:00+00:00")

    assert len(rows) == 2
    assert all(item["source"] == "user" for item in rows)
    assert all(item["confidence"] == 1.0 for item in rows)


def test_collect_user_questions_prefers_nested() -> None:
    data = {
        "questions": {
            "user_provided": [
                {"question_text": "Describe Kafka partitions."},
                {"question_text": ""},
            ]
        },
        "extracted_questions": [
            {"source": "user", "question_text": "fallback should not be used"}
        ],
    }

    questions = _collect_user_questions_for_reprocess(data)
    assert len(questions) == 1
    assert questions[0]["question_text"] == "Describe Kafka partitions."


def test_compute_search_terms_has_signal() -> None:
    terms = _compute_search_terms(
        company="Example Co",
        role="Backend Engineer",
        round_name="System Design",
        difficulty="Hard",
        summary="Focused on caching and consistency tradeoffs",
        raw_text="We discussed distributed systems, retries, and data modeling in depth.",
        topics=["System Design", "DBMS"],
        questions_flat=[
            {"question_text": "How would you design a URL shortener?"},
            {"question_text": "What is eventual consistency?"},
        ],
    )

    assert terms
    assert "design" in terms or "consistency" in terms
