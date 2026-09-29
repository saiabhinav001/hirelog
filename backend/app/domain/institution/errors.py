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
