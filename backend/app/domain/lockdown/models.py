"""Managed-lockdown admission domain models (Prompt 10).

Verifies Safe Exam Browser (SEB) Config Key / Browser Exam Key HTTP
headers per SEB's own documented algorithm
(https://www.safeexambrowser.org/developer/seb-config-key.html — fetched
and confirmed during implementation, not guessed). See `decisions.py` for
exactly which parts of the protocol are precisely implemented versus
treated as an opaque institution-supplied secret.

Firestore layout (private — no client rule grants them; default-deny
catch-all covers everything here):

  exam_lockdown_profiles/{run_id}
  exam_lockdown_admission_log/{entry_id}
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LockdownAdmissionOutcome(str, Enum):
    ADMITTED = "admitted"
    # Profile has enabled=True but no key was ever configured — fails
    # closed rather than silently admitting any client (contract: "PWA
    # installation alone does not pass").
    DENIED_NOT_CONFIGURED = "denied_not_configured"
    DENIED_MISSING_HEADERS = "denied_missing_headers"
    DENIED_HASH_MISMATCH = "denied_hash_mismatch"


class LockdownProfile(BaseModel):
    """Per-run strict-admission configuration. `enabled=False` (default)
    means ordinary browsers/PWA are admitted exactly like any other exam —
    strict mode is always an explicit, per-run opt-in, never a global
    switch (contract: feature flags off by default)."""

    run_id: str
    enabled: bool = False
    # Opaque secrets copy-pasted by an institution admin from SEB's own
    # Config Tool / Preferences "Exam" tab — never generated or guessed by
    # this system (see decisions.py docstring for why).
    expected_config_key_hash: Optional[str] = None
    expected_browser_exam_key_hashes: list[str] = Field(default_factory=list)
    # Declarative, admin-attested support matrix — never auto-detected
    # (SEB's classic header mechanism carries no verifiable platform
    # field). Empty by default: "mark untested platforms unsupported".
    piloted_platforms: list[str] = Field(default_factory=list)
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class LockdownAdmissionLogEntry(BaseModel):
    """Append-only audit trail — every admission check against a
    lockdown-enabled run, regardless of outcome."""

    id: str
    run_id: str
    uid: str
    outcome: LockdownAdmissionOutcome
    config_key_header_present: bool
    request_hash_header_present: bool
    at: datetime
