"""Idempotent outbox drain worker.

Delivery is at-least-once (claim can race a crash between "handle" and "mark
processed"). `OutboxHandler.handle` MUST therefore be safe to call more than
once for the same entry. This phase ships a logging no-op default handler —
real consumers (grading trigger in Prompt 6, live-control notify in Prompt 7)
register their own handler later without changing the drain loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from app.domain.exams.models import OutboxEntry
from app.domain.exams.repository import OutboxRepository

logger = logging.getLogger("hirelog.exams.outbox")


class OutboxHandler(Protocol):
    def handle(self, entry: OutboxEntry) -> None: ...


class LoggingNoopHandler:
    """Default handler: proves delivery, does nothing else. Idempotent by
    construction (logging twice is harmless)."""

    def handle(self, entry: OutboxEntry) -> None:
        logger.info(
            "exam outbox entry delivered: id=%s kind=%s run_id=%s",
            entry.id,
            entry.kind,
            entry.run_id,
        )


class CompositeOutboxHandler:
    """Fans one entry out to every registered handler — each handler decides
    for itself (via `entry.kind`) whether it applies. This is the seam real
    consumers plug into (Prompt 6's grading trigger, Prompt 7's live-control
    notify) without ever changing `drain_outbox_once`."""

    def __init__(self, handlers: list[OutboxHandler]) -> None:
        self._handlers = handlers

    def handle(self, entry: OutboxEntry) -> None:
        for handler in self._handlers:
            handler.handle(entry)


def drain_outbox_once(
    repo: OutboxRepository,
    handler: OutboxHandler,
    *,
    now: datetime,
    limit: int = 25,
    lease_seconds: int = 60,
    max_attempts: int = 5,
    worker_id: str | None = None,
) -> int:
    """Process up to `limit` unprocessed entries once. Returns count handled
    (successfully or exhausted). Callers loop this on a timer/thread, mirroring
    `SearchIndexQueue`'s existing poll pattern."""

    wid = worker_id or f"exam-outbox-{uuid4().hex[:8]}"
    entries = repo.claim_batch(limit=limit, lease_seconds=lease_seconds, worker_id=wid, now=now)
    handled = 0
    for entry in entries:
        try:
            handler.handle(entry)
            repo.mark_processed(entry.id, now=now)
            handled += 1
        except Exception as exc:  # noqa: BLE001 - must not crash the drain loop
            attempts = entry.attempts + 1
            logger.exception("exam outbox handler failed for entry %s (attempt %d)", entry.id, attempts)
            repo.mark_failed(entry.id, error=str(exc), attempts=attempts)
            if attempts >= max_attempts:
                logger.critical(
                    "exam outbox entry %s exceeded max_attempts=%d — needs manual review",
                    entry.id,
                    max_attempts,
                )
    return handled
