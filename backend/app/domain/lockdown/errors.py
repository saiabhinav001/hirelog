"""Domain exceptions for managed-lockdown admission."""

from __future__ import annotations


class LockdownDomainError(Exception):
    code: str = "lockdown_domain_error"


class LockdownAdmissionDeniedError(LockdownDomainError):
    """Strict-mode admission failed — missing/mismatched SEB headers, or a
    profile enabled with no keys configured yet (fails closed)."""

    code = "lockdown_admission_denied"
