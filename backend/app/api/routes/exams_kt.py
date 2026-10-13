"""Faculty KT ingestion routes (Prompt 4, ADR 0020) - private upload/quarantine, the
durable scan+parse pipeline and the approved-source registry.

Uploads are validated, stored privately and queued; scanning and parsing happen in
the outbox worker, so a request never parses a file. No uploaded file or storage
path is ever returned to a client - only status, warnings and extracted text
(which is untrusted data: render it as text, never as markup).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled
from app.api.exams_errors import raise_for_domain_error
from app.api.exams_scope import authorize_upload, filter_accessible_sources, require_source_scope, require_upload_scope
from app.core.config import settings
from app.core.firebase import exam_db as db
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.kt.file_validation import validate_fetched_bytes, validate_upload_bytes
from app.domain.kt.ingestion import DuplicateUploadError, KtIngestionService, UploadLimitError
from app.domain.kt.limits import MAX_UPLOAD_BYTES
from app.domain.kt.malware_scan import DevBypassScanner, MalwareScanner, build_scanner
from app.domain.kt.models import ApprovedSource, RightsEvidence, SourceReviewStatus, UploadedSource, UploadStatus
from app.domain.kt.repository import ApprovedSourceRepository, UploadedSourceRepository
from app.domain.kt.source_registry import decide_approve_source, decide_reject_source
from app.domain.kt.storage import StorageNotConfiguredError, kt_storage
from app.domain.kt.url_resolver import LimitedHttpClient, UnsafeUrlError, fetch_url_safely, validate_url_scheme_and_host

logger = logging.getLogger("hirelog.exams.kt")

router = APIRouter(
    prefix="/api/exams/v1/faculty/kt",
    tags=["exams-kt"],
    dependencies=[Depends(require_exams_enabled)],
)

_upload_limiter = SlidingWindowLimiter(30, 60, namespace="kt_upload")
_url_limiter = SlidingWindowLimiter(10, 60, namespace="kt_url_fetch")

_scanner_singleton: MalwareScanner | None = None


def _scanner() -> MalwareScanner:
    """Fail closed unless EXAM_MALWARE_SCANNER_ENDPOINT names a clamd daemon (docs/adr/0020)."""
    global _scanner_singleton
    if _scanner_singleton is None:
        if settings.malware_scan_dev_bypass_allowed():
            logger.warning("EXAM_MALWARE_SCAN_DEV_BYPASS is on - KT uploads are NOT virus-scanned (development only)")
            _scanner_singleton = DevBypassScanner()
            return _scanner_singleton
        _scanner_singleton = build_scanner(settings.EXAM_MALWARE_SCANNER_ENDPOINT)
        if settings.EXAM_MALWARE_SCANNER_ENDPOINT and not _scanner_singleton.configured:
            logger.warning("EXAM_MALWARE_SCANNER_ENDPOINT is set but not a valid host:port - uploads stay SCAN_UNAVAILABLE")
    return _scanner_singleton


def _service() -> KtIngestionService:
    try:
        storage = kt_storage()
    except StorageNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "kt_storage_not_configured", "message": "Private source storage is not configured on this deployment."},
        ) from exc
    return KtIngestionService(
        db, storage, _scanner(), max_active_uploads=settings.EXAM_KT_MAX_ACTIVE_UPLOADS_PER_USER,
        parse_memory_mb=settings.EXAM_KT_PARSE_MEMORY_MB,
    )


def _uploads_repo() -> UploadedSourceRepository:
    return UploadedSourceRepository(db)


def _sources_repo() -> ApprovedSourceRepository:
    return ApprovedSourceRepository(db)


class RegisterUploadSourceRequest(BaseModel):
    upload_id: str


class RegisterUrlSourceRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class ReviewSourceRequest(BaseModel):
    decision: Literal["approve", "reject"]
    rights_statement: str | None = Field(default=None, max_length=1000)
    rejection_reason: str | None = Field(default=None, max_length=500)


def _summary(upload: UploadedSource) -> dict:
    """Teacher-facing view: no storage path, no bytes."""
    return {
        "id": upload.id, "filename": upload.original_filename, "kind": upload.detected_kind,
        "source_kind": upload.source_kind, "size_bytes": upload.size_bytes, "sha256": upload.sha256,
        "status": upload.status.value, "failure_code": upload.failure_code, "failure_reason": upload.rejection_reason,
        "ingest_attempt": upload.ingest_attempt, "final_url": upload.final_url,
        "fetched_at": upload.fetched_at.isoformat() if upload.fetched_at else None,
        "latest_extracted_version_id": upload.latest_extracted_version_id,
        "replaces_upload_id": upload.replaces_upload_id, "superseded_by_upload_id": upload.superseded_by_upload_id,
        "created_at": upload.created_at.isoformat(), "updated_at": upload.updated_at.isoformat(),
        "deleted_at": upload.deleted_at.isoformat() if upload.deleted_at else None,
    }


def _version_summary(version) -> dict:
    return {
        "id": version.id, "version_number": version.version_number, "created_at": version.created_at.isoformat(),
        "anchor_count": version.anchor_count if version.anchor_count is not None else len(version.anchors),
        "total_pages": version.total_pages, "truncated": version.truncated, "content_sha256": version.content_sha256,
        "parser": version.parser, "stats": version.stats,
        "warnings": [w.model_dump(mode="json") for w in version.warnings],
    }


def _reject_invalid(service: KtIngestionService, user: dict, filename: str, declared: str, content: bytes, reason: str):
    upload = service.record_rejected(
        owner_uid=user["uid"], filename=filename, declared_type=declared, content=content, reason=reason
    )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "invalid_file", "message": reason, "upload_id": upload.id},
    )


def _accept(service: KtIngestionService, **kwargs) -> UploadedSource:
    try:
        return service.accept(**kwargs)
    except UploadLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"code": "upload_limit_reached", "message": str(exc), "limit": exc.limit}
        ) from exc
    except DuplicateUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "duplicate_upload", "message": str(exc), "existing_upload_id": exc.existing_upload_id},
        ) from exc
    except Exception as exc:
        raise_for_domain_error(exc)
        raise


async def _read_limited(file: UploadFile) -> bytes:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="file too large")
    return content


async def _validated_upload(service: KtIngestionService, file: UploadFile, user: dict):
    content = await _read_limited(file)
    filename = file.filename or "unknown"
    declared = file.content_type or "application/octet-stream"
    result = validate_upload_bytes(content, declared_content_type=declared, filename=filename)
    if not result.ok:
        _reject_invalid(service, user, filename, declared, content, result.reason or "invalid file")
    return content, filename, declared, result.detected_kind


@router.post("/uploads", status_code=status.HTTP_202_ACCEPTED)
async def upload_kt_file(file: UploadFile = File(...), user: dict = Depends(require_exam_faculty)) -> dict:
    """Validate, quarantine and queue. Scanning and parsing run in the worker: poll `GET /uploads/{id}`."""
    _upload_limiter.check(user["uid"], detail="Too many uploads. Please slow down.")
    service = _service()
    content, filename, declared, kind = await _validated_upload(service, file, user)
    upload = _accept(
        service, owner_uid=user["uid"], filename=filename, declared_type=declared, content=content, detected_kind=kind
    )
    return {"upload_id": upload.id, "status": upload.status.value, "queued": True}


@router.get("/uploads")
def list_uploads(user: dict = Depends(require_exam_faculty), include_deleted: bool = False) -> dict:
    """The caller's own uploads, newest first - status and failure reason for the progress view."""
    uploads = _uploads_repo().list_by_owner(user["uid"])
    if not include_deleted:
        uploads = [u for u in uploads if u.status != UploadStatus.DELETED]
    uploads.sort(key=lambda u: u.created_at, reverse=True)
    return {"results": [_summary(u) for u in uploads[:200]]}


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str, user: dict = Depends(require_upload_scope)) -> dict:
    repo = _uploads_repo()
    upload = repo.get(upload_id)
    if upload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="upload not found")
    return {
        **_summary(upload),
        "versions": [_version_summary(v) for v in repo.list_extracted_versions(upload_id)],
        "sources": [{"id": s.id, "review_status": s.review_status.value} for s in _sources_repo().list_by_upload(upload_id)],
    }


@router.get("/uploads/{upload_id}/versions/{version_id}/anchors")
def get_extracted_anchors(
    upload_id: str, version_id: str, offset: int = 0, limit: int = 50, user: dict = Depends(require_upload_scope)
) -> dict:
    """Extracted text for teacher review, paged. It is untrusted document content: show it as plain text."""
    version = _uploads_repo().get_extracted_version(upload_id, version_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="extracted version not found")
    start = max(0, offset)
    page = version.anchors[start:start + max(1, min(limit, 200))]
    return {
        "version": _version_summary(version), "offset": start,
        "anchors": [a.model_dump(mode="json") for a in page],
        "next_offset": start + len(page) if start + len(page) < len(version.anchors) else None,
    }


@router.post("/uploads/{upload_id}/retry")
def retry_upload(upload_id: str, user: dict = Depends(require_upload_scope)) -> dict:
    """New processing attempt for a failed upload (e.g. scanner was down). History and audit are kept."""
    try:
        upload = _service().retry(upload_id, actor_uid=user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return _summary(upload)


@router.post("/uploads/{upload_id}/replace", status_code=status.HTTP_202_ACCEPTED)
async def replace_upload(
    upload_id: str, file: UploadFile = File(...), user: dict = Depends(require_upload_scope)
) -> dict:
    """Upload a corrected file. The old upload is kept and marked superseded; register the new one as a source."""
    _upload_limiter.check(user["uid"], detail="Too many uploads. Please slow down.")
    service = _service()
    content, filename, declared, kind = await _validated_upload(service, file, user)
    upload = _accept(
        service, owner_uid=user["uid"], filename=filename, declared_type=declared, content=content,
        detected_kind=kind, replaces_upload_id=upload_id,
    )
    return {"upload_id": upload.id, "replaces_upload_id": upload_id, "status": upload.status.value, "queued": True}


@router.delete("/uploads/{upload_id}")
def delete_upload(upload_id: str, user: dict = Depends(require_upload_scope)) -> dict:
    """Delete the file and its extracted text. The metadata row and audit trail remain."""
    try:
        upload = _service().delete(upload_id, actor_uid=user["uid"])
    except Exception as exc:
        raise_for_domain_error(exc)
        raise
    return _summary(upload)


@router.post("/sources/from-upload")
def register_source_from_upload(
    payload: RegisterUploadSourceRequest, user: dict = Depends(require_exam_faculty)
) -> dict:
    upload = authorize_upload(user, payload.upload_id)
    if upload.status != UploadStatus.PARSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"upload is not parsed/clean yet (status={upload.status.value})",
        )
    now = datetime.now(timezone.utc)
    source = ApprovedSource(
        id=uuid4().hex, kind="upload", upload_id=upload.id, extracted_version_id=upload.latest_extracted_version_id,
        review_status=SourceReviewStatus.PENDING, created_by=user["uid"], created_at=now, updated_at=now,
    )
    _sources_repo().create(source)
    return source.model_dump(mode="json")


@router.post("/sources/from-url", status_code=status.HTTP_202_ACCEPTED)
def register_source_from_url(payload: RegisterUrlSourceRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    """Fetch (SSRF-safe, size/time bounded), then queue the bytes through the same scan+parse pipeline."""
    _url_limiter.check(user["uid"], detail="Too many link submissions. Please slow down.")
    allowed = settings.exam_kt_url_allowed_hosts()
    try:
        validate_url_scheme_and_host(payload.url, allowed_hosts=allowed)
        with httpx.Client() as client:
            fetched = fetch_url_safely(payload.url, http_client=LimitedHttpClient(client), allowed_hosts=allowed)
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "unsafe_or_unreachable_url", "message": str(exc)}
        ) from exc

    service = _service()
    checked = validate_fetched_bytes(fetched.content, content_type=fetched.content_type)
    if not checked.ok:
        _reject_invalid(service, user, fetched.final_url, fetched.content_type or "unknown", fetched.content, checked.reason or "unsupported")
    upload = _accept(
        service, owner_uid=user["uid"], filename=fetched.final_url, declared_type=fetched.content_type or "unknown",
        content=fetched.content, detected_kind=checked.detected_kind, source_kind="url", origin_url=payload.url,
        final_url=fetched.final_url, fetched_at=fetched.fetched_at,
    )

    now = datetime.now(timezone.utc)
    source = ApprovedSource(
        id=uuid4().hex, kind="url", upload_id=upload.id, url=fetched.final_url,
        review_status=SourceReviewStatus.PENDING, created_by=user["uid"], created_at=now, updated_at=now,
    )
    _sources_repo().create(source)
    return {"source": source.model_dump(mode="json"), "upload_id": upload.id, "status": upload.status.value}


@router.get("/sources")
def list_sources(user: dict = Depends(require_exam_faculty)) -> dict:
    sources = filter_accessible_sources(user, _sources_repo().list_all())
    return {"results": [s.model_dump(mode="json") for s in sources]}


@router.get("/sources/{source_id}")
def get_source(source_id: str, user: dict = Depends(require_source_scope)) -> dict:
    source = _sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return source.model_dump(mode="json")


@router.post("/sources/{source_id}/review")
def review_source(source_id: str, payload: ReviewSourceRequest, user: dict = Depends(require_source_scope)) -> dict:
    repo = _sources_repo()
    source = repo.get(source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")

    now = datetime.now(timezone.utc)
    try:
        if payload.decision == "approve":
            if not payload.rights_statement:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rights_statement is required to approve")
            pinned_version = source.extracted_version_id
            if source.upload_id:
                upload = _uploads_repo().get(source.upload_id)
                if upload is None or upload.status != UploadStatus.PARSED:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"code": "content_not_ready", "message": "The source text has not been extracted yet; it cannot be approved."},
                    )
                pinned_version = upload.latest_extracted_version_id  # approval pins the exact text that was reviewed
            evidence = RightsEvidence(statement=payload.rights_statement, attested_by=user["uid"], attested_at=now)
            updated = decide_approve_source(source, reviewer=user["uid"], rights_evidence=evidence, now=now)
            updated = updated.model_copy(update={"extracted_version_id": pinned_version})
        else:
            updated = decide_reject_source(
                source, reviewer=user["uid"], reason=payload.rejection_reason or "rejected", now=now
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    repo.save(updated)
    return updated.model_dump(mode="json")
