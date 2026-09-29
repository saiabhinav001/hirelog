"""Provider-neutral generation adapter.

`GenerationProvider.generate` returns a `ProviderResponse` shaped exactly
like the JSON contract in "Backend model prompt A" of the implementation
pack. The provider is UNTRUSTED input from here on — every field is
re-validated by `generation/validation.py` before a candidate is persisted;
nothing the provider returns is trusted at face value (e.g. it cannot set
`status="published"` because `CandidateStatus` values above `draft`/
`needs_revision`/`ready_for_human_review` are simply never accepted from
provider output — see `validation.py::sanitize_provider_status`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Protocol
from uuid import uuid4

from app.domain.generation.models import GenerationRequest


@dataclass
class ProviderResponse:
    request_id: str
    status: str  # "draft" | "insufficient_source"
    candidates: list[dict] = field(default_factory=list)
    shortfall: int = 0
    issues: list[str] = field(default_factory=list)


class ProviderTimeoutError(Exception):
    pass


class ProviderError(Exception):
    """Raised for malformed/non-schema-conforming provider output."""


class GenerationProvider(Protocol):
    def generate(self, request: GenerationRequest) -> ProviderResponse: ...


class DeterministicFakeProvider:
    """Test/dev provider — produces schema-conforming, deterministic output
    from `request.deterministic_seed`. No network call, no real model. Used
    by default when no real provider is configured (contract: "no live
    generation during student attempts" is trivially true for this provider
    since it never runs during an attempt in the first place — it is only
    ever invoked from the faculty authoring path)."""

    def __init__(self, *, force_insufficient: bool = False, force_timeout: bool = False) -> None:
        self._force_insufficient = force_insufficient
        self._force_timeout = force_timeout

    def generate(self, request: GenerationRequest) -> ProviderResponse:
        if self._force_timeout:
            raise ProviderTimeoutError("fake provider forced timeout")

        if not request.approved_source_bundle:
            return ProviderResponse(
                request_id=request.id, status="insufficient_source",
                candidates=[], shortfall=request.requested_variant_count,
                issues=["no approved_source_bundle supplied"],
            )
        if self._force_insufficient:
            return ProviderResponse(
                request_id=request.id, status="insufficient_source",
                candidates=[], shortfall=request.requested_variant_count,
                issues=["forced insufficient_source for test"],
            )

        rng = random.Random(request.deterministic_seed)
        candidates = []
        for i in range(request.requested_variant_count):
            local_id = f"cand-{i}-{uuid4().hex[:8]}"
            anchor = request.approved_source_bundle[i % len(request.approved_source_bundle)]
            candidates.append(
                {
                    "local_candidate_id": local_id,
                    "family_id": f"fam-{request.slot_index}",
                    "item_type": request.item_type.value,
                    "objective_ids": request.objective_ids,
                    "difficulty": request.difficulty,
                    "marks": request.marks,
                    "estimated_minutes": request.target_minutes,
                    "variant_parameters": {"variant_index": i, "roll": rng.randint(1, 1000)},
                    "uniqueness_signature": f"sig-{request.deterministic_seed}-{i}",
                    "public_prompt": f"[fake] Question variant {i} for slot {request.slot_index}",
                    "constraints": {},
                    "public_examples": [],
                    "options": [] if request.item_type != "single_choice" else ["A", "B", "C", "D"],
                    "private_expected_answer": "A" if request.item_type == "single_choice" else "fake-answer",
                    "private_explanation": "fake explanation",
                    "marking_rubric": {"points": request.marks},
                    "supporting_anchors": [
                        {
                            "source_id": anchor.source_id,
                            "source_version": anchor.source_version,
                            "anchor_id": anchor.anchor_id,
                        }
                    ],
                    "equivalence_rationale": "deterministic fake — same objective, varied index",
                    "reference_solutions": (
                        {lang: "def solve():\n    return None" for lang in request.permitted_languages}
                        if request.item_type == "coding"
                        else {}
                    ),
                    "public_tests": [] if request.item_type == "coding" else [],
                    "validation_concerns": [],
                    "status": "draft",
                }
            )
        return ProviderResponse(request_id=request.id, status="draft", candidates=candidates, shortfall=0)


class HttpGenerationProviderAdapter:
    """Configurable real-provider adapter interface.

    NOT wired to any live endpoint in this environment — no provider
    endpoint/credentials were supplied. Calling `generate` raises until an
    institution configures `base_url`/`api_key`, per master prompt: "surface
    missing institutional/provider inputs as bounded setup requirements."
    """

    def __init__(self, *, base_url: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self._base_url = base_url
        self._api_key = api_key

    def generate(self, request: GenerationRequest) -> ProviderResponse:
        if not self._base_url or not self._api_key:
            raise ProviderError(
                "HttpGenerationProviderAdapter is not configured — set a real "
                "provider base_url/api_key (see docs/adr/0009 setup requirements)."
            )
        raise NotImplementedError("Real provider HTTP call not implemented — no provider configured/tested.")
