"""Structured validation for provider-returned (and teacher-edited) candidates.

This is where every non-negotiable from contract #7 is enforced in code, not just
prompt text: source anchors must actually exist in the supplied bundle, duplicates are
rejected, objective/difficulty/marks must match the request, the answer key must be
consistent with the options/rubric, and - critically - whatever `status` field a
(possibly compromised or malfunctioning) provider claims is IGNORED. The status returned
here is always recomputed from the issues found; a provider cannot self-approve.

Prompt 5 (ADR 0021) added: answer-key consistency, near-duplicate detection,
option-shuffle safety, rubric/mark totals, hidden-test structure, and a single
`validate_candidate` entry point shared by generation and teacher edits.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Mapping, Optional
from uuid import uuid4

from pydantic import ValidationError

from app.domain.generation.models import (
    CandidateStatus,
    GenerationCandidate,
    GenerationRequest,
    ValidationIssue,
)

NEAR_DUPLICATE_THRESHOLD = 0.8
MIN_PROMPT_CHARS = 10
MAX_OPTIONS = 10
_WORD = re.compile(r"[a-z0-9]+")
# Text that only makes sense in one fixed position - unsafe once options are shuffled per student.
_POSITIONAL = re.compile(
    r"\b(all|none|both|neither|any)\s+of\s+the\s+(above|below|following)\b"
    r"|\b(options?|choices?|answers?)\s+\(?[a-e]\)?(\s*(,|and|or|&)\s*\(?[a-e]\)?)?\b"
    r"|\b(above|below)\b",
    re.IGNORECASE,
)


def _blocker(field: str, reason: str) -> ValidationIssue:
    return ValidationIssue(severity="blocker", field=field, reason=reason)


def _major(field: str, reason: str) -> ValidationIssue:
    return ValidationIssue(severity="major", field=field, reason=reason)


def _minor(field: str, reason: str) -> ValidationIssue:
    return ValidationIssue(severity="minor", field=field, reason=reason)


# -- text similarity ---------------------------------------------------------------------------

def prompt_shingles(text: str, size: int = 3) -> frozenset[str]:
    tokens = _WORD.findall(text.lower())
    if len(tokens) < size:
        return frozenset({" ".join(tokens)}) if tokens else frozenset()
    return frozenset(" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1))


def similarity(a: str, b: str) -> float:
    left, right = prompt_shingles(a), prompt_shingles(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def find_near_duplicate(prompt: str, others: Mapping[str, str], *, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> Optional[tuple[str, float]]:
    best: Optional[tuple[str, float]] = None
    for other_id, other_prompt in others.items():
        score = similarity(prompt, other_prompt)
        if score >= threshold and (best is None or score > best[1]):
            best = (other_id, score)
    return best


# -- item consistency --------------------------------------------------------------------------

def _option_problems(options: list) -> tuple[list[ValidationIssue], list[str]]:
    """(issues, keys). Options must be {key, text} objects with unique keys and unique texts."""
    issues: list[ValidationIssue] = []
    keys: list[str] = []
    texts: list[str] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict) or not str(option.get("key", "")).strip() or not str(option.get("text", "")).strip():
            issues.append(_blocker("options", f"option {index} must be an object with a non-empty key and text"))
            continue
        keys.append(str(option["key"]).strip())
        texts.append(" ".join(str(option["text"]).lower().split()))
    if len(set(keys)) != len(keys):
        issues.append(_blocker("options", "option keys must be unique"))
    if len(set(texts)) != len(texts):
        issues.append(_blocker("options", "two options have the same text"))
    if len(options) > MAX_OPTIONS:
        issues.append(_blocker("options", f"at most {MAX_OPTIONS} options are supported"))
    return issues, keys


def _parse_expected_keys(expected: Optional[str]) -> list[str]:
    return [part.strip() for part in (expected or "").split(",") if part.strip()]


_MAX_NOTES = 20
_MAX_NOTE_CHARS = 500
_CODING_CONTRACT_KEYS = ("input_output_spec", "complexity")  # `constraints` is student-visible: no boundary cases here


def _text_list(value) -> list[str]:
    """Provider-reported notes: a bounded list of short strings; anything else is ignored."""
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:_MAX_NOTE_CHARS] for v in value if isinstance(v, (str, int, float)) and str(v).strip()][:_MAX_NOTES]


def _forbidden_topic_hits(candidate: GenerationCandidate, request: GenerationRequest) -> list[str]:
    constraints = request.family_constraints
    if constraints is None or not constraints.forbidden_topics:
        return []
    option_text = " ".join(str(o.get("text", "")) for o in candidate.options if isinstance(o, dict))
    haystack = f"{candidate.public_prompt} {option_text}".lower()
    return [topic for topic in constraints.forbidden_topics if topic.strip().lower() in haystack]


def _test_group_problems(field: str, groups: list) -> list[ValidationIssue]:
    problems: list[ValidationIssue] = []
    for index, group in enumerate(groups):
        tests = group.get("tests") if isinstance(group, dict) else None
        if not isinstance(tests, list) or not tests:
            problems.append(_major(field, f"test group {index} has no tests"))
            continue
        for test in tests:
            if not isinstance(test, dict) or "stdin" not in test or "expected_output" not in test:
                problems.append(_major(field, f"test group {index} has a test without stdin/expected_output"))
                break
    return problems


def check_item_consistency(candidate: GenerationCandidate, request: GenerationRequest) -> tuple[list[ValidationIssue], Optional[str]]:
    """Returns (issues, normalised expected answer or None to leave it)."""
    issues: list[ValidationIssue] = []
    normalised: Optional[str] = None
    kind = candidate.item_type.value

    if len(candidate.public_prompt.strip()) < MIN_PROMPT_CHARS:
        issues.append(_blocker("public_prompt", "the question text is empty or too short"))
    if candidate.estimated_minutes <= 0 or candidate.estimated_minutes > max(3 * request.target_minutes, request.target_minutes + 10):
        issues.append(_major("estimated_minutes", f"estimated {candidate.estimated_minutes} min against a {request.target_minutes} min target"))

    if kind in ("single_choice", "multiple_select"):
        option_issues, keys = _option_problems(candidate.options)
        issues += option_issues
        if len(candidate.options) < 2:
            issues.append(_blocker("options", f"{kind} item needs at least 2 options"))
        expected = _parse_expected_keys(candidate.private_expected_answer)
        unknown = [k for k in expected if k not in keys]
        if not expected:
            issues.append(_blocker("private_expected_answer", "the answer key is missing"))
        elif unknown:
            issues.append(_blocker("private_expected_answer", f"the answer key names option(s) {unknown} that do not exist"))
        elif len(set(expected)) != len(expected):
            issues.append(_blocker("private_expected_answer", "the answer key repeats an option"))
        elif kind == "single_choice" and len(expected) != 1:
            issues.append(_blocker("private_expected_answer", "a single-choice item must have exactly one correct option"))
        elif kind == "multiple_select":
            if len(expected) == len(keys):
                issues.append(_major("private_expected_answer", "every option is marked correct"))
            normalised = ",".join(k for k in keys if k in set(expected))  # canonical: option order
        if request.shuffle_options:
            for option in candidate.options:
                if isinstance(option, dict) and _POSITIONAL.search(str(option.get("text", ""))):
                    issues.append(_blocker("options", f"option {option.get('key')!r} refers to other options by position, which per-student shuffling breaks"))
        if kind == "single_choice" and len(expected) == 1 and not any(i.severity == "blocker" for i in option_issues) and len(candidate.options) >= 3:
            lengths = {str(o["key"]).strip(): len(str(o["text"])) for o in candidate.options if isinstance(o, dict)}
            ranked = sorted(lengths.values(), reverse=True)
            if lengths.get(expected[0]) == ranked[0] and ranked[0] > 2 * ranked[1]:
                issues.append(_major("options", "the correct option is more than twice as long as any other, which cues the answer"))

    elif kind == "short_answer":
        rubric = candidate.marking_rubric or {}
        points = rubric.get("points")
        criteria = rubric.get("criteria")
        total: Optional[float] = None
        if isinstance(criteria, list) and criteria:
            try:
                total = sum(float(c.get("points", 0)) for c in criteria)
            except (AttributeError, TypeError, ValueError):
                issues.append(_blocker("marking_rubric", "rubric criteria must be objects with numeric points"))
        elif isinstance(points, (int, float)):
            total = float(points)
        if total is None and not any(i.field == "marking_rubric" for i in issues):
            issues.append(_blocker("marking_rubric", "a short-answer item needs a rubric with points"))
        elif total is not None and abs(total - request.marks) > 1e-6:
            issues.append(_major("marking_rubric", f"rubric points total {total}, but the item is worth {request.marks}"))

    elif kind == "coding":
        for language in request.permitted_languages:
            if not candidate.reference_solutions.get(language, "").strip():
                issues.append(_blocker("reference_solutions", f"missing reference solution for {language!r}"))
        extra = sorted(set(candidate.reference_solutions) - set(request.permitted_languages))
        if request.permitted_languages and extra:
            issues.append(_major("reference_solutions", f"reference solutions for languages outside the allowlist: {extra}"))
        if not candidate.public_tests:
            issues.append(_major("public_tests", "no public tests supplied"))
        if not candidate.hidden_test_groups:
            issues.append(_major("hidden_test_groups", "no hidden test groups supplied; grading would only see the public tests"))
        issues += _test_group_problems("public_tests", candidate.public_tests)
        issues += _test_group_problems("hidden_test_groups", candidate.hidden_test_groups)
        missing = [key for key in _CODING_CONTRACT_KEYS if not candidate.constraints.get(key)]
        if missing:
            issues.append(_minor("constraints", f"coding contract is missing {missing}"))
    return issues, normalised


# -- entry points ----------------------------------------------------------------------------------

def validate_candidate(
    candidate: GenerationCandidate,
    request: GenerationRequest,
    *,
    previously_used_signatures: set[str],
    signatures_seen_this_batch: set[str],
    previous_prompts: Optional[Mapping[str, str]] = None,
    batch_prompts: Optional[Mapping[str, str]] = None,
) -> tuple[GenerationCandidate, list[ValidationIssue]]:
    """Recomputes issues and status for a candidate (new or edited). Never trusts a stored status."""
    issues: list[ValidationIssue] = []

    # Source-anchor checks: every cited anchor must exist in the bundle we
    # actually supplied - a provider cannot invent a citation.
    bundle_keys = {(a.source_id, a.source_version, a.anchor_id) for a in request.approved_source_bundle}
    if not candidate.supporting_anchors:
        issues.append(_blocker("supporting_anchors", "no supporting anchors cited"))
    for anchor in candidate.supporting_anchors:
        if (anchor.source_id, anchor.source_version, anchor.anchor_id) not in bundle_keys:
            issues.append(_blocker("supporting_anchors", f"cited anchor {anchor.anchor_id!r} does not exist in the supplied source bundle"))

    # Duplicate checks.
    if not candidate.uniqueness_signature:
        issues.append(_blocker("uniqueness_signature", "missing"))
    elif candidate.uniqueness_signature in previously_used_signatures:
        issues.append(_blocker("uniqueness_signature", "duplicates a previously used signature"))
    elif candidate.uniqueness_signature in signatures_seen_this_batch:
        issues.append(_blocker("uniqueness_signature", "duplicates another candidate in this batch"))
    for label, others in (("this batch", batch_prompts or {}), ("an existing candidate", previous_prompts or {})):
        hit = find_near_duplicate(candidate.public_prompt, {k: v for k, v in others.items() if k != candidate.id})
        if hit:
            issues.append(_blocker("public_prompt", f"near-duplicate ({hit[1]:.0%} similar) of {label} {hit[0]}"))
            break

    # Objective/difficulty/marks must match the request - a provider cannot
    # silently substitute an easier item to reach the requested count.
    if set(candidate.objective_ids) != set(request.objective_ids):
        issues.append(_major("objective_ids", "does not match the requested objectives"))
    if candidate.difficulty != request.difficulty:
        issues.append(_major("difficulty", f"expected {request.difficulty!r}, got {candidate.difficulty!r}"))
    if abs(candidate.marks - request.marks) > 1e-6:
        issues.append(_major("marks", f"expected {request.marks}, got {candidate.marks}"))
    if candidate.item_type.value != request.item_type.value:
        issues.append(_blocker("item_type", "does not match the requested item type"))

    # Faculty-set family limits and provider-reported doubts (informational: they never block by themselves).
    for topic in _forbidden_topic_hits(candidate, request):
        issues.append(_major("public_prompt", f"mentions the forbidden topic {topic!r}"))
    for concern in candidate.provider_concerns:
        issues.append(_minor("provider_concern", concern))

    # Structural only: nothing here executes code. Execution belongs to the isolated judge (decision D-5).
    item_issues, normalised = check_item_consistency(candidate, request)
    issues += item_issues
    if normalised is not None:
        candidate = candidate.model_copy(update={"private_expected_answer": normalised})

    has_blocker = any(issue.severity == "blocker" for issue in issues)
    candidate = candidate.model_copy(
        update={
            "validation_issues": issues,
            "status": CandidateStatus.NEEDS_REVISION if has_blocker else CandidateStatus.READY_FOR_HUMAN_REVIEW,
        }
    )
    return candidate, issues


def validate_candidate_dict(
    raw: dict,
    *,
    request: GenerationRequest,
    now: datetime,
    previously_used_signatures: set[str],
    signatures_seen_this_batch: set[str],
    previous_prompts: Optional[Mapping[str, str]] = None,
    batch_prompts: Optional[Mapping[str, str]] = None,
) -> tuple[Optional[GenerationCandidate], list[ValidationIssue]]:
    """Returns (candidate_or_none, issues). `candidate` is None only when the
    raw dict fails basic schema validation (malformed provider JSON) -
    everything else produces a candidate with status=NEEDS_REVISION and the
    issues attached, so faculty can see exactly what a provider got wrong.
    """
    try:
        candidate = GenerationCandidate(
            id=uuid4().hex,
            request_id=request.id,
            blueprint_id=request.blueprint_id,
            slot_index=request.slot_index,
            family_id=str(raw["family_id"]),
            local_candidate_id=str(raw["local_candidate_id"]),
            item_type=raw["item_type"],
            objective_ids=list(raw.get("objective_ids") or []),
            difficulty=str(raw.get("difficulty", "")),
            marks=float(raw.get("marks", 0)),
            estimated_minutes=int(raw.get("estimated_minutes", 0)),
            uniqueness_signature=str(raw.get("uniqueness_signature", "")),
            public_prompt=str(raw.get("public_prompt", "")),
            constraints=dict(raw.get("constraints") or {}),
            public_examples=list(raw.get("public_examples") or []),
            options=list(raw.get("options") or []),
            private_expected_answer=None if raw.get("private_expected_answer") is None else str(raw["private_expected_answer"]),
            private_explanation=raw.get("private_explanation"),
            marking_rubric=dict(raw.get("marking_rubric") or {}),
            reference_solutions=dict(raw.get("reference_solutions") or {}),
            public_tests=list(raw.get("public_tests") or []),
            hidden_test_groups=list(raw.get("hidden_test_groups") or []),
            starter_code=dict(raw.get("starter_code") or {}),
            supporting_anchors=[
                {
                    "source_id": a.get("source_id"),
                    "source_version": a.get("source_version"),
                    "anchor_id": a.get("anchor_id"),
                }
                for a in (raw.get("supporting_anchors") or [])
            ],
            equivalence_rationale=str(raw.get("equivalence_rationale", "")),
            variant_parameters=raw.get("variant_parameters") if isinstance(raw.get("variant_parameters"), dict) else {},
            assumptions=_text_list(raw.get("assumptions")),
            provider_concerns=_text_list(raw.get("validation_concerns")),
            status=CandidateStatus.DRAFT,  # NEVER read from `raw` - see module docstring
            created_at=now,
        )
    except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return None, [_blocker("schema", f"malformed provider candidate: {exc}")]

    return validate_candidate(
        candidate, request, previously_used_signatures=previously_used_signatures,
        signatures_seen_this_batch=signatures_seen_this_batch, previous_prompts=previous_prompts, batch_prompts=batch_prompts,
    )
