"""Student-facing browser observation reporting + evidence snapshot upload,
and faculty-facing incident review/resolution + evidence view + appeals.

Browser telemetry is CLIENT-REPORTED, not cryptographic proof — every
faculty-facing DTO here says so. Camera pictures are not automatic
identity/liveness proof — evidence viewing is a manual review aid, audited
on every access, never an automated decision input.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import get_current_exam_user, require_exam_faculty, require_exams_enabled
from app.api.exams_errors import raise_for_domain_error
from app.core.config import settings
from app.core.firebase import db
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.exams.repository import AttemptRepository
from app.domain.live.publisher import publish_attempt_status_changed
from app.domain.proctoring.appeals_service import AppealsService
from app.domain.proctoring.evidence_service import EvidenceService
from app.domain.proctoring.ledger_service import ProctoringLedgerService
from app.domain.proctoring.models import AppealStatus, EvidenceSnapshotKind, ObservationKind

MAX_SNAPSHOT_BYTES = 2_000_000  # 2 MB — a single compressed still frame, never video

student_router = APIRouter(
    prefix="/api/exams/v1/student/proctoring", tags=["exams-proctoring-student"],
    dependencies=[Depends(require_exams_enabled)],
)
faculty_router = APIRouter(
    prefix="/api/exams/v1/faculty/proctoring", tags=["exams-proctoring-faculty"],
    dependencies=[Depends(require_exams_enabled)],
)

# Generous but bounded — legitimate rapid focus-switching must not lock a
# student out, but an unbounded client cannot flood the ledger either.
_observation_limiter = SlidingWindowLimiter(120, 60, namespace="proctoring_observation")


def _evidence_storage():
    from app.domain.proctoring.evidence_storage import FirebaseStorageAdapter, InMemoryEvidenceStorage

    if settings.EXAM_EVIDENCE_STORAGE_BUCKET:
        return FirebaseStorageAdapter(bucket_name=settings.EXAM_EVIDENCE_STORAGE_BUCKET)
    return InMemoryEvidenceStorage()


_shared_storage = None


def _storage():
    # A single process-wide in-memory store so an upload in one request is
    # readable by a later view request in the same process — the same
    # single-process caveat already documented for the live connection
    # manager (ADR 0012 §4); a real deployment sets EXAM_EVIDENCE_STORAGE_BUCKET.
    global _shared_storage
    if _shared_storage is None:
        _shared_storage = _evidence_storage()
    return _shared_storage


class ObservationRequest(BaseModel):
    kind: ObservationKind
    client_event_id: str = Field(min_length=1, max_length=200)
    client_timestamp: datetime


class SnapshotRequest(BaseModel):
    kind: EvidenceSnapshotKind


class MissingSnapshotRequest(BaseModel):
    kind: EvidenceSnapshotKind
    reason: str = Field(min_length=1, max_length=200)


class AppealRequest(BaseModel):
    student_statement: str = Field(min_length=1, max_length=4000)


class ResolveBlockRequest(BaseModel):
    allow_continue: bool
    notes: str = Field(default="", max_length=1000)


class ResolveAppealRequest(BaseModel):
    status: AppealStatus
    resolution_notes: str = Field(default="", max_length=1000)


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@student_router.post("/runs/{run_id}/observations")
def record_observation(run_id: str, payload: ObservationRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    _observation_limiter.check(f"{run_id}:{user['uid']}", detail="Too many observations reported. Please slow down.")

    service = ProctoringLedgerService(db)
    run = service.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    # Threshold is per-run policy, not a global default (contract #5:
    # "Default warning threshold is 3; policy can customize it.").
    outcome = service.record_observation(
        run_id=run_id, uid=user["uid"], kind=payload.kind, client_event_id=payload.client_event_id,
        client_timestamp=payload.client_timestamp, grace_seconds=settings.EXAM_PROCTORING_GRACE_SECONDS,
        cooldown_seconds=settings.EXAM_PROCTORING_COOLDOWN_SECONDS, threshold=run.policy.warning_threshold,
    )
    if outcome.should_block:
        updated_attempt = service.attempts.get(run_id, user["uid"])
        if updated_attempt is not None:
            publish_attempt_status_changed(updated_attempt, now=datetime.now(timezone.utc))
    return {"action": outcome.action, "warning_count": outcome.new_state.counted_incident_count}


@student_router.get("/runs/{run_id}/incidents")
def list_my_incidents(run_id: str, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """Visible count explanations — a student can always see why their own
    warning count is what it is (contract: 'visible count explanations')."""
    _set_no_store(response)
    entries = ProctoringLedgerService(db).log.list_for_attempt(run_id, user["uid"])
    return {"results": [e.model_dump(mode="json") for e in entries]}


@student_router.post("/runs/{run_id}/snapshots")
def request_snapshot(run_id: str, payload: SnapshotRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    service = EvidenceService(db, _storage())
    snapshot, ticket = service.request_snapshot(
        run_id=run_id, uid=user["uid"], kind=payload.kind, upload_ticket_ttl_seconds=settings.EXAM_EVIDENCE_UPLOAD_TICKET_TTL_SECONDS,
    )
    return {"snapshot_id": snapshot.id, "upload_ticket": ticket.id, "expires_at": ticket.expires_at.isoformat()}


@student_router.post("/snapshots/upload/{ticket_id}")
async def upload_snapshot(ticket_id: str, response: Response, file: UploadFile = File(...), user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    content = await file.read(MAX_SNAPSHOT_BYTES + 1)
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="snapshot too large")
    declared_sha256 = hashlib.sha256(content).hexdigest()

    service = EvidenceService(db, _storage())
    try:
        snapshot = service.confirm_upload(
            ticket_id=ticket_id, uid=user["uid"], content=content, declared_sha256=declared_sha256,
            content_type=file.content_type or "application/octet-stream", max_size_bytes=MAX_SNAPSHOT_BYTES,
        )
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return snapshot.model_dump(mode="json")


@student_router.post("/runs/{run_id}/snapshots/missing")
def record_missing_snapshot(run_id: str, payload: MissingSnapshotRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    """The client calls this when permission was denied or capture failed —
    an honest, complete outcome, not an error (contract: submission
    acceptance never depends on this)."""
    _set_no_store(response)
    snapshot = EvidenceService(db, _storage()).record_missing(run_id=run_id, uid=user["uid"], kind=payload.kind, reason=payload.reason)
    return snapshot.model_dump(mode="json")


@student_router.post("/runs/{run_id}/appeals")
def submit_appeal(run_id: str, payload: AppealRequest, response: Response, user: dict = Depends(get_current_exam_user)) -> dict:
    _set_no_store(response)
    appeal = AppealsService(db).submit_appeal(run_id=run_id, uid=user["uid"], student_statement=payload.student_statement)
    return appeal.model_dump(mode="json")


# ── Faculty ──────────────────────────────────────────────────────────────────

@faculty_router.get("/runs/{run_id}/uids/{uid}/incidents")
def list_incidents_for_student(run_id: str, uid: str, user: dict = Depends(require_exam_faculty)) -> dict:
    entries = ProctoringLedgerService(db).log.list_for_attempt(run_id, uid)
    return {"results": [e.model_dump(mode="json") for e in entries], "client_reported_disclaimer": "Browser telemetry is client-reported, not cryptographic proof of misconduct."}


@faculty_router.post("/runs/{run_id}/uids/{uid}/resolve")
def resolve_block(run_id: str, uid: str, payload: ResolveBlockRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    service = ProctoringLedgerService(db)
    try:
        service.resolve_block(run_id=run_id, uid=uid, resolved_by=user["uid"], allow_continue=payload.allow_continue, notes=payload.notes)
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    updated_attempt = AttemptRepository(db).get(run_id, uid)
    if updated_attempt is not None:
        publish_attempt_status_changed(updated_attempt, now=datetime.now(timezone.utc))
    return {"status": "resolved", "allowed_to_continue": payload.allow_continue}


@faculty_router.get("/runs/{run_id}/uids/{uid}/snapshots")
def list_snapshots_for_student(run_id: str, uid: str, user: dict = Depends(require_exam_faculty)) -> dict:
    snapshots = EvidenceService(db, _storage()).snapshots.list_for_attempt(run_id, uid)
    return {"results": [s.model_dump(mode="json") for s in snapshots]}


@faculty_router.post("/snapshots/{snapshot_id}/view-ticket")
def issue_view_ticket(snapshot_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    ticket = EvidenceService(db, _storage()).issue_view_ticket(
        snapshot_id=snapshot_id, faculty_uid=user["uid"], ttl_seconds=settings.EXAM_EVIDENCE_VIEW_TICKET_TTL_SECONDS,
    )
    return {"view_ticket": ticket.id, "expires_at": ticket.expires_at.isoformat()}


@faculty_router.get("/view/{ticket_id}")
def view_snapshot(ticket_id: str, response: Response, user: dict = Depends(require_exam_faculty)):
    _set_no_store(response)
    from fastapi import Response as RawResponse

    service = EvidenceService(db, _storage())
    try:
        content = service.view_snapshot(ticket_id=ticket_id, faculty_uid=user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return RawResponse(content=content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@faculty_router.get("/runs/{run_id}/appeals")
def list_appeals(run_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    appeals = AppealsService(db).appeals.list_for_run(run_id)
    return {"results": [a.model_dump(mode="json") for a in appeals]}


@faculty_router.post("/appeals/{appeal_id}/resolve")
def resolve_appeal(appeal_id: str, payload: ResolveAppealRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    try:
        appeal = AppealsService(db).resolve_appeal(
            appeal_id=appeal_id, resolved_by=user["uid"], status=payload.status, resolution_notes=payload.resolution_notes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return appeal.model_dump(mode="json")
