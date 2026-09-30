"""Domain exceptions for live-control ticket admission."""

from __future__ import annotations


class LiveDomainError(Exception):
    code: str = "live_domain_error"


class TicketNotFoundError(LiveDomainError):
    code = "ticket_not_found"


class TicketExpiredError(LiveDomainError):
    code = "ticket_expired"


class TicketAlreadyConsumedError(LiveDomainError):
    """A ticket is presented twice — e.g. a replayed admission request. Never
    admit a second connection on one ticket; the client mints a fresh one for
    every reconnect attempt (contract: authenticated WS ticket admission)."""

    code = "ticket_already_consumed"


class TicketScopeMismatchError(LiveDomainError):
    """The caller presenting the ticket is not who it was issued to."""

    code = "ticket_scope_mismatch"
