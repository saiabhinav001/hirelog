from __future__ import annotations

import re
from typing import List

import numpy as np
import spacy
from sentence_transformers import SentenceTransformer

from app.core.config import settings


QUESTION_PREFIXES = (
    "q:",
    "q1",
    "q2",
    "q3",
    "q4",
    "q5",
    "q6",
    "q7",
    "q8",
    "q9",
    "q10",
    "question",
    "asked",
    "they asked",
    "we were asked",
)

# ── Section headers / labels that should NEVER be treated as questions ──────
_HEADER_PATTERNS = re.compile(
    r"^("
    r"questions?\s*asked"
    r"|round\s*\d+"
    r"|technical\s*round"
    r"|hr\s*round"
    r"|coding\s*round"
    r"|managerial\s*round"
    r"|online\s*(test|assessment|round)"
    r"|tips?"
    r"|advice"
    r"|overall\s*difficulty"
    r"|summary"
    r"|experience"
    r"|interview\s*process"
    r"|preparation"
    r"|project\s*discussion"
    r"|topics?\s*covered"
    r"|rounds?"
    r")\s*:?\s*$",
    re.IGNORECASE,
)

# Minimum length to be considered a real question (filters out stubs like "Q1?")
_MIN_QUESTION_LENGTH = 12

CODING_KEYWORDS = [
    "algorithm",
    "array",
    "string",
    "linked list",
    "tree",
    "graph",
    "dp",
    "dynamic programming",
    "recursion",
    "binary search",
    "hash",
    "stack",
    "queue",
    "heap",
    "trie",
    "leetcode",
    "complexity",
    "optimize",
    "coding",
    "sort",
    "search",
    "traversal",
    "bfs",
    "dfs",
    "sliding window",
    "two pointer",
]

THEORY_KEYWORDS = [
    "dbms",
    "database",
    "sql",
    "normalization",
    "transaction",
    "index",
    "operating system",
    "os",
    "process",
    "thread",
    "deadlock",
    "scheduling",
    "network",
    "tcp",
    "udp",
    "http",
    "system design",
    "oop",
    "polymorphism",
    "inheritance",
    "encapsulation",
    "cn",
]

HR_KEYWORDS = [
    "introduce yourself",
    "tell me about yourself",
    "strength",
    "weakness",
    "why",
    "team",
    "conflict",
    "leadership",
    "challenge",
    "goal",
    "salary",
    "culture",
    "project",
    "motivation",
]

TOPIC_KEYWORDS = {
    "DSA": CODING_KEYWORDS,
    "DBMS": ["dbms", "database", "sql", "normalization", "transaction", "index", "join", "query"],
    "OS": ["operating system", "os", "process", "thread", "deadlock", "scheduling", "paging", "virtual memory"],
    "CN": ["network", "cn", "tcp", "udp", "http", "dns", "latency", "handshake", "routing"],
    "OOP": ["oop", "class", "object", "inheritance", "polymorphism", "encapsulation", "abstraction", "interface", "overloading", "overriding"],
    "System Design": ["system design", "scalability", "caching", "load balancer", "microservice", "rest api", "architecture"],
    "HR": HR_KEYWORDS,
}


def _classify_question_topic(text: str) -> str:
    """Classify a single question into its most specific topic."""
    lowered = text.lower()
    # Score each topic by keyword hits; return the best match.
    best_topic = "General"
    best_score = 0
    for topic, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in lowered)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic


def _is_section_header(text: str) -> bool:
    """Return True if the text looks like a section header, not a question."""
    stripped = text.strip().rstrip(":").strip()
    if _HEADER_PATTERNS.match(stripped):
        return True
    # Very short labels like "Questions:" or "Round 1:"
    if len(stripped) < 8 and not stripped.endswith("?"):
        return True
    return False


def _compute_confidence(text: str) -> float:
    """Heuristic confidence score for question extraction quality.
    
    1.0 = ends with '?' and is reasonably long (definite question)
    0.9 = starts with a question prefix (Q1:, asked, etc.)
    0.7 = extracted from bullet point / heuristic match
    """
    stripped = text.strip()
    if stripped.endswith("?") and len(stripped) >= _MIN_QUESTION_LENGTH:
        return 1.0
    lowered = stripped.lower()
    if lowered.startswith(QUESTION_PREFIXES):
        return 0.9
    return 0.7


def _normalize_question(text: str) -> str:
    """Strip leading prefixes like 'Q1:', 'Q:', bullet markers etc."""
    cleaned = text.strip()
    # Remove leading "Q1:", "Q2:", "Q:" etc.
    cleaned = re.sub(r"^[Qq]\d*[\s:.\-]+", "", cleaned).strip()
    # Remove leading "Question:", "Asked:", etc.
    cleaned = re.sub(
        r"^(question|asked|they\s+asked|we\s+were\s+asked)\s*[:.\-]+\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    # Capitalize first letter
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


class NlpPipeline:
    def __init__(self) -> None:
        self.model = SentenceTransformer(settings.EMBEDDING_MODEL)
        self.nlp = spacy.load("en_core_web_sm")

    def clean_text(self, text: str) -> str:
        # Normalize whitespace so downstream NLP and embeddings stay consistent.
        cleaned = re.sub(r"\s+", " ", text).strip()
        return cleaned

    def embed(self, text: str) -> np.ndarray:
        embedding = self.model.encode(text, normalize_embeddings=True)
        return np.asarray(embedding, dtype="float32")

    def extract_questions(self, raw_text: str, sentences: List[str]) -> list:
        """Extract actual interview questions from raw text.

        Returns a list of structured dicts:
            { question_text, topic, category, confidence }

        Filters OUT section headers, labels, and stubs.
        Deduplicates by normalized lowercase form.
        """
        candidates: list[str] = []

        # Pass 1: line-based extraction (catches bullet lists, Q1: prefixes)
        for line in raw_text.splitlines():
            stripped = line.strip("-• \t").strip()
            if not stripped:
                continue
            if _is_section_header(stripped):
                continue
            lowered = stripped.lower()
            if stripped.endswith("?") or lowered.startswith(QUESTION_PREFIXES):
                candidates.append(stripped)

        # Pass 2: sentence-based extraction (catches inline questions)
        for sentence in sentences:
            stripped = sentence.strip()
            if stripped.endswith("?") and not _is_section_header(stripped):
                candidates.append(stripped)

        # Normalize, deduplicate, and filter
        seen: set[str] = set()
        deduped: list[str] = []
        for raw_q in candidates:
            normalized = _normalize_question(raw_q)
            if len(normalized) < _MIN_QUESTION_LENGTH:
                continue
            dedup_key = re.sub(r"\s+", " ", normalized.lower().strip("? "))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            deduped.append(normalized)

        # Build structured output
        results: list[dict] = []
        for q_text in deduped[:20]:
            topic = _classify_question_topic(q_text)
            confidence = _compute_confidence(q_text)
            results.append({
                "question_text": q_text,
                "topic": topic,
                "category": topic.lower() if topic != "General" else "theory",
                "confidence": confidence,
                "question_type": "extracted",
                "source": "ai",
                "added_later": False,
                # Legacy field kept for backwards compatibility with frontend
                "question": q_text,
            })
        return results

    def classify_topics(self, text: str) -> List[str]:
        lowered = text.lower()
        topics = []
        for topic, keywords in TOPIC_KEYWORDS.items():
            if any(keyword in lowered for keyword in keywords):
                topics.append(topic)
        return topics

    def generate_summary(self, sentences: List[str], doc_embedding: np.ndarray) -> str:
        if not sentences:
            return "No summary available yet. Add more detail to improve the summary."
        if len(sentences) <= 2:
            return " ".join(sentences)

        sentence_embeddings = self.model.encode(sentences, normalize_embeddings=True)
        sentence_embeddings = np.asarray(sentence_embeddings, dtype="float32")
        scores = np.dot(sentence_embeddings, doc_embedding)
        top_indices = scores.argsort()[-3:]
        top_indices_sorted = sorted(top_indices.tolist())
        summary_sentences = [sentences[i] for i in top_indices_sorted]
        summary = " ".join(summary_sentences)
        return summary

    def process(self, raw_text: str) -> dict:
        cleaned = self.clean_text(raw_text)
        doc = self.nlp(cleaned)
        sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]
        doc_embedding = self.embed(cleaned)
        questions = self.extract_questions(raw_text, sentences)
        topics = self.classify_topics(cleaned)
        summary = self.generate_summary(sentences, doc_embedding)
        return {
            "cleaned_text": cleaned,
            "questions": questions,
            "topics": topics,
            "summary": summary,
            "embedding": doc_embedding,
        }

    def classify_single_question(self, question_text: str) -> dict:
        """Classify a single question through the NLP pipeline.

        Used by the "add questions later" feature so that every
        question — whether extracted at submission or added later —
        goes through the same classification path.
        """
        normalized = _normalize_question(question_text)
        topic = _classify_question_topic(normalized)
        confidence = _compute_confidence(normalized)
        return {
            "question_text": normalized,
            "topic": topic,
            "category": topic.lower() if topic != "General" else "theory",
            "confidence": confidence,
            "question_type": "added_later",
            "source": "user",
            "question": normalized,  # Legacy field
        }


pipeline = NlpPipeline()
