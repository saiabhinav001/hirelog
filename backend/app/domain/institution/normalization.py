"""Identity-key and email normalization.

Institution-specific — different colleges format IDs differently (e.g.
`"007A"`, `"2024-CS-007"`). The one universal rule: NEVER coerce to an
integer anywhere in this pipeline — that would silently drop leading zeros,
which is explicitly called out as a thing to preserve. All identity keys are
strings, always.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_identity_key(raw: str) -> str:
    """Trim, collapse internal whitespace, uppercase (case-insensitive college
    IDs are the common case) — but never touch numeric-looking prefixes, since
    this never becomes a number."""
    value = _WHITESPACE_RE.sub(" ", raw.strip())
    return value.upper()


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def make_membership_doc_id(institution_id: str, identity_key: str) -> str:
    """The document id itself IS the uniqueness constraint for
    (institution_id, identity_key) — Firestore has no native composite unique
    index, so collisions are caught by a document-already-exists check."""
    return f"{institution_id}::{normalize_identity_key(identity_key)}"
