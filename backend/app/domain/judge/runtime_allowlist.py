"""Runtime allowlist — the ONLY thing a student ever chooses is a
`language_key` (e.g. "python3.11"); everything Judge0 needs (language_id,
runtime image, resource limits) is resolved server-side from this table.
Students cannot choose network settings, compiler flags, image IDs, or
callback URLs (contract #6/Prompt 6) because there is no field for any of
that anywhere in the student-facing DTOs.
"""

from __future__ import annotations

from app.domain.judge.errors import RuntimeNotAllowedError
from app.domain.judge.models import RuntimeAllowlistEntry


def resolve_runtime(language_key: str, allowlist: list[RuntimeAllowlistEntry]) -> RuntimeAllowlistEntry:
    for entry in allowlist:
        if entry.language_key == language_key:
            return entry
    raise RuntimeNotAllowedError(f"language {language_key!r} is not on the configured allowlist")


def unvalidated_entries(allowlist: list[RuntimeAllowlistEntry]) -> list[RuntimeAllowlistEntry]:
    """Entries an operator configured but never actually confirmed against a
    real provider's `/languages` endpoint — a bounded setup requirement, not
    something this function can satisfy on its own (no provider configured)."""
    return [entry for entry in allowlist if not entry.validated_against_provider]


def default_allowlist() -> list[RuntimeAllowlistEntry]:
    """Judge0's well-known default language_id assignments for these three
    runtimes. NONE of these have `validated_against_provider=True` — no real
    Judge0 instance was ever queried to confirm these ids/images are what it
    actually offers. Treat this as a starting point requiring institutional
    verification, not a validated allowlist (contract: "Allowlist the
    configured Python/Java/C++ runtime versions after actual provider
    validation.")."""
    return [
        RuntimeAllowlistEntry(
            language_key="python3.11", judge0_language_id=92, display_name="Python 3.11",
            runtime_image="judge0/python:3.11", validated_against_provider=False,
        ),
        RuntimeAllowlistEntry(
            language_key="java17", judge0_language_id=91, display_name="Java 17",
            runtime_image="judge0/java:17", validated_against_provider=False,
        ),
        RuntimeAllowlistEntry(
            language_key="cpp17", judge0_language_id=54, display_name="C++ (GCC 17)",
            runtime_image="judge0/gcc:17", validated_against_provider=False,
        ),
    ]
