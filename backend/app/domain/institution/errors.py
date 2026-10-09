"""Domain-level exceptions for institution identity/roster/auth. Separate
from FastAPI so the pure decision layer stays framework-free and unit-testable.
"""

from __future__ import annotations


class InstitutionDomainError(Exception):
    code: str = "institution_domain_error"


class CrossInstitutionScopeError(InstitutionDomainError):
    code = "cross_institution_scope"


class UnauthorizedRoleGrantError(InstitutionDomainError):
    code = "unauthorized_role_grant"


class DuplicateIdentityKeyError(InstitutionDomainError):
    code = "duplicate_identity_key"


class IdentityAlreadyLinkedError(InstitutionDomainError):
    """A verified identity is already linked to a DIFFERENT uid — requires
    an audited conflict/recovery workflow, never a silent overwrite."""

    code = "identity_already_linked"


class AccountAlreadyLinkedError(InstitutionDomainError):
    """The signed-in account already holds a DIFFERENT identity in this
    institution; two identities on one uid would make its membership ambiguous."""

    code = "account_already_linked_in_institution"


class RosterRowsChangedError(InstitutionDomainError):
    """Apply was given rows other than the ones the dry-run validated."""

    code = "roster_rows_changed"


class RosterRowRejectedError(InstitutionDomainError):
    """A single-row roster operation (teacher assignment) failed validation."""

    code = "roster_row_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OtpInvalidError(InstitutionDomainError):
    code = "otp_invalid"


class OtpExpiredError(InstitutionDomainError):
    code = "otp_expired"


class OtpAttemptsExceededError(InstitutionDomainError):
    code = "otp_attempts_exceeded"


class MembershipNotActiveError(InstitutionDomainError):
    code = "membership_not_active"


class StaleAuthorizationVersionError(InstitutionDomainError):
    """The caller's token reflects an authorization_version that has since
    been revoked/rotated — the membership was changed after the token was
    issued (contract: revocation during a session must be observed)."""

    code = "stale_authorization_version"
