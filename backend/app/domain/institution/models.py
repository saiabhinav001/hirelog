"""Institution/membership/roster domain models.

Schema versions are stamped so a future shape change has a version signal to
key migrations off (mirrors the existing `EXPERIENCE_SCHEMA_VERSION` pattern
in `app.core.config`). `identity_key` is always the normalized, string form —
see `normalization.py` — never re-derived ad hoc elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

MEMBERSHIP_SCHEMA_VERSION = 1
ROSTER_IMPORT_SCHEMA_VERSION = 1


class RoleScope(str, Enum):
    """Institution-scoped roles — deliberately separate from the legacy
    global `users/{uid}.role` field (contract #2: exam/institution authority
    never lives in a client-editable user record)."""

    STUDENT = "student"
    FACULTY = "faculty"
    PROCTOR = "proctor"
    ADMIN = "admin"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class IdentityKeyPolicy(BaseModel):
    """Per-institution college-ID rules (Prompt 2, docs/adr/0018). Applied on top
    of the universal trim/collapse/UPPERCASE step. Set when the institution is
    created and never edited: membership document ids embed the normalized key,
    so changing the policy later would orphan existing memberships."""

    remove_characters: str = Field(default="", max_length=16)  # e.g. "-/. " so 21-BCE-0001 == 21BCE0001
    min_length: int = Field(default=1, ge=1, le=64)
    max_length: int = Field(default=64, ge=1, le=64)
    charset: Literal["any", "alphanumeric"] = "any"

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> "IdentityKeyPolicy":
        if self.min_length > self.max_length:
            raise ValueError("min_length must not exceed max_length")
        return self


class Institution(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime
    # None = the universal normalization only (every pre-Prompt-2 institution).
    identity_policy: Optional[IdentityKeyPolicy] = None


class Membership(BaseModel):
    """Doc id == f'{institution_id}::{identity_key}' — this IS the uniqueness
    constraint (Firestore has no native composite-unique index; a document
    either exists at that id or it doesn't)."""

    schema_version: int = MEMBERSHIP_SCHEMA_VERSION
    institution_id: str
    identity_key: str  # normalized college ID — see normalization.py
    contact_email: str
    role_scope: RoleScope = RoleScope.STUDENT
    status: MembershipStatus = MembershipStatus.ACTIVE
    uid: Optional[str] = None  # set once linked/provisioned; None until first login
    authorization_version: int = 1  # bumped on any revocation/role change
    created_at: datetime
    updated_at: datetime


class RosterRowInput(BaseModel):
    """One raw CSV row as submitted by the uploader — untrusted input,
    validated by `roster_import.py` before ever becoming a `Membership`."""

    identity_key: str = Field(min_length=1, max_length=64)
    contact_email: str = Field(min_length=3, max_length=254)
    role_scope: str = "student"

    model_config = {"extra": "forbid"}


class RosterRowResult(BaseModel):
    row_index: int
    identity_key: str
    ok: bool
    action: str = ""  # "create" | "update" | "skip_unchanged"
    error: Optional[str] = None
    first_seen_row: Optional[int] = None  # set on duplicate_identity_key_in_file
    line_number: Optional[int] = None  # CSV line, set when the row could not be parsed


class RosterImportStatus(str, Enum):
    DRY_RUN = "dry_run"
    APPLYING = "applying"
    COMPLETED = "completed"
    FAILED = "failed"


class RosterImportJob(BaseModel):
    id: str
    schema_version: int = ROSTER_IMPORT_SCHEMA_VERSION
    institution_id: str
    uploaded_by: str
    requester_max_role_scope: str  # the highest role_scope the uploader may grant
    # SHA-256 of the normalized rows seen at dry-run; apply must present the same
    # rows (None only on jobs created before Prompt 2).
    rows_digest: Optional[str] = None
    status: RosterImportStatus = RosterImportStatus.DRY_RUN
    total_rows: int = 0
    applied_cursor: int = 0  # resumable — index of the next unprocessed row
    row_results: list[RosterRowResult] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
