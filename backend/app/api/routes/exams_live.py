"""Live-control ticket issuance and the WebSocket event stream.

Ticket issuance is a normal authenticated REST call (Bearer token). The
socket handshake itself only carries the short-lived ticket as a query
param (browsers cannot set an Authorization header on a WebSocket upgrade)
— the ticket is proof of a REAL prior auth check, single-use, and expires in
seconds (contract: "authenticated WebSocket ticket admission... no
privileged event before authentication").

The socket NEVER accepts client-sent commands — it only streams snapshot/
state-changed/heartbeat events outward. Every mutation still goes through
the existing REST command/student endpoints with their own transactional
guards (contract: "Socket events never override server guards").
"""

from __future__ import annotations

import asyncio
import logging
import queue
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from firebase_admin import firestore
from pydantic import BaseModel

from app.api.exams_dependencies import _has_institution_faculty_membership, get_current_exam_user, require_exams_enabled
from app.core.config import settings
from app.core.firebase import db
from app.domain.exams.commands import ExamCommandService
from app.domain.live.connection_manager import Connection, live_connection_manager
from app.domain.live.errors import LiveDomainError, TicketNotFoundError
from app.domain.live.events import snapshot_event
from app.domain.live.models import LiveRole
from app.domain.live.repository import TicketRepository
from app.domain.live.tickets import mint_ticket, verify_and_consume_ticket

logger = logging.getLogger("hirelog.exams.live")

router = APIRouter(prefix="/api/exams/v1/live", tags=["exams-live"], dependencies=[Depends(require_exams_enabled)])

_HEARTBEAT_INTERVAL_SECONDS = 15.0


class TicketRequest(BaseModel):
    run_id: str


def _resolve_role(run, uid: str, email: str) -> LiveRole:
    is_faculty = (email and email in settings.exam_faculty_emails_set()) or _has_institution_faculty_membership(uid)
    if is_faculty:
        return LiveRole.FACULTY
    if uid in run.enrolled_uids:
        return LiveRole.STUDENT
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled or faculty on this run.")


@router.post("/ticket")
def issue_ticket(payload: TicketRequest, user: dict = Depends(get_current_exam_user)) -> dict:
    run = ExamCommandService(db).runs.get(payload.run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    role = _resolve_role(run, user["uid"], str(user.get("email") or "").strip().lower())

    now = datetime.now(timezone.utc)
    ticket = mint_ticket(run_id=payload.run_id, uid=user["uid"], role=role, now=now, ttl_seconds=settings.EXAM_LIVE_TICKET_TTL_SECONDS)
    TicketRepository(db).create(ticket)
    return {"ticket": ticket.id, "role": role.value, "expires_at": ticket.expires_at.isoformat()}


@router.websocket("/ws")
async def live_ws(websocket: WebSocket, ticket: str, run_id: str) -> None:
    origin = websocket.headers.get("origin", "")
    allowed = settings.allowed_origins_list()
    if allowed and "*" not in allowed and origin.rstrip("/") not in allowed:
        await websocket.close(code=4403, reason="origin_not_allowed")
        return

    await websocket.accept()
    repo = TicketRepository(db)
    transaction = db.transaction()

    @firestore.transactional
    def _consume(transaction):
        stored = repo.get_in_transaction(transaction, ticket)
        if stored is None:
            raise TicketNotFoundError(f"ticket {ticket!r} not found")
        consumed = verify_and_consume_ticket(stored, uid=stored.uid, run_id=run_id, now=datetime.now(timezone.utc))
        repo.mark_consumed_in_transaction(transaction, consumed.id, consumed_at=consumed.consumed_at)
        return consumed

    try:
        consumed = _consume(transaction)
    except LiveDomainError as exc:
        await websocket.close(code=4401, reason=exc.code)
        return

    connection = Connection(connection_id=uuid4().hex, run_id=run_id, role=consumed.role, uid=consumed.uid)
    live_connection_manager.register(connection)
    logger.info("live ws admitted: run=%s uid=%s role=%s", run_id, consumed.uid, consumed.role.value)

    run = ExamCommandService(db).runs.get(run_id)
    snapshot_seq = live_connection_manager.current_seq(run_id)
    await websocket.send_json(
        snapshot_event(
            run_id=run_id, seq=snapshot_seq, state_version=run.version if run else 0, now=datetime.now(timezone.utc)
        ).model_dump(mode="json")
    )

    async def _drain_and_send() -> None:
        while True:
            try:
                event = await asyncio.to_thread(connection.queue.get, True, _HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                continue  # no event within the heartbeat window — client's own ping/pong keeps the socket alive
            await websocket.send_json(event.model_dump(mode="json"))

    async def _receive_loop() -> None:
        # The socket never accepts commands — this loop exists only to
        # detect client disconnects/pings promptly; any received frame is
        # discarded.
        while True:
            await websocket.receive_text()

    sender = asyncio.ensure_future(_drain_and_send())
    receiver = asyncio.ensure_future(_receive_loop())
    try:
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        live_connection_manager.unregister(connection)
        logger.info("live ws closed: run=%s uid=%s role=%s", run_id, consumed.uid, consumed.role.value)
