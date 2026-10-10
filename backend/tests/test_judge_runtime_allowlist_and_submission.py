"""Runtime-allowlist resolution and submission-identity (hash + result-
ownership) tests. Together these enforce: students only ever choose a
`language_key` (never network/image/flags), and no provider result is ever
trusted unless its token matches exactly what this server issued for that
test.
"""

from __future__ import annotations

import pytest

from app.domain.judge.errors import RuntimeNotAllowedError
from app.domain.judge.models import RuntimeAllowlistEntry
from app.domain.judge.runtime_allowlist import default_allowlist, resolve_runtime, unvalidated_entries
from app.domain.judge.submission import compute_submission_hash, verify_result_ownership


def _entry(language_key: str, *, validated: bool = False) -> RuntimeAllowlistEntry:
    return RuntimeAllowlistEntry(
        language_key=language_key, judge0_language_id=1, display_name=language_key,
        runtime_image=f"img/{language_key}", validated_against_provider=validated,
    )


# ── resolve_runtime ───────────────────────────────────────────────────────────

def test_resolve_runtime_finds_configured_language() -> None:
    allowlist = [_entry("python3.11"), _entry("java17")]
    resolved = resolve_runtime("java17", allowlist)
    assert resolved.language_key == "java17"


def test_resolve_runtime_rejects_language_not_on_allowlist() -> None:
    with pytest.raises(RuntimeNotAllowedError):
        resolve_runtime("php8", [_entry("python3.11")])


def test_resolve_runtime_rejects_arbitrary_student_supplied_strings() -> None:
    """Students cannot smuggle a docker image name or network setting through
    this field — anything not an exact allowlist key key is rejected."""
    with pytest.raises(RuntimeNotAllowedError):
        resolve_runtime("python3.11; --network=host", [_entry("python3.11")])


def test_unvalidated_entries_filters_correctly() -> None:
    allowlist = [_entry("python3.11", validated=True), _entry("java17", validated=False)]
    unvalidated = unvalidated_entries(allowlist)
    assert [e.language_key for e in unvalidated] == ["java17"]


def test_default_allowlist_lists_the_candidate_runtimes_all_unvalidated() -> None:
    """Regression guard: nobody should flip `validated_against_provider` to
    True on the built-in defaults without actually querying a real Judge0
    instance first (docs/adr/0011, 0024)."""
    allowlist = default_allowlist()
    assert {e.language_key for e in allowlist} == {"python3.11", "java17", "cpp17", "c17", "node18", "typescript5"}
    assert all(e.validated_against_provider is False for e in allowlist)
    assert unvalidated_entries(allowlist) == allowlist


# ── compute_submission_hash ───────────────────────────────────────────────────

def test_submission_hash_is_deterministic() -> None:
    assert compute_submission_hash("print(1)") == compute_submission_hash("print(1)")


def test_submission_hash_changes_with_content() -> None:
    assert compute_submission_hash("print(1)") != compute_submission_hash("print(2)")


def test_submission_hash_is_sha256_hex() -> None:
    digest = compute_submission_hash("anything")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


# ── verify_result_ownership ────────────────────────────────────────────────────

def test_verify_result_ownership_accepts_matching_token() -> None:
    tokens = {"test-1": "tok-abc"}
    assert verify_result_ownership(tokens, "test-1", "tok-abc") is True


def test_verify_result_ownership_rejects_mismatched_token() -> None:
    tokens = {"test-1": "tok-abc"}
    assert verify_result_ownership(tokens, "test-1", "tok-FORGED") is False


def test_verify_result_ownership_rejects_unknown_test_id() -> None:
    tokens = {"test-1": "tok-abc"}
    assert verify_result_ownership(tokens, "test-unknown", "tok-abc") is False
