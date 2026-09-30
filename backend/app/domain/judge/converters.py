"""Converts the loosely-typed JSON test-group shape stored on a
`GenerationCandidate` (`public_tests`/`hidden_test_groups`) into the judge
domain's structured `TestGroupSpec`."""

from __future__ import annotations

from app.domain.judge.models import TestCaseSpec, TestGroupSpec


def to_test_groups(raw_groups: list) -> list[TestGroupSpec]:
    groups: list[TestGroupSpec] = []
    for raw in raw_groups:
        groups.append(
            TestGroupSpec(
                group_name=str(raw.get("group_name", "group")),
                weight=float(raw.get("weight", 1.0)),
                is_hidden=bool(raw.get("is_hidden", False)),
                tests=[
                    TestCaseSpec(stdin=str(t.get("stdin", "")), expected_output=str(t.get("expected_output", "")))
                    for t in (raw.get("tests") or [])
                ],
            )
        )
    return groups
