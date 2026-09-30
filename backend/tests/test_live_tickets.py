"""Pure ticket lifecycle tests: mint, single-use consumption, expiry, and
scope mismatch (uid/run_id must match exactly who/what it was issued for).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.live.errors import (
    TicketAlreadyConsumedError,
    TicketExpiredError,
    TicketScopeMismatchError,
)
from app.domain.live.models import LiveRole
from app.domain.live.tickets import mint_ticket, verify_and_consume_ticket

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_mint_ticket_defaults_to_60_second_ttl() -> None:
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW)
    assert (ticket.expires_at - ticket.issued_at) == timedelta(seconds=60)
    assert ticket.consumed is False


def test_verify_and_consume_marks_consumed_and_stamps_time() -> None:
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW)
    consumed = verify_and_consume_ticket(ticket, uid="u1", run_id="run-1", now=NOW + timedelta(seconds=5))
    assert consumed.consumed is True
    assert consumed.consumed_at == NOW + timedelta(seconds=5)
    # Original object is untouched — pure function, no in-place mutation.
    assert ticket.consumed is False


def test_reject_already_consumed_ticket() -> None:
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW)
    consumed = verify_and_consume_ticket(ticket, uid="u1", run_id="run-1", now=NOW)
    with pytest.raises(TicketAlreadyConsumedError):
        verify_and_consume_ticket(consumed, uid="u1", run_id="run-1", now=NOW)


def test_reject_expired_ticket() -> None:
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW, ttl_seconds=60)
    with pytest.raises(TicketExpiredError):
        verify_and_consume_ticket(ticket, uid="u1", run_id="run-1", now=NOW + timedelta(seconds=61))


def test_reject_wrong_uid() -> None:
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW)
    with pytest.raises(TicketScopeMismatchError):
        verify_and_consume_ticket(ticket, uid="attacker", run_id="run-1", now=NOW)


def test_reject_wrong_run_id() -> None:
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW)
    with pytest.raises(TicketScopeMismatchError):
        verify_and_consume_ticket(ticket, uid="u1", run_id="run-OTHER", now=NOW)


def test_already_consumed_check_happens_before_expiry_check() -> None:
    """Ordering matters for a clean error message: an expired-AND-consumed
    ticket should report 'already consumed', not 'expired'."""
    ticket = mint_ticket(run_id="run-1", uid="u1", role=LiveRole.STUDENT, now=NOW, ttl_seconds=1)
    consumed = verify_and_consume_ticket(ticket, uid="u1", run_id="run-1", now=NOW)
    with pytest.raises(TicketAlreadyConsumedError):
        verify_and_consume_ticket(consumed, uid="u1", run_id="run-1", now=NOW + timedelta(seconds=100))
