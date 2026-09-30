"""Pure ticket lifecycle decisions — no Firestore dependency, so admission
logic is fully unit-testable without mocking (functional-core pattern used
throughout this codebase)."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from app.domain.live.errors import (
    TicketAlreadyConsumedError,
    TicketExpiredError,
    TicketScopeMismatchError,
)
from app.domain.live.models import LiveRole, LiveTicket

DEFAULT_TICKET_TTL_SECONDS = 60


def mint_ticket(*, run_id: str, uid: str, role: LiveRole, now: datetime, ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS) -> LiveTicket:
    return LiveTicket(
        id=uuid4().hex, run_id=run_id, uid=uid, role=role,
        issued_at=now, expires_at=now + timedelta(seconds=ttl_seconds),
    )


def verify_and_consume_ticket(ticket: LiveTicket, *, uid: str, run_id: str, now: datetime) -> LiveTicket:
    """Returns the consumed ticket (caller persists it) or raises. Checked in
    this exact order so the error always reflects the FIRST real problem."""
    if ticket.consumed:
        raise TicketAlreadyConsumedError(f"ticket {ticket.id} was already used")
    if ticket.uid != uid or ticket.run_id != run_id:
        raise TicketScopeMismatchError(f"ticket {ticket.id} is not valid for this caller/run")
    if now > ticket.expires_at:
        raise TicketExpiredError(f"ticket {ticket.id} expired at {ticket.expires_at.isoformat()}")
    return ticket.model_copy(update={"consumed": True, "consumed_at": now})
