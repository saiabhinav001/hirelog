"""Server-owned language registry. The ONLY thing a student ever chooses is a `language_key` (e.g. "python3.11");
everything the judge needs (language id, runtime image, limits) is resolved here, server-side. Students cannot choose
network settings, compiler flags, image ids or callback URLs because no student-facing DTO has a field for them.

The registry lists the candidate languages (C, C++, Java, Python, JavaScript, TypeScript). With the fake dev judge every
entry is usable. With a real provider configured, an entry is usable only after an operator has checked its id against
the provider's `/languages` and listed its key in `EXAM_JUDGE_VALIDATED_LANGUAGES` (ADR 0024); nothing in this repository
queries a provider, so none is validated by default.
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
    """Entries an operator configured but never actually confirmed against a real provider's `/languages` endpoint."""
    return [entry for entry in allowlist if not entry.validated_against_provider]


def default_allowlist() -> list[RuntimeAllowlistEntry]:
    """Judge0 CE 1.13 default ids for the candidate runtimes. These are a starting point, NOT validated: no Judge0
    instance was queried to confirm that these ids and images are what a deployment actually offers."""
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
        RuntimeAllowlistEntry(
            language_key="c17", judge0_language_id=103, display_name="C (GCC 14, C17)",
            runtime_image="judge0/gcc:14", validated_against_provider=False,
        ),
        RuntimeAllowlistEntry(
            language_key="node18", judge0_language_id=93, display_name="JavaScript (Node.js 18)",
            runtime_image="judge0/node:18", validated_against_provider=False,
        ),
        RuntimeAllowlistEntry(
            language_key="typescript5", judge0_language_id=94, display_name="TypeScript 5",
            runtime_image="judge0/typescript:5", validated_against_provider=False,
        ),
    ]


def real_provider_configured() -> bool:
    from app.core.config import settings

    return bool(settings.EXAM_JUDGE_PROVIDER_BASE_URL and settings.EXAM_JUDGE_PROVIDER_API_KEY)


def configured_allowlist() -> list[RuntimeAllowlistEntry]:
    """What a student may run and what a blueprint may name, right now."""
    from app.core.config import settings

    validated = {key.strip() for key in (settings.EXAM_JUDGE_VALIDATED_LANGUAGES or "").split(",") if key.strip()}
    entries = [
        entry.model_copy(update={"validated_against_provider": entry.language_key in validated})
        for entry in default_allowlist()
    ]
    if real_provider_configured():
        return [entry for entry in entries if entry.validated_against_provider]
    return entries
