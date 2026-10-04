"""Proctoring domain models.

Firestore layout (all private — no client rule grants them; default-deny
catch-all covers everything here):

  proctoring_states/{run_id}__{uid}          (one doc per attempt — hot path)
  proctoring_log/{entry_id}                  (append-only audit trail)
  evidence_snapshots/{snapshot_id}
  evidence_upload_tickets/{ticket_id}
  evidence_view_tickets/{ticket_id}
  proctoring_appeals/{appeal_id}

Browser-reported observations are CLIENT-REPORTED, not cryptographic proof —
every DTO that surfaces them to faculty says so explicitly (contract: "Mark
browser telemetry as client-reported, not cryptographic misconduct proof.").
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ObservationKind(str, Enum):
    """`PAGE_HIDDEN`/`FULLSCREEN_EXIT` are departure-candidates (may be
    counted after correlation). Everything else is informational by
    construction — it is never counted, regardless of correlation, timing,
    or frequency (contract: "Blur alone, permissions prompts and connection/
    camera failures are technical/informational by default.")."""

    PAGE_HIDDEN = "page_hidden"
    PAGE_VISIBLE = "page_visible"
    FULLSCREEN_EXIT = "fullscreen_exit"
    FULLSCREEN_REENTERED = "fullscreen_reentered"
    BLUR = "blur"
    FOCUS = "focus"
    PERMISSION_DENIED = "permission_denied"
    CAMERA_ERROR = "camera_error"
    CONNECTION_DEGRADED = "connection_degraded"
    CLIPBOARD_BLOCKED = "clipboard_blocked"


DEPARTURE_START_KINDS = frozenset({ObservationKind.PAGE_HIDDEN, ObservationKind.FULLSCREEN_EXIT})
DEPARTURE_END_KINDS = frozenset({ObservationKind.PAGE_VISIBLE, ObservationKind.FULLSCREEN_REENTERED})
INFORMATIONAL_KINDS = frozenset(
    {
        ObservationKind.BLUR,
        ObservationKind.FOCUS,
        ObservationKind.PERMISSION_DENIED,
        ObservationKind.CAMERA_ERROR,
        ObservationKind.CONNECTION_DEGRADED,
        ObservationKind.CLIPBOARD_BLOCKED,
    }
)


class ProctoringState(BaseModel):
    """One doc per attempt — the hot-path correlation state. Never exposes
    anything beyond counts/timestamps to the student; the full narrative
    lives in `IncidentLogEntry` for faculty review.

    `last_closed_incident_*` fields exist solely so a departure that flaps
    (blur/focus/blur/focus rapidly) within `cooldown_seconds` reopens and
    extends the SAME logical incident — preserving its true original
    `started_at` and whether it was already counted — rather than being
    miscounted as two separate departures."""

    run_id: str
    uid: str
    open_incident_started_at: Optional[datetime] = None
    open_incident_last_activity_at: Optional[datetime] = None
    open_incident_already_counted: bool = False
    last_closed_incident_started_at: Optional[datetime] = None
    last_closed_incident_ended_at: Optional[datetime] = None
    last_closed_incident_counted: bool = False
    counted_incident_count: int = 0
    # Bounded dedup window — a replay older than this cap is vanishingly
    # unlikely within one exam attempt; documented, not unlimited growth.
    recent_event_ids: list[str] = Field(default_factory=list, max_length=64)
    version: int = 1
    updated_at: Optional[datetime] = None


class IncidentLogEntry(BaseModel):
    """Append-only — never edited, only ever added to. This IS the visible
    'review history' (contract: 'Preserve... visible count explanations and
    review history.')."""

    id: str
    run_id: str
    uid: str
    kind: ObservationKind
    counted: bool
    client_timestamp: datetime
    received_at: datetime
    duration_seconds: Optional[float] = None
    client_event_id: str
    note: str = ""  # e.g. "below grace period", "merged into open incident"


class EvidenceSnapshotKind(str, Enum):
    CHECK_IN = "check_in"
    PERIODIC = "periodic"
    SUBMISSION = "submission"


class EvidenceSnapshotStatus(str, Enum):
    REQUESTED = "requested"
    UPLOADED = "uploaded"
    VERIFIED = "verified"
    MISSING = "missing"


class EvidenceSnapshot(BaseModel):
    """Submission acceptance never depends on this succeeding — a MISSING
    snapshot with a reason is a complete, valid, honest outcome, not an
    error state that blocks anything (contract: 'Submission acceptance
    cannot depend on a successful image upload.')."""

    id: str
    run_id: str
    uid: str
    kind: EvidenceSnapshotKind
    status: EvidenceSnapshotStatus = EvidenceSnapshotStatus.REQUESTED
    requested_at: datetime
    uploaded_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    storage_ref: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    missing_reason: Optional[str] = None  # e.g. "permission_denied", "capture_failed", "upload_failed", "not_attempted"


class UploadTicket(BaseModel):
    """Single-use, short-lived, scoped to exactly one snapshot — mirrors the
    live-control WS ticket pattern (ADR 0012)."""

    id: str
    run_id: str
    uid: str
    snapshot_id: str
    expires_at: datetime
    consumed: bool = False
    consumed_at: Optional[datetime] = None


class ViewTicket(BaseModel):
    """Single-use, short-lived — issued only to an authenticated faculty
    reviewer, and every issuance/use is audited via `EvidenceAccessLogEntry`."""

    id: str
    snapshot_id: str
    issued_to: str  # faculty uid
    expires_at: datetime
    consumed: bool = False
    consumed_at: Optional[datetime] = None


class EvidenceAccessLogEntry(BaseModel):
    id: str
    snapshot_id: str
    accessed_by: str
    accessed_at: datetime


class AppealStatus(str, Enum):
    PENDING = "pending"
    UPHELD = "upheld"  # block stands
    OVERTURNED = "overturned"  # block lifted


class Appeal(BaseModel):
    id: str
    run_id: str
    uid: str
    student_statement: str = Field(max_length=4000)
    status: AppealStatus = AppealStatus.PENDING
    created_at: datetime
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    # Regrade-style versioning — a reversed decision creates a new appeal
    # record via `version`, never edits a resolved one in place.
    version: int = 1
