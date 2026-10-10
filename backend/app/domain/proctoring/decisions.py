"""Pure decision functions for proctoring: observation correlation (grace/
cooldown/dedup/one-continuous-departure), threshold blocking, evidence
snapshot completion validation, and retention expiry. Zero Firestore
dependency — every scenario in the Prompt 8 test list is unit-testable
directly against plain models (functional-core pattern used throughout).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional
from uuid import uuid4

from app.domain.proctoring.models import (
    DEPARTURE_END_KINDS,
    DEPARTURE_START_KINDS,
    INFORMATIONAL_KINDS,
    EvidenceSnapshot,
    EvidenceSnapshotStatus,
    IncidentLogEntry,
    ObservationKind,
    ProctoringState,
)

CorrelationAction = Literal[
    "deduped",
    "ignored_inactive",
    "informational_logged",
    "incident_opened",
    "incident_extended",
    "incident_reopened",
    "incident_closed_uncounted",
    "incident_closed_counted",
    "stray_end_ignored",
]


@dataclass(frozen=True)
class CorrelationOutcome:
    action: CorrelationAction
    new_state: ProctoringState
    log_entry: Optional[IncidentLogEntry]
    should_block: bool = False


@dataclass(frozen=True)
class EffectiveProctoring:
    grace_seconds: float
    cooldown_seconds: float
    allowance_after_review: int
    retention_days: int


def effective_proctoring(policy, *, default_grace: float, default_cooldown: float, default_retention_days: int) -> EffectiveProctoring:
    """The run's profile where it sets a value, the deployment default elsewhere. A run without a profile gets
    exactly the defaults, i.e. the behaviour from before profiles existed."""
    profile = getattr(policy, "proctoring_profile", None)
    if profile is None:
        return EffectiveProctoring(default_grace, default_cooldown, 0, default_retention_days)
    return EffectiveProctoring(
        grace_seconds=default_grace if profile.grace_seconds is None else profile.grace_seconds,
        cooldown_seconds=default_cooldown if profile.cooldown_seconds is None else profile.cooldown_seconds,
        allowance_after_review=profile.allowance_after_review,
        retention_days=default_retention_days if profile.evidence_retention_days is None else profile.evidence_retention_days,
    )


def _crosses_block_line(newly_counted: bool, new_count: int, threshold: int, review_floor: int) -> bool:
    """Blocks only when an incident was just COUNTED and the count is at the threshold and above the review floor.
    An uncounted blip never blocks, so a reviewer's "allow to continue" is not undone by the next trivial event."""
    return newly_counted and new_count >= threshold and new_count > review_floor


def _new_log_entry(
    *, run_id: str, uid: str, kind: ObservationKind, counted: bool, client_timestamp: datetime,
    now: datetime, client_event_id: str, duration_seconds: Optional[float] = None, note: str = "",
) -> IncidentLogEntry:
    return IncidentLogEntry(
        id=uuid4().hex, run_id=run_id, uid=uid, kind=kind, counted=counted,
        client_timestamp=client_timestamp, received_at=now, duration_seconds=duration_seconds,
        client_event_id=client_event_id, note=note,
    )


def correlate_observation(
    *,
    state: ProctoringState,
    kind: ObservationKind,
    client_event_id: str,
    client_timestamp: datetime,
    now: datetime,
    grace_seconds: float,
    cooldown_seconds: float,
    threshold: int,
) -> CorrelationOutcome:
    """The ENTIRE correlation contract in one place. `state` is always the
    CURRENT (possibly just-created, all-default) state for this attempt —
    the caller reads it fresh inside a transaction on every call."""

    if client_event_id in state.recent_event_ids:
        return CorrelationOutcome(action="deduped", new_state=state, log_entry=None)

    recent_ids = list(state.recent_event_ids) + [client_event_id]
    if len(recent_ids) > 64:
        recent_ids = recent_ids[-64:]
    base_state = state.model_copy(update={"recent_event_ids": recent_ids, "version": state.version + 1, "updated_at": now})

    if kind in INFORMATIONAL_KINDS:
        entry = _new_log_entry(
            run_id=state.run_id, uid=state.uid, kind=kind, counted=False,
            client_timestamp=client_timestamp, now=now, client_event_id=client_event_id,
            note="informational — never counted",
        )
        return CorrelationOutcome(action="informational_logged", new_state=base_state, log_entry=entry)

    if kind in DEPARTURE_END_KINDS:
        if base_state.open_incident_started_at is None:
            entry = _new_log_entry(
                run_id=state.run_id, uid=state.uid, kind=kind, counted=False,
                client_timestamp=client_timestamp, now=now, client_event_id=client_event_id,
                note="no open incident — stray end event ignored",
            )
            return CorrelationOutcome(action="stray_end_ignored", new_state=base_state, log_entry=entry)

        duration = (client_timestamp - base_state.open_incident_started_at).total_seconds()
        qualifies = duration >= grace_seconds
        # Only bump the counter once per logical incident — a reopened
        # (flapped) incident that already counted on an earlier close must
        # not count again when it closes a second time.
        newly_incremented = qualifies and not base_state.open_incident_already_counted
        new_count = base_state.counted_incident_count + (1 if newly_incremented else 0)
        closed_state = base_state.model_copy(update={
            "open_incident_started_at": None,
            "open_incident_last_activity_at": None,
            "open_incident_already_counted": False,
            "last_closed_incident_started_at": base_state.open_incident_started_at,
            "last_closed_incident_ended_at": client_timestamp,
            "last_closed_incident_counted": qualifies,
            "counted_incident_count": new_count,
        })
        entry = _new_log_entry(
            run_id=state.run_id, uid=state.uid, kind=kind, counted=qualifies,
            client_timestamp=client_timestamp, now=now, client_event_id=client_event_id,
            duration_seconds=duration,
            note="counted departure" if qualifies else "below grace period — not counted",
        )
        return CorrelationOutcome(
            action="incident_closed_counted" if qualifies else "incident_closed_uncounted",
            new_state=closed_state, log_entry=entry,
            should_block=_crosses_block_line(newly_incremented, new_count, threshold, base_state.review_floor),
        )

    # DEPARTURE_START_KINDS
    assert kind in DEPARTURE_START_KINDS
    if base_state.open_incident_started_at is not None:
        extended_state = base_state.model_copy(update={"open_incident_last_activity_at": client_timestamp})
        entry = _new_log_entry(
            run_id=state.run_id, uid=state.uid, kind=kind, counted=False,
            client_timestamp=client_timestamp, now=now, client_event_id=client_event_id,
            note="extended already-open incident",
        )
        return CorrelationOutcome(action="incident_extended", new_state=extended_state, log_entry=entry)

    if (
        base_state.last_closed_incident_ended_at is not None
        and (client_timestamp - base_state.last_closed_incident_ended_at).total_seconds() <= cooldown_seconds
    ):
        reopened_state = base_state.model_copy(update={
            "open_incident_started_at": base_state.last_closed_incident_started_at,
            "open_incident_last_activity_at": client_timestamp,
            "open_incident_already_counted": base_state.last_closed_incident_counted,
            "last_closed_incident_started_at": None,
            "last_closed_incident_ended_at": None,
            "last_closed_incident_counted": False,
        })
        entry = _new_log_entry(
            run_id=state.run_id, uid=state.uid, kind=kind, counted=False,
            client_timestamp=client_timestamp, now=now, client_event_id=client_event_id,
            note="within cooldown — reopened as continuation of the same departure",
        )
        return CorrelationOutcome(action="incident_reopened", new_state=reopened_state, log_entry=entry)

    opened_state = base_state.model_copy(update={
        "open_incident_started_at": client_timestamp,
        "open_incident_last_activity_at": client_timestamp,
        "open_incident_already_counted": False,
    })
    entry = _new_log_entry(
        run_id=state.run_id, uid=state.uid, kind=kind, counted=False,
        client_timestamp=client_timestamp, now=now, client_event_id=client_event_id,
        note="new departure started",
    )
    return CorrelationOutcome(action="incident_opened", new_state=opened_state, log_entry=entry)


def close_stale_open_incident(
    *, state: ProctoringState, now: datetime, grace_seconds: float, threshold: int, stale_after_seconds: float,
) -> Optional[CorrelationOutcome]:
    """Defensive sweep path: a student who closes the tab entirely while
    'away' never sends the END event. Called from finalization/periodic
    sweeps, never from the request path. Returns None if there's nothing
    stale to close."""
    if state.open_incident_last_activity_at is None or state.open_incident_started_at is None:
        return None
    if (now - state.open_incident_last_activity_at).total_seconds() < stale_after_seconds:
        return None

    # The incident is still ongoing as far as anyone has reported — closing
    # it "now" (not at the last-seen activity time) reflects that its true
    # duration only just became known to be over this long.
    duration = (now - state.open_incident_started_at).total_seconds()
    counted = duration >= grace_seconds and not state.open_incident_already_counted
    new_count = state.counted_incident_count + (1 if counted else 0)
    closed_state = state.model_copy(update={
        "open_incident_started_at": None,
        "open_incident_last_activity_at": None,
        "open_incident_already_counted": False,
        "last_closed_incident_started_at": state.open_incident_started_at,
        "last_closed_incident_ended_at": now,
        "last_closed_incident_counted": counted,
        "counted_incident_count": new_count,
        "version": state.version + 1,
        "updated_at": now,
    })
    entry = _new_log_entry(
        run_id=state.run_id, uid=state.uid, kind=ObservationKind.PAGE_HIDDEN, counted=counted,
        client_timestamp=state.open_incident_last_activity_at, now=now, client_event_id=f"sweep-close-{uuid4().hex}",
        duration_seconds=duration, note="auto-closed stale open incident (no end event received)",
    )
    return CorrelationOutcome(
        action="incident_closed_counted" if counted else "incident_closed_uncounted",
        new_state=closed_state, log_entry=entry,
        should_block=_crosses_block_line(counted, new_count, threshold, state.review_floor),
    )


@dataclass(frozen=True)
class SnapshotCompletionOutcome:
    accepted: bool
    conflict_reason: Optional[str] = None


def detect_image_type(content: bytes) -> Optional[str]:
    """The type the BYTES say they are (JPEG, PNG or WebP); the client's declared type is never trusted."""
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def decide_confirm_snapshot_upload(
    *, snapshot: EvidenceSnapshot, declared_sha256: str, actual_sha256: str, size_bytes: int, max_size_bytes: int,
    image_ok: bool = True,
) -> SnapshotCompletionOutcome:
    if snapshot.status != EvidenceSnapshotStatus.REQUESTED:
        return SnapshotCompletionOutcome(False, conflict_reason="snapshot_not_pending")
    if size_bytes > max_size_bytes:
        return SnapshotCompletionOutcome(False, conflict_reason="payload_too_large")
    if declared_sha256 != actual_sha256:
        return SnapshotCompletionOutcome(False, conflict_reason="hash_mismatch")
    if not image_ok:
        return SnapshotCompletionOutcome(False, conflict_reason="not_an_image")
    return SnapshotCompletionOutcome(True)


def is_past_retention(reference_time: datetime, *, now: datetime, retention_days: int) -> bool:
    return now > reference_time + timedelta(days=retention_days)
