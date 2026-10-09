"""Domain-level exceptions for the exam module.

Kept separate from FastAPI/HTTPException so the pure decision layer
(`state_machine.py`, `decisions.py`) has zero web-framework dependency and can
be unit-tested in isolation. The API route layer maps these to HTTP responses.
"""

from __future__ import annotations


class ExamDomainError(Exception):
    """Base class for all exam domain errors."""

    code: str = "exam_domain_error"


class IllegalTransitionError(ExamDomainError):
    code = "illegal_transition"

    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"Cannot transition {entity} from {current!r} to {target!r}")
        self.entity = entity
        self.current = current
        self.target = target


class VersionConflictError(ExamDomainError):
    code = "version_conflict"

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"Expected version {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class UnresolvedAttemptsError(ExamDomainError):
    """Raised when a close command would affect unresolved attempts and the
    caller did not pass force=True — the "warn before commands that close
    unresolved drafts" contract."""

    code = "unresolved_attempts"

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} attempt(s) are not yet resolved; pass force=True to proceed")
        self.count = count


class CommandRejectedError(ExamDomainError):
    """A faculty attempt/access command that is well-formed but not allowed in
    the current state (e.g. extending an attempt that already expired).
    `reason` is a stable machine-readable code; the API maps it to 409."""

    code = "command_rejected"

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"{command} rejected: {reason}")
        self.command = command
        self.reason = reason


class PublicationBlockedError(ExamDomainError):
    """Publishing from a blueprint failed validation (see `publication.py`)."""

    code = "publication_blocked"

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("; ".join(blockers) or "publication blocked")
        self.blockers = blockers


class NotEnrolledError(ExamDomainError):
    code = "not_enrolled"


class StaleEpochError(ExamDomainError):
    code = "stale_epoch"


class SubmitConflictError(ExamDomainError):
    """A final-submit request could not be applied as-is (draft version drift,
    already finalized under a different key, deadline passed, blocked, etc.).
    `reason` is a stable machine-readable code for the API layer/tests."""

    code = "submit_conflict"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
