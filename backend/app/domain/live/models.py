"""Live-control domain models: short-lived WS admission tickets and the
lightweight event envelope broadcast over a run's socket connections.

A `LiveEvent` NEVER carries question content, answers, rubrics or evidence —
only summary/version fields already safe for whichever audience the scope
targets (contract #3). Clients treat every event as a "something changed, go
refetch the real state over REST" nudge, never as authoritative data on its
own (contract: "Socket events never override server guards").
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LiveRole(str, Enum):
    STUDENT = "student"
    FACULTY = "faculty"


class LiveTicket(BaseModel):
    """Single-use, short-lived WS admission proof. Verified and consumed
    exactly once inside a Firestore transaction — a ticket can never be
    replayed to open a second connection (contract: authenticated WS ticket
    admission)."""

    id: str
    run_id: str
    uid: str
    role: LiveRole
    issued_at: datetime
    expires_at: datetime
    consumed: bool = False
    consumed_at: Optional[datetime] = None


class LiveEventKind(str, Enum):
    SNAPSHOT = "snapshot"  # first message after admission — current version/seq only
    RUN_STATE_CHANGED = "run_state_changed"
    ATTEMPT_STATUS_CHANGED = "attempt_status_changed"
    HEARTBEAT = "heartbeat"


class LiveEvent(BaseModel):
    """`seq` is monotonically increasing PER RUN (not per connection) — the
    client's dedupe/ordering/gap-detection logic keys off this field alone."""

    seq: int = Field(ge=0)
    kind: LiveEventKind
    run_id: str
    state_version: int = Field(ge=0)
    payload: dict = Field(default_factory=dict)
    # None = every connection for this run (faculty room + all students).
    # Set = faculty room + that one student's own connection(s) only — other
    # students never see another student's targeted event.
    target_uid: Optional[str] = None
    created_at: datetime
