"""Source-derived topic suggestions (Prompt 5, DOM-05, ADR 0021).

Deterministic and extractive: headings found in the parsed sources and frequent content
words, each with the passages that evidence it. Suggestions are only suggestions - they are
never stored on a blueprint unless the teacher submits them as confirmed topics.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from app.domain.generation.grounding import detect_injection
from app.domain.kt.models import ExtractedVersion

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{3,}")
_STOP = frozenset(
    "this that with from have been were will would could should their there which about into than then them also "
    "such these those when where while what your more most other some only over under between within using used "
    "each both many much very well can may might must shall chapter section page figure table example examples".split()
)


@dataclass
class TopicSuggestion:
    topic: str
    score: int
    kind: str  # "heading" | "term"
    evidence: list[dict] = field(default_factory=list)  # {source_id, anchor_id, page}

    def to_dict(self) -> dict:
        return {"topic": self.topic, "score": self.score, "kind": self.kind, "evidence": self.evidence}


def suggest_topics(sources: Sequence[tuple[str, ExtractedVersion]], *, limit: int = 12, evidence_per_topic: int = 3) -> list[TopicSuggestion]:
    headings: Counter[str] = Counter()
    heading_evidence: dict[str, list[dict]] = defaultdict(list)
    terms: Counter[str] = Counter()
    term_evidence: dict[str, list[dict]] = defaultdict(list)

    for source_id, version in sources:
        for anchor in version.anchors:
            if detect_injection(anchor.text):
                continue  # instruction-like passages never feed suggestions
            ref = {"source_id": source_id, "anchor_id": anchor.anchor_id, "page": anchor.page}
            if anchor.section:
                key = " ".join(anchor.section.split())
                headings[key] += 1
                if len(heading_evidence[key]) < evidence_per_topic:
                    heading_evidence[key].append(ref)
            for word in {w.lower() for w in _WORD.findall(anchor.text)} - _STOP:
                terms[word] += 1
                if len(term_evidence[word]) < evidence_per_topic:
                    term_evidence[word].append(ref)

    suggestions = [
        TopicSuggestion(topic=h, score=5 * n, kind="heading", evidence=heading_evidence[h])
        for h, n in headings.items() if 3 <= len(h) <= 120
    ]
    seen = {s.topic.lower() for s in suggestions}
    suggestions += [
        TopicSuggestion(topic=w, score=n, kind="term", evidence=term_evidence[w])
        for w, n in terms.items() if n >= 3 and w not in seen
    ]
    suggestions.sort(key=lambda s: (-s.score, s.topic))
    return suggestions[:limit]
