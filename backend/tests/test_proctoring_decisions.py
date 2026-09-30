"""Pure proctoring decision tests: observation correlation (dedup/grace/
cooldown/one-continuous-departure), threshold blocking, stale-incident
sweep closure, snapshot completion validation, and retention expiry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.proctoring.decisions import (
    close_stale_open_incident,
    correlate_observation,
    decide_confirm_snapshot_upload,
    is_past_retention,
)
from app.domain.proctoring.models import EvidenceSnapshot, EvidenceSnapshotKind, EvidenceSnapshotStatus, ObservationKind, ProctoringState

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
GRACE = 2.0
COOLDOWN = 5.0
THRESHOLD = 3


def _state(**overrides) -> ProctoringState:
    defaults = dict(run_id="run-1", uid="u1")
    defaults.update(overrides)
    return ProctoringState(**defaults)


def _correlate(state, kind, *, event_id, at, now=None):
    return correlate_observation(
        state=state, kind=kind, client_event_id=event_id, client_timestamp=at, now=now or at,
        grace_seconds=GRACE, cooldown_seconds=COOLDOWN, threshold=THRESHOLD,
    )


# ── Dedup ──────────────────────────────────────────────────────────────────

def test_duplicate_event_id_is_deduped() -> None:
    state = _state(recent_event_ids=["e1"])
    outcome = _correlate(state, ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW)
    assert outcome.action == "deduped"
    assert outcome.new_state == state  # untouched


def test_burst_of_identical_events_only_processed_once() -> None:
    state = _state()
    first = _correlate(state, ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW)
    assert first.action == "incident_opened"
    second = _correlate(first.new_state, ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW)
    assert second.action == "deduped"


# ── Informational kinds — never counted ─────────────────────────────────────

def test_blur_alone_is_informational_never_counted() -> None:
    state = _state()
    outcome = _correlate(state, ObservationKind.BLUR, event_id="e1", at=NOW)
    assert outcome.action == "informational_logged"
    assert outcome.log_entry.counted is False
    assert outcome.new_state.counted_incident_count == 0
    assert outcome.should_block is False


def test_permission_denied_and_camera_error_are_informational() -> None:
    for kind in (ObservationKind.PERMISSION_DENIED, ObservationKind.CAMERA_ERROR, ObservationKind.CONNECTION_DEGRADED):
        outcome = _correlate(_state(), kind, event_id=f"e-{kind.value}", at=NOW)
        assert outcome.action == "informational_logged"
        assert outcome.new_state.counted_incident_count == 0


# ── Grace period ─────────────────────────────────────────────────────────────

def test_departure_shorter_than_grace_is_not_counted() -> None:
    opened = _correlate(_state(), ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW)
    closed = _correlate(opened.new_state, ObservationKind.PAGE_VISIBLE, event_id="e2", at=NOW + timedelta(seconds=1))
    assert closed.action == "incident_closed_uncounted"
    assert closed.new_state.counted_incident_count == 0
    assert closed.should_block is False


def test_departure_at_least_grace_length_is_counted() -> None:
    opened = _correlate(_state(), ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW)
    closed = _correlate(opened.new_state, ObservationKind.PAGE_VISIBLE, event_id="e2", at=NOW + timedelta(seconds=2))
    assert closed.action == "incident_closed_counted"
    assert closed.new_state.counted_incident_count == 1


# ── Cooldown — one continuous departure, not many ───────────────────────────

def test_flapping_within_cooldown_counts_as_one_continuous_departure() -> None:
    state = _state()
    # T0: hidden
    s1 = _correlate(state, ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW).new_state
    # T0+1s: visible (too short alone — not counted)
    s2 = _correlate(s1, ObservationKind.PAGE_VISIBLE, event_id="e2", at=NOW + timedelta(seconds=1)).new_state
    assert s2.counted_incident_count == 0
    # T0+2s: hidden again, within cooldown — reopen, preserving true start (T0)
    r = _correlate(s2, ObservationKind.PAGE_HIDDEN, event_id="e3", at=NOW + timedelta(seconds=2))
    assert r.action == "incident_reopened"
    assert r.new_state.open_incident_started_at == NOW
    # T0+5s: visible — total duration from TRUE start (T0) is 5s >= grace
    final = _correlate(r.new_state, ObservationKind.PAGE_VISIBLE, event_id="e4", at=NOW + timedelta(seconds=5))
    assert final.action == "incident_closed_counted"
    assert final.new_state.counted_incident_count == 1  # exactly ONE, not two


def test_flapping_beyond_cooldown_counts_as_two_separate_departures() -> None:
    state = _state()
    s1 = _correlate(state, ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW).new_state
    closed1 = _correlate(s1, ObservationKind.PAGE_VISIBLE, event_id="e2", at=NOW + timedelta(seconds=3)).new_state
    assert closed1.counted_incident_count == 1  # first one qualifies (3s >= 2s grace)

    # Next departure starts well beyond the cooldown window (5s) — a NEW incident.
    far_later = NOW + timedelta(seconds=30)
    opened2 = _correlate(closed1, ObservationKind.PAGE_HIDDEN, event_id="e3", at=far_later)
    assert opened2.action == "incident_opened"
    closed2 = _correlate(opened2.new_state, ObservationKind.PAGE_VISIBLE, event_id="e4", at=far_later + timedelta(seconds=3))
    assert closed2.new_state.counted_incident_count == 2


def test_already_counted_incident_does_not_double_count_on_reopen_and_reclose() -> None:
    state = _state()
    s1 = _correlate(state, ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW).new_state
    closed1 = _correlate(s1, ObservationKind.PAGE_VISIBLE, event_id="e2", at=NOW + timedelta(seconds=3)).new_state
    assert closed1.counted_incident_count == 1

    reopened = _correlate(closed1, ObservationKind.PAGE_HIDDEN, event_id="e3", at=NOW + timedelta(seconds=4)).new_state
    assert reopened.open_incident_already_counted is True
    closed2 = _correlate(reopened, ObservationKind.PAGE_VISIBLE, event_id="e4", at=NOW + timedelta(seconds=8))
    assert closed2.new_state.counted_incident_count == 1  # still one, not two


# ── Stray/edge cases ─────────────────────────────────────────────────────────

def test_stray_end_event_with_no_open_incident_is_ignored() -> None:
    outcome = _correlate(_state(), ObservationKind.PAGE_VISIBLE, event_id="e1", at=NOW)
    assert outcome.action == "stray_end_ignored"
    assert outcome.new_state.counted_incident_count == 0


def test_extending_an_already_open_incident_does_not_count_yet() -> None:
    opened = _correlate(_state(), ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW)
    extended = _correlate(opened.new_state, ObservationKind.FULLSCREEN_EXIT, event_id="e2", at=NOW + timedelta(seconds=1))
    assert extended.action == "incident_extended"
    assert extended.new_state.counted_incident_count == 0


# ── Threshold blocking ────────────────────────────────────────────────────────

def test_should_block_becomes_true_exactly_at_threshold() -> None:
    state = _state()
    for i in range(THRESHOLD):
        opened = _correlate(state, ObservationKind.PAGE_HIDDEN, event_id=f"open-{i}", at=NOW + timedelta(seconds=i * 100))
        closed = _correlate(opened.new_state, ObservationKind.PAGE_VISIBLE, event_id=f"close-{i}", at=NOW + timedelta(seconds=i * 100 + 3))
        state = closed.new_state
        if i < THRESHOLD - 1:
            assert closed.should_block is False
    assert closed.should_block is True
    assert state.counted_incident_count == THRESHOLD


# ── Stale open-incident sweep (tab closed without an end event) ─────────────

def test_stale_open_incident_is_closed_and_counted_if_long_enough() -> None:
    state = _correlate(_state(), ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW).new_state
    much_later = NOW + timedelta(seconds=200)
    outcome = close_stale_open_incident(state=state, now=much_later, grace_seconds=GRACE, threshold=THRESHOLD, stale_after_seconds=120)
    assert outcome is not None
    assert outcome.action == "incident_closed_counted"
    assert outcome.new_state.open_incident_started_at is None


def test_non_stale_open_incident_is_left_alone() -> None:
    state = _correlate(_state(), ObservationKind.PAGE_HIDDEN, event_id="e1", at=NOW).new_state
    soon_after = NOW + timedelta(seconds=10)
    outcome = close_stale_open_incident(state=state, now=soon_after, grace_seconds=GRACE, threshold=THRESHOLD, stale_after_seconds=120)
    assert outcome is None


def test_no_open_incident_sweep_is_a_noop() -> None:
    outcome = close_stale_open_incident(state=_state(), now=NOW, grace_seconds=GRACE, threshold=THRESHOLD, stale_after_seconds=120)
    assert outcome is None


# ── Snapshot completion ───────────────────────────────────────────────────────

def _snapshot(**overrides) -> EvidenceSnapshot:
    defaults = dict(id="snap-1", run_id="run-1", uid="u1", kind=EvidenceSnapshotKind.CHECK_IN, requested_at=NOW)
    defaults.update(overrides)
    return EvidenceSnapshot(**defaults)


def test_confirm_snapshot_accepts_matching_hash() -> None:
    outcome = decide_confirm_snapshot_upload(snapshot=_snapshot(), declared_sha256="abc", actual_sha256="abc", size_bytes=100, max_size_bytes=1000)
    assert outcome.accepted


def test_confirm_snapshot_rejects_hash_mismatch() -> None:
    outcome = decide_confirm_snapshot_upload(snapshot=_snapshot(), declared_sha256="abc", actual_sha256="def", size_bytes=100, max_size_bytes=1000)
    assert not outcome.accepted
    assert outcome.conflict_reason == "hash_mismatch"


def test_confirm_snapshot_rejects_oversized_payload() -> None:
    outcome = decide_confirm_snapshot_upload(snapshot=_snapshot(), declared_sha256="abc", actual_sha256="abc", size_bytes=2000, max_size_bytes=1000)
    assert not outcome.accepted
    assert outcome.conflict_reason == "payload_too_large"


def test_confirm_snapshot_rejects_already_completed_snapshot() -> None:
    snapshot = _snapshot(status=EvidenceSnapshotStatus.VERIFIED)
    outcome = decide_confirm_snapshot_upload(snapshot=snapshot, declared_sha256="abc", actual_sha256="abc", size_bytes=100, max_size_bytes=1000)
    assert not outcome.accepted
    assert outcome.conflict_reason == "snapshot_not_pending"


# ── Retention ─────────────────────────────────────────────────────────────────

def test_is_past_retention() -> None:
    assert is_past_retention(NOW, now=NOW + timedelta(days=91), retention_days=90) is True
    assert is_past_retention(NOW, now=NOW + timedelta(days=89), retention_days=90) is False
