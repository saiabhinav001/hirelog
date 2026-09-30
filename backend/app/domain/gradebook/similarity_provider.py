"""JPlag-style code-similarity provider adapter.

Mirrors the exact honesty tier as `judge/provider.py`'s `Judge0HttpAdapter`:
a real `Protocol`, a `DeterministicFakeSimilarityProvider` that NEVER
performs real comparison (every result is explicitly scripted by the
caller), and a real `JPlagCliAdapter` that raises until an operator
actually configures and validates a real JPlag jar — never a silent
"good enough" text-diff standing in for genuine tokenized/AST-aware
similarity detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class SimilaritySubmissionInput:
    submission_id: str
    uid: str
    language: str
    source_code: str


@dataclass(frozen=True)
class SimilarityMatchResult:
    uid_a: str
    submission_id_a: str
    uid_b: str
    submission_id_b: str
    similarity_percent: float
    matched_spans: list[dict]


class SimilarityProvider(Protocol):
    def compare(self, submissions: list[SimilaritySubmissionInput]) -> list[SimilarityMatchResult]:
        """Pairwise-compares submissions the CALLER has already grouped by
        (question, language) and stripped of starter code — a provider
        never needs to know about grouping or exclusion policy."""
        ...


class DeterministicFakeSimilarityProvider:
    """NEVER performs real similarity analysis — every result is explicitly
    scripted by the caller (tests), keyed by a pair of submission ids. This
    mirrors `DeterministicFakeJudgeProvider`'s contract: a "smart" fake that
    approximates real comparison would misrepresent what similarity review
    is actually validated to do in this environment."""

    def __init__(self) -> None:
        self._scripted: dict[tuple[str, str], SimilarityMatchResult] = {}

    def script(self, submission_id_a: str, submission_id_b: str, result: SimilarityMatchResult) -> None:
        key = tuple(sorted((submission_id_a, submission_id_b)))
        self._scripted[key] = result

    def compare(self, submissions: list[SimilaritySubmissionInput]) -> list[SimilarityMatchResult]:
        results = []
        for i in range(len(submissions)):
            for j in range(i + 1, len(submissions)):
                key = tuple(sorted((submissions[i].submission_id, submissions[j].submission_id)))
                scripted = self._scripted.get(key)
                if scripted is not None:
                    results.append(scripted)
        return results


class JPlagCliAdapter:
    """Real-provider adapter interface for a JPlag CLI installation. NOT
    wired to any real JPlag jar — none is installed/available in this
    environment. Calling this raises until an operator supplies a
    `jplag_jar_path` AND actually runs it against known-similar/known-
    dissimilar fixtures to confirm the parsed output format matches this
    adapter's expectations (this class cannot verify that for you — see
    docs/adr/0014 deployment requirements)."""

    def __init__(self, *, jplag_jar_path: Optional[str] = None, java_executable: str = "java") -> None:
        self._jplag_jar_path = jplag_jar_path
        self._java_executable = java_executable

    def compare(self, submissions: list[SimilaritySubmissionInput]) -> list[SimilarityMatchResult]:
        if not self._jplag_jar_path:
            raise NotImplementedError(
                "JPlagCliAdapter is not configured — set a real jplag_jar_path "
                "(see docs/adr/0014 deployment requirements)."
            )
        raise NotImplementedError("Real JPlag CLI invocation not implemented — no provider configured/tested.")
