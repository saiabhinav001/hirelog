"""Pure exam authorization policy — no Firestore, no FastAPI (Prompt 1, ADR 0017).

Roles are computed on the server from three inputs only: the VERIFIED token
e-mail checked against server-side allowlists, the caller's institution
memberships, and the resource being touched. Nothing a client can write
(request body, `users/{uid}` fields) is ever an input.

Mapping onto the EXISTING role model (no second role store):
  student      <- ACTIVE `RoleScope.STUDENT` membership (enrollment decides access to a run)
  teacher      <- verified bootstrap allowlist (`EXAM_FACULTY_EMAILS`) or an ACTIVE
                  `RoleScope.FACULTY` / `RoleScope.ADMIN` membership
  super_admin  <- verified bootstrap allowlist (`EXAM_SUPER_ADMIN_EMAILS`); implies teacher
`RoleScope.PROCTOR` grants nothing, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from app.domain.institution.errors import MembershipNotActiveError, StaleAuthorizationVersionError
from app.domain.institution.models import Membership, MembershipStatus, RoleScope
from app.domain.institution.normalization import normalize_email


class ExamRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    SUPER_ADMIN = "super_admin"


class ScopeDeniedError(Exception):
    code = "scope_denied"


class InstitutionRequiredError(Exception):
    code = "institution_required"


@dataclass(frozen=True)
class StaffGrants:
    """What the allowlists alone entitle a verified e-mail to."""

    super_admin: bool = False
    teacher: bool = False

    @property
    def any(self) -> bool:
        return self.super_admin or self.teacher


def resolve_staff_grants(
    *,
    email: str,
    email_verified: bool,
    super_admin_emails: set[str],
    teacher_emails: set[str],
    allow_unverified: bool = False,
) -> StaffGrants:
    """An allowlisted address only counts when the identity provider vouches
    for it (`email_verified`); an unverified e-mail/password account that
    merely *claims* an allowlisted address gets nothing."""
    normalized = normalize_email(email or "")
    if not normalized:
        return StaffGrants()
    if not (email_verified or allow_unverified):
        return StaffGrants()
    return StaffGrants(
        super_admin=normalized in super_admin_emails,
        teacher=normalized in teacher_emails,
    )


@dataclass(frozen=True)
class ExamPrincipal:
    uid: str
    email: str  # verified-token e-mail, normalized; "" when the token has none
    email_verified: bool
    bootstrap_super_admin: bool = False
    bootstrap_teacher: bool = False
    memberships: tuple[Membership, ...] = ()

    def _active(self) -> list[Membership]:
        return [m for m in self.memberships if m.status == MembershipStatus.ACTIVE]

    @property
    def is_super_admin(self) -> bool:
        return self.bootstrap_super_admin

    @property
    def teacher_institution_ids(self) -> frozenset[str]:
        return frozenset(
            m.institution_id for m in self._active() if m.role_scope in (RoleScope.FACULTY, RoleScope.ADMIN)
        )

    @property
    def admin_institution_ids(self) -> frozenset[str]:
        return frozenset(m.institution_id for m in self._active() if m.role_scope == RoleScope.ADMIN)

    @property
    def student_institution_ids(self) -> frozenset[str]:
        return frozenset(m.institution_id for m in self._active() if m.role_scope == RoleScope.STUDENT)

    @property
    def is_teacher(self) -> bool:
        return self.bootstrap_super_admin or self.bootstrap_teacher or bool(self.teacher_institution_ids)

    def roles(self) -> list[ExamRole]:
        roles: list[ExamRole] = []
        if self.student_institution_ids:
            roles.append(ExamRole.STUDENT)
        if self.is_teacher:
            roles.append(ExamRole.TEACHER)
        if self.is_super_admin:
            roles.append(ExamRole.SUPER_ADMIN)
        return roles


def can_manage_resource(
    principal: ExamPrincipal,
    *,
    owner_uid: Optional[str],
    institution_id: Optional[str],
    co_owner_uids: Sequence[str] = (),
) -> bool:
    """Teacher-side access to an exam resource: its owner, a co-instructor
    assigned to its definition ("definition = course", Prompt 2 / ADR 0018), an
    ADMIN of the resource's institution, or a super_admin. A lost teacher role
    (revoked membership, allowlist removal) ends access even for the original
    owner or a co-instructor."""
    if principal.is_super_admin:
        return True
    if not principal.is_teacher:
        return False
    if owner_uid and owner_uid == principal.uid:
        return True
    if principal.uid in co_owner_uids:
        return True
    return bool(institution_id) and institution_id in principal.admin_institution_ids


def can_administer_institution(principal: ExamPrincipal, institution_id: Optional[str]) -> bool:
    """Super-admin, or ADMIN of that institution: may re-point a member's
    contact e-mail, revoke / reinstate / unlink student memberships and assign
    co-instructors. (Granting the FACULTY/ADMIN role itself is super-admin only —
    see `max_grantable_role_scope`.)"""
    return principal.is_super_admin or (bool(institution_id) and institution_id in principal.admin_institution_ids)


def can_participate_as_student(principal: ExamPrincipal, *, run_institution_id: Optional[str]) -> bool:
    """Runs without an institution (pilot / uid-list enrollment) rely on the
    enrollment snapshot alone; institution-scoped runs also require a
    CURRENT, ACTIVE student membership in that institution."""
    if not run_institution_id:
        return True
    return run_institution_id in principal.student_institution_ids


def max_grantable_role_scope(principal: ExamPrincipal, institution_id: str) -> Optional[RoleScope]:
    """Highest `RoleScope` the caller may put on roster rows of this institution,
    and the highest role whose membership they may revoke / reinstate / unlink.
    Teacher assignment is a super-admin function: only a super-admin may grant
    FACULTY or ADMIN; institution ADMINs and FACULTY manage STUDENT rows.
    `None` means the caller may not manage its roster at all."""
    if principal.is_super_admin:
        return RoleScope.ADMIN
    if institution_id in principal.teacher_institution_ids:  # FACULTY or ADMIN membership
        return RoleScope.STUDENT
    return None


def check_token_membership_freshness(
    memberships: Sequence[Membership], *, institution_id: str, token_authz_version: Optional[int]
) -> Membership:
    """Same contract as `InstitutionAuthService.check_active_membership`, but
    over memberships the request already loaded (one query per request)."""
    candidates = [m for m in memberships if m.institution_id == institution_id]
    if not candidates:
        raise MembershipNotActiveError(f"no membership in institution {institution_id}")
    membership = candidates[0]
    if membership.status != MembershipStatus.ACTIVE:
        raise MembershipNotActiveError("membership is not active")
    if token_authz_version is not None and token_authz_version != membership.authorization_version:
        raise StaleAuthorizationVersionError(
            f"token authz_version={token_authz_version} != current={membership.authorization_version}"
        )
    return membership


def resolve_creation_institution(principal: ExamPrincipal, requested: Optional[str]) -> Optional[str]:
    """Institution a NEW definition belongs to. Authority that comes from an
    institution membership never produces an unscoped resource."""
    teacher_institutions = principal.teacher_institution_ids
    if requested:
        if principal.is_super_admin or requested in teacher_institutions:
            return requested
        raise ScopeDeniedError(f"not a teacher in institution {requested}")
    if principal.is_super_admin:
        return None
    if len(teacher_institutions) == 1:
        return next(iter(teacher_institutions))
    if len(teacher_institutions) > 1:
        raise InstitutionRequiredError("institution_id is required when teaching in more than one institution")
    return None  # bootstrap teacher with no membership: pilot / unscoped resource
