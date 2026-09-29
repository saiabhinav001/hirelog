from __future__ import annotations

from app.services.search_core import bm25_score_documents, build_search_terms, keyword_score


def test_keyword_score_prioritizes_exact_question_match() -> None:
    doc = {
        "company": "Example Co",
        "role": "SDE",
        "summary": "Interview focused on operating systems and networking fundamentals",
        "raw_text": "The panel asked OS scheduling and TCP connection questions.",
        "topics": ["OS", "CN"],
        "extracted_questions": [
            {"question_text": "Explain process scheduling in operating systems", "source": "ai"},
            {"question_text": "What is TCP three way handshake", "source": "ai"},
        ],
    }

    score, matched = keyword_score(doc, "process scheduling", ["process", "scheduling"])

    assert score > 0
    assert matched is not None
    assert "scheduling" in matched.lower()


def test_build_search_terms_includes_core_metadata() -> None:
    terms = build_search_terms(
        company="Example Co",
        role="Data Analyst",
        round_name="SQL Round",
        difficulty="Medium",
        summary="Heavy focus on joins and indexing",
        raw_text="Included SQL joins, normalization, and indexing discussions",
        topics=["DBMS"],
        question_texts=["How do clustered and non-clustered indexes differ?"],
    )

    assert terms
    assert "sql" in terms
    assert "joins" in terms


def test_bm25_prefers_more_relevant_document() -> None:
    scores = bm25_score_documents(
        ["system design", "caching", "latency"],
        {
            "doc-a": ["system", "design", "caching", "latency", "distributed", "system"],
            "doc-b": ["dbms", "transactions", "joins", "normalization"],
        },
    )

    assert scores
    assert scores.get("doc-a", 0.0) > scores.get("doc-b", 0.0)
