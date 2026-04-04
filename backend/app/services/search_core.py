from __future__ import annotations

from difflib import SequenceMatcher
import math
import re
from collections import Counter
from typing import Optional

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]*")

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
    "your",
}

_CAMPUS_SYNONYMS: dict[str, list[str]] = {
    "oa": ["online assessment", "coding assessment", "aptitude round"],
    "dsa": ["data structures", "algorithms", "problem solving"],
    "hr": ["human resources", "behavioral", "fit interview"],
    "cn": ["computer networks", "networking", "tcp udp"],
    "os": ["operating system", "process thread", "scheduling"],
    "dbms": ["database", "sql", "normalization"],
    "sde": ["software engineer", "developer"],
}


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def tokenize_terms(text: str, *, max_terms: int = 10) -> list[str]:
    if not text:
        return []

    terms: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        if len(token) < 2 or token in _STOPWORDS:
            continue
        terms.append(token)

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
        if len(deduped) >= max_terms:
            break

    return deduped


def expand_query_terms(query_terms: list[str], *, max_terms: int = 20) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        normalized = normalize_text(term)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        expanded.append(normalized)

    for term in query_terms:
        _add(term)
        for synonym in _CAMPUS_SYNONYMS.get(term, []):
            _add(synonym)
            for token in tokenize_terms(synonym, max_terms=4):
                _add(token)
        if len(expanded) >= max_terms:
            break

    return expanded[:max_terms]


def expand_query_text(query: str) -> str:
    normalized = normalize_text(query)
    if not normalized:
        return normalized

    base_terms = tokenize_terms(normalized, max_terms=10)
    if not base_terms:
        return normalized

    expanded_terms = expand_query_terms(base_terms, max_terms=24)
    return " ".join(dict.fromkeys([normalized, *expanded_terms]).keys())


def _term_matches(term: str, field_text: str) -> bool:
    if not term or not field_text:
        return False

    if term in field_text:
        return True

    # Lightweight typo tolerance: fuzzy compare against nearby token lengths.
    if len(term) < 4:
        return False

    for token in field_text.split():
        if abs(len(token) - len(term)) > 1:
            continue
        if SequenceMatcher(a=term, b=token).ratio() >= 0.84:
            return True
    return False


def build_search_terms(
    *,
    company: str,
    role: str,
    round_name: str,
    difficulty: str,
    summary: str,
    raw_text: str,
    topics: list[str],
    question_texts: list[str],
    max_terms: int = 80,
) -> list[str]:
    weighted_text = [
        company,
        company,
        role,
        role,
        round_name,
        difficulty,
        summary,
        " ".join(topics),
        " ".join(question_texts[:20]),
        raw_text[:1800],
    ]

    counts: Counter[str] = Counter()
    for chunk in weighted_text:
        for token in tokenize_terms(chunk, max_terms=max_terms * 2):
            counts[token] += 1

    if not counts:
        return []

    ranked = [token for token, _ in counts.most_common(max_terms)]
    return ranked


def build_document_terms(data: dict, *, max_terms: int = 240) -> list[str]:
    weighted_chunks: list[str] = [
        str(data.get("company") or ""),
        str(data.get("company") or ""),
        str(data.get("role") or ""),
        str(data.get("round") or ""),
        str(data.get("difficulty") or ""),
        str(data.get("summary") or ""),
        " ".join(data.get("topics") or []),
        " ".join(data.get("search_terms") or []),
    ]

    question_bits: list[str] = []
    for entry in data.get("extracted_questions") or []:
        if isinstance(entry, dict):
            text = str(entry.get("question_text") or entry.get("question") or "").strip()
        else:
            text = str(entry).strip()
        if text:
            question_bits.append(text)
        if len(question_bits) >= 30:
            break
    if question_bits:
        weighted_chunks.append(" ".join(question_bits))

    raw_text = str(data.get("raw_text") or "")
    if raw_text:
        weighted_chunks.append(raw_text[:1200])

    terms: list[str] = []
    for chunk in weighted_chunks:
        if not chunk:
            continue
        terms.extend(tokenize_terms(chunk, max_terms=max_terms))
        if len(terms) >= max_terms:
            break

    return terms[:max_terms]


def bm25_score_documents(
    query_terms: list[str],
    document_terms: dict[str, list[str]],
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> dict[str, float]:
    if not query_terms or not document_terms:
        return {}

    query_tokens: list[str] = []
    seen_query: set[str] = set()
    for raw in query_terms:
        for token in tokenize_terms(raw, max_terms=6):
            if token in seen_query:
                continue
            seen_query.add(token)
            query_tokens.append(token)

    if not query_tokens:
        return {}

    normalized_docs: dict[str, list[str]] = {
        doc_id: [token for token in tokens if token]
        for doc_id, tokens in document_terms.items()
        if tokens
    }
    if not normalized_docs:
        return {}

    doc_count = len(normalized_docs)
    avgdl = sum(len(tokens) for tokens in normalized_docs.values()) / max(doc_count, 1)

    df: Counter[str] = Counter()
    query_set = set(query_tokens)
    for tokens in normalized_docs.values():
        present = set(tokens)
        for token in present:
            if token in query_set:
                df[token] += 1

    scores: dict[str, float] = {}
    for doc_id, tokens in normalized_docs.items():
        term_freq = Counter(tokens)
        doc_len = max(len(tokens), 1)
        score = 0.0
        for token in query_tokens:
            frequency = term_freq.get(token, 0)
            if frequency <= 0:
                continue

            token_df = df.get(token, 0)
            idf = math.log(1.0 + ((doc_count - token_df + 0.5) / (token_df + 0.5)))
            numerator = frequency * (k1 + 1.0)
            denominator = frequency + k1 * (1.0 - b + b * (doc_len / max(avgdl, 1.0)))
            score += idf * (numerator / max(denominator, 1e-9))

        if score > 0:
            scores[doc_id] = score

    if not scores:
        return {}

    maximum = max(scores.values())
    if maximum <= 0:
        return {doc_id: 0.0 for doc_id in scores}
    return {doc_id: value / maximum for doc_id, value in scores.items()}


def keyword_score(data: dict, query: str, query_terms: list[str]) -> tuple[float, Optional[str]]:
    if not query and not query_terms:
        return 0.0, None

    q_norm = normalize_text(query)
    fields = {
        "company": normalize_text(str(data.get("company", ""))),
        "role": normalize_text(str(data.get("role", ""))),
        "round": normalize_text(str(data.get("round", ""))),
        "difficulty": normalize_text(str(data.get("difficulty", ""))),
        "summary": normalize_text(str(data.get("summary", ""))),
        "topics": normalize_text(" ".join(data.get("topics") or [])),
        "raw": normalize_text(str(data.get("raw_text", ""))),
    }

    questions = []
    for entry in data.get("extracted_questions") or []:
        if isinstance(entry, dict):
            questions.append(normalize_text(str(entry.get("question_text") or entry.get("question") or "")))
        else:
            questions.append(normalize_text(str(entry)))

    score = 0.0
    matched_question: Optional[str] = None

    if q_norm:
        if q_norm in fields["company"]:
            score += 7.0
        if q_norm in fields["role"]:
            score += 5.5
        if q_norm in fields["summary"]:
            score += 4.0
        if q_norm in fields["topics"]:
            score += 4.0
        if q_norm in fields["raw"]:
            score += 2.0
        for question in questions:
            if q_norm and q_norm in question:
                score += 5.0
                matched_question = matched_question or question
                break

    for term in query_terms:
        if _term_matches(term, fields["company"]):
            score += 2.4
        if _term_matches(term, fields["role"]):
            score += 2.1
        if _term_matches(term, fields["round"]):
            score += 1.4
        if _term_matches(term, fields["difficulty"]):
            score += 1.0
        if _term_matches(term, fields["topics"]):
            score += 2.0
        if _term_matches(term, fields["summary"]):
            score += 1.6
        if _term_matches(term, fields["raw"]):
            score += 0.5

        for question in questions:
            if _term_matches(term, question):
                score += 1.8
                matched_question = matched_question or question
                break

    normalized = min(score / 18.0, 1.0)
    return normalized, matched_question
