"""Domain exceptions for proctoring (incident ledger, evidence, appeals)."""

from __future__ import annotations


class ProctoringDomainError(Exception):
    code: str = "proctoring_domain_error"


class UploadTicketInvalidError(ProctoringDomainError):
    """Expired, already-consumed, or scope-mismatched upload ticket."""

    code = "upload_ticket_invalid"


class ViewTicketInvalidError(ProctoringDomainError):
    code = "view_ticket_invalid"


class SnapshotHashMismatchError(ProctoringDomainError):
    """Uploaded bytes don't match the hash the client declared when
    confirming completion — rejected rather than silently trusted."""

    code = "snapshot_hash_mismatch"


class AttemptNotBlockedError(ProctoringDomainError):
    """A resolution/unblock action was attempted on an attempt that isn't
    currently BLOCKED — never a silent no-op, always explicit."""

    code = "attempt_not_blocked"


class EvidenceStorageUnavailableError(ProctoringDomainError):
    """Private evidence storage is not configured or could not be reached. Evidence is then recorded as missing;
    nothing else (answers, submission) depends on it."""

    code = "evidence_storage_unavailable"
