"""Policy schema validation and outbox drain-worker idempotency tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.exams.models import OutboxEntry
from app.domain.exams.outbox import LoggingNoopHandler, drain_outbox_once
from app.domain.exams.policy import AccommodationWindow, ExamPolicy

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_policy_defaults_warning_threshold_to_three() -> None:
    policy = ExamPolicy(duration_minutes=60)
    assert policy.warning_threshold == 3


def test_policy_can_customize_warning_threshold() -> None:
    policy = ExamPolicy(duration_minutes=60, warning_threshold=5)
    assert policy.warning_threshold == 5


def test_policy_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, sneaky_field="x")


def test_policy_accommodation_extra_minutes_lookup() -> None:
    policy = ExamPolicy(
        duration_minutes=60,
        accommodations={"u1": AccommodationWindow(extra_minutes=15, note="screen reader")},
    )
    assert policy.extra_minutes_for("u1") == 15
    assert policy.extra_minutes_for("unknown-uid") == 0


def test_policy_rejects_empty_accommodation_uid() -> None:
    with pytest.raises(ValidationError):
        ExamPolicy(duration_minutes=60, accommodations={"": AccommodationWindow(extra_minutes=10)})


# ── Outbox drain worker ───────────────────────────────────────────────────────

class _FakeOutboxRepo:
    """In-memory stand-in for `OutboxRepository` — proves the drain loop's
    claim/handle/mark-processed/retry contract without needing Firestore."""

    def __init__(self, entries: list[OutboxEntry]) -> None:
        self._entries = {e.id: e for e in entries}
        self.processed_ids: list[str] = []
        self.failed_calls: list[tuple[str, int]] = []

    def claim_batch(self, *, limit, lease_seconds, worker_id, now):
        return [e for e in self._entries.values() if not e.processed][:limit]

    def mark_processed(self, entry_id, *, now):
        self.processed_ids.append(entry_id)
        self._entries[entry_id].processed = True

    def mark_failed(self, entry_id, *, error, attempts):
        self.failed_calls.append((entry_id, attempts))


class _FailingHandler:
    def handle(self, entry: OutboxEntry) -> None:
        raise RuntimeError("boom")


def test_drain_processes_unprocessed_entries_idempotently() -> None:
    entry = OutboxEntry(id="e1", kind="run_command", run_id="run-1", created_at=NOW)
    repo = _FakeOutboxRepo([entry])
    handled = drain_outbox_once(repo, LoggingNoopHandler(), now=NOW, limit=10)
    assert handled == 1
    assert repo.processed_ids == ["e1"]


def test_drain_survives_handler_failure_and_records_attempt() -> None:
    """Handler failures never crash the drain loop — the entry stays
    unprocessed for the next tick (at-least-once delivery)."""
    entry = OutboxEntry(id="e1", kind="run_command", run_id="run-1", created_at=NOW)
    repo = _FakeOutboxRepo([entry])
    handled = drain_outbox_once(repo, _FailingHandler(), now=NOW, limit=10, max_attempts=5)
    assert handled == 0
    assert repo.failed_calls == [("e1", 1)]
    assert repo.processed_ids == []
