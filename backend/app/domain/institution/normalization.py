"""Identity-key and email normalization.

Institution-specific — different colleges format IDs differently (e.g.
`"007A"`, `"2024-CS-007"`). The one universal rule: NEVER coerce to an
integer anywhere in this pipeline — that would silently drop leading zeros,
which is explicitly called out as a thing to preserve. All identity keys are
strings, always.

An institution may add an `IdentityKeyPolicy` (Prompt 2) on top of the
universal step. The result is always a fixed point of the universal step, so
the repository can keep re-normalizing document ids without the policy.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.domain.institution.models import IdentityKeyPolicy

_WHITESPACE_RE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_EMAIL_LENGTH = 254


def _collapse_and_upper(raw: str) -> str:
    return _WHITESPACE_RE.sub(" ", raw.strip()).upper()


def normalize_identity_key(raw: str, policy: Optional["IdentityKeyPolicy"] = None) -> str:
    """Trim, collapse internal whitespace, uppercase (case-insensitive college
    IDs are the common case) — but never touch numeric-looking prefixes, since
    this never becomes a number. With a policy, the institution's separator
    characters are then removed (so `21-BCE-0001` and `21BCE0001` are one ID)."""
    value = _collapse_and_upper(raw)
    if policy is not None and policy.remove_characters:
        removable = set(policy.remove_characters) | set(policy.remove_characters.upper())
        value = _collapse_and_upper("".join(ch for ch in value if ch not in removable))
    return value


def identity_key_problem(key: str, policy: Optional["IdentityKeyPolicy"] = None) -> Optional[str]:
    """Why a NORMALIZED key is unacceptable on import, or None. Rejects control
    and invisible characters so two visually identical IDs can never be distinct
    rows."""
    if not key:
        return "empty"
    if not key.isprintable():
        return "invalid_characters"
    min_length = policy.min_length if policy else 1
    max_length = policy.max_length if policy else 64
    if len(key) < min_length:
        return "too_short"
    if len(key) > max_length:
        return "too_long"
    if policy is not None and policy.charset == "alphanumeric" and not (key.isascii() and key.isalnum()):
        return "not_alphanumeric"
    return None


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def is_valid_contact_email(email: str) -> bool:
    """Shape check only (the OTP delivery is the real proof). Whitespace and
    control characters are rejected so an address can never smuggle header
    lines into a delivery adapter."""
    return len(email) <= _MAX_EMAIL_LENGTH and email.isprintable() and bool(_EMAIL_RE.match(email))


def make_membership_doc_id(institution_id: str, identity_key: str) -> str:
    """The document id itself IS the uniqueness constraint for
    (institution_id, identity_key) — Firestore has no native composite unique
    index, so collisions are caught by a document-already-exists check."""
    return f"{institution_id}::{normalize_identity_key(identity_key)}"
