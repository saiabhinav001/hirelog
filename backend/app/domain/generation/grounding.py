"""Source grounding for generation requests (Prompt 5, ADR 0021).

Builds the `approved_source_bundle` a provider receives from the REAL extracted text of
approved sources: each anchor must exist in the source's pinned extracted version, the
excerpt is that anchor's stored text (never a client-supplied string), and the bundle
records exact source id / version number / anchor id / page / section.

Source text is untrusted data. Passages that read like instructions to a model are
withheld from the bundle and reported to the teacher instead of being sent; everything
else is still treated as data downstream (structured request, schema-checked output).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.domain.generation.models import SourceAnchorWithExcerpt
from app.domain.kt.models import ApprovedSource, ExtractedAnchor, ExtractedVersion

MAX_BUNDLE_ANCHORS = 40
MAX_BUNDLE_CHARS = 60_000
AUTO_SELECT_TOKENS = {"", "auto", "full-document"}  # `full-document` is what the earlier UI sent

# Phrases that address a model/assistant rather than describe a subject.
_INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any)\b.{0,40}\b(instruction|prompt|rule|direction)s?\b", re.I | re.S),
    "role_reassignment": re.compile(r"\b(you are now|act as|pretend to be|from now on you)\b", re.I),
    "prompt_disclosure": re.compile(r"\b(system prompt|developer message|reveal (your|the) (instructions|prompt))\b", re.I),
    "approval_demand": re.compile(r"\b(mark|set|flag|return|output)\b.{0,30}\b(approved|published|verified|correct for (all|every))\b", re.I | re.S),
    "answer_key_demand": re.compile(r"\b(the correct answer is always|always (choose|select|answer)|every question'?s? answer)\b", re.I),
    "exfiltration": re.compile(r"\b(send|post|exfiltrate|forward)\b.{0,40}\b(to|at)\b.{0,20}(https?://|\S+@\S+)", re.I | re.S),
    "chat_markup": re.compile(r"(<\|?(system|assistant|im_start|im_end)\|?>|\[/?INST\]|^\s*(system|assistant)\s*:)", re.I | re.M),
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("the a an of to in and or for with on is are be as at by from that this it its into over under".split())


class GroundingError(Exception):
    def __init__(self, code: str, message: str, *, detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def detect_injection(text: str) -> list[str]:
    return [name for name, pattern in _INJECTION_PATTERNS.items() if pattern.search(text)]


def sanitize_excerpt(text: str) -> str:
    return _CONTROL.sub("", text).strip()


def terms_from(*groups: Sequence[str]) -> set[str]:
    terms: set[str] = set()
    for group in groups:
        for item in group:
            terms.update(w for w in _WORD.findall(item.lower().replace("_", " ")) if len(w) > 2 and w not in _STOP)
    return terms


def _score(anchor: ExtractedAnchor, terms: set[str]) -> int:
    if not terms:
        return 0
    haystack = set(_WORD.findall(f"{anchor.section or ''} {anchor.text}".lower()))
    return len(terms & haystack)


@dataclass
class GroundedBundle:
    anchors: list[SourceAnchorWithExcerpt] = field(default_factory=list)
    source_versions: dict[str, int] = field(default_factory=dict)
    excluded: list[dict] = field(default_factory=list)  # withheld from the provider, shown to the teacher
    truncated: bool = False

    @property
    def sha256(self) -> str:
        canonical = json.dumps([a.model_dump(mode="json") for a in self.anchors], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _to_bundle_anchor(source_id: str, version: ExtractedVersion, anchor: ExtractedAnchor) -> SourceAnchorWithExcerpt:
    return SourceAnchorWithExcerpt(
        source_id=source_id, source_version=version.version_number, anchor_id=anchor.anchor_id,
        excerpt=sanitize_excerpt(anchor.text)[:4000], page=anchor.page, section=anchor.section,
    )


def build_bundle(
    resolved: Sequence[tuple[ApprovedSource, ExtractedVersion, Optional[list[str]]]],
    *,
    terms: set[str],
    max_anchors: int = MAX_BUNDLE_ANCHORS,
    max_chars: int = MAX_BUNDLE_CHARS,
) -> GroundedBundle:
    """`resolved` = (approved source, its pinned extracted version, requested anchor ids or None/auto).

    Explicitly requested anchors must exist (`unknown_anchor` otherwise). With none requested the
    passages most relevant to `terms` (objectives + confirmed topics) are chosen, in document
    order within equal relevance. The budget is shared evenly between sources."""
    bundle = GroundedBundle()
    if not resolved:
        raise GroundingError("no_sources", "Choose at least one approved source.")
    per_source = max(1, max_anchors // len(resolved))

    for source, version, requested in resolved:
        by_id = {a.anchor_id: a for a in version.anchors}
        explicit = [a for a in (requested or []) if a not in AUTO_SELECT_TOKENS]
        if explicit:
            unknown = [a for a in explicit if a not in by_id]
            if unknown:
                raise GroundingError(
                    "unknown_anchor", f"Source {source.id} has no passage(s) {unknown[:5]} in its approved version.",
                    detail={"source_id": source.id, "anchor_ids": unknown[:20]},
                )
            candidates = [by_id[a] for a in dict.fromkeys(explicit)]
        else:
            ranked = sorted(enumerate(version.anchors), key=lambda item: (-_score(item[1], terms), item[0]))
            candidates = [a for _, a in ranked]

        taken = 0
        for anchor in candidates:
            if taken >= per_source:
                bundle.truncated = bundle.truncated or not explicit and len(candidates) > per_source
                break
            text = sanitize_excerpt(anchor.text)
            if not text:
                continue
            flags = detect_injection(text)
            if flags:
                bundle.excluded.append({"source_id": source.id, "anchor_id": anchor.anchor_id, "reason": "looks_like_instructions", "flags": flags})
                continue
            if sum(len(a.excerpt) for a in bundle.anchors) + len(text) > max_chars:
                bundle.truncated = True
                break
            bundle.anchors.append(_to_bundle_anchor(source.id, version, anchor))
            taken += 1
        bundle.source_versions[source.id] = version.version_number

    if not bundle.anchors:
        raise GroundingError(
            "no_usable_passages",
            "None of the selected passages can be sent (empty, or withheld because they read like instructions).",
            detail={"excluded": bundle.excluded[:20]},
        )
    return bundle
