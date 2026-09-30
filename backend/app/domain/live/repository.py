"""Firestore repository for live-control tickets. Private collection — no
client rule grants it; covered by the default-deny catch-all.
"""

from __future__ import annotations

from typing import Optional

from app.domain.live.models import LiveTicket

TICKETS_COLLECTION = "live_tickets"


class TicketRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, ticket_id: str):
        return self._db.collection(TICKETS_COLLECTION).document(ticket_id)

    def create(self, ticket: LiveTicket) -> None:
        self._ref(ticket.id).set(ticket.model_dump(mode="json"))

    def get(self, ticket_id: str) -> Optional[LiveTicket]:
        snap = self._ref(ticket_id).get()
        if not snap.exists:
            return None
        return LiveTicket.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def get_in_transaction(self, transaction, ticket_id: str) -> Optional[LiveTicket]:
        snap = self._ref(ticket_id).get(transaction=transaction)
        if not snap.exists:
            return None
        return LiveTicket.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def mark_consumed_in_transaction(self, transaction, ticket_id: str, *, consumed_at) -> None:
        transaction.update(self._ref(ticket_id), {"consumed": True, "consumed_at": consumed_at.isoformat()})
