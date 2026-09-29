"""Faculty KT ingestion routes — private upload/quarantine/scan/parse and the
approved-source registry (URL submissions go through the SSRF-safe resolver).
No uploaded file is ever returned to a client; only status/metadata is.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exam_faculty, require_exams_enabled
from app.core.config import settings
from app.core.firebase import db
from app.domain.kt.file_validation import validate_upload_bytes
from app.domain.kt.limits import MAX_UPLOAD_BYTES
from app.domain.kt.malware_scan import NullMalwareScanner
from app.domain.kt.models import (
    ApprovedSource,
    ExtractedVersion,
    RightsEvidence,
    SourceReviewStatus,
    UploadedSource,
    UploadStatus,
)
from app.domain.kt.parsers import ParseTimeoutError, parse_bounded
from app.domain.kt.repository import ApprovedSourceRepository, UploadedSourceRepository
from app.domain.kt.source_registry import decide_approve_source, decide_reject_source
from app.domain.kt.url_resolver import UnsafeUrlError, fetch_url_safely, validate_url_scheme_and_host

logger = logging.getLogger("hirelog.exams.kt")

router = APIRouter(
    prefix="/api/exams/v1/faculty/kt",
    tags=["exams-kt"],
    dependencies=[Depends(require_exams_enabled)],
)

if settings.EXAM_MALWARE_SCANNER_ENDPOINT:
    # No real scanner adapter is implemented yet (see docs/adr/0009) — warn
    # loudly rather than let an operator believe scanning is active.
    logger.warning(
        "EXAM_MALWARE_SCANNER_ENDPOINT is set but no scanner adapter reads it yet — "
        "uploads still use the fail-closed NullMalwareScanner (never marked clean)."
    )


_scanner = NullMalwareScanner()  # fail-closed default — see docs/adr/0009


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


@router.post("/uploads")
async def upload_kt_file(file: UploadFile = File(...), user: dict = Depends(require_exam_faculty)) -> dict:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="file too large")

    declared_type = file.content_type or "application/octet-stream"
    result = validate_upload_bytes(content, declared_content_type=declared_type)

    now = datetime.now(timezone.utc)
    upload_id = uuid4().hex
    repo = _uploads_repo()

    if not result.ok:
        rejected = UploadedSource(
            id=upload_id, uploaded_by=user["uid"], original_filename=file.filename or "unknown",
            declared_content_type=declared_type, detected_kind=None, size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(), status=UploadStatus.REJECTED,
            rejection_reason=result.reason, storage_ref=f"kt-uploads/{upload_id}", created_at=now, updated_at=now,
        )
        repo.create(rejected)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=result.reason)

    upload = UploadedSource(
        id=upload_id, uploaded_by=user["uid"], original_filename=file.filename or "unknown",
        declared_content_type=declared_type, detected_kind=result.detected_kind, size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(), status=UploadStatus.QUARANTINED,
        storage_ref=f"kt-uploads/{upload_id}", created_at=now, updated_at=now,
    )
    repo.create(upload)

    scan = _scanner.scan(content)
    if not scan.clean:
        new_status = UploadStatus.INFECTED if _scanner_reports_infection(scan) else UploadStatus.SCAN_UNAVAILABLE
        repo.update_status(upload_id, status=new_status, reason=scan.detail, now=now)
        return {"upload_id": upload_id, "status": new_status.value, "detail": scan.detail}

    try:
        parsed = parse_bounded(result.detected_kind, content)
    except ParseTimeoutError as exc:
        repo.update_status(upload_id, status=UploadStatus.PARSE_FAILED, reason=str(exc), now=now)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a parser bug must not 500-crash the API
        repo.update_status(upload_id, status=UploadStatus.PARSE_FAILED, reason=str(exc), now=now)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"parse failed: {exc}") from exc

    version = ExtractedVersion(
        id=uuid4().hex, source_id=upload_id, version_number=1, created_at=now,
        anchors=parsed.anchors, total_pages=parsed.total_pages, truncated=parsed.truncated,
    )
    repo.create_extracted_version(version)
    repo.update_status(upload_id, status=UploadStatus.PARSED, reason=None, now=now)

    return {
        "upload_id": upload_id,
        "status": UploadStatus.PARSED.value,
        "extracted_version_id": version.id,
        "anchor_count": len(parsed.anchors),
        "truncated": parsed.truncated,
    }


def _scanner_reports_infection(scan) -> bool:
    return bool(scan.detail) and "no malware scanner configured" not in scan.detail


@router.post("/sources/from-upload")
def register_source_from_upload(
    payload: RegisterUploadSourceRequest, user: dict = Depends(require_exam_faculty)
) -> dict:
    upload = _uploads_repo().get(payload.upload_id)
    if upload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="upload not found")
    if upload.status != UploadStatus.PARSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"upload is not parsed/clean yet (status={upload.status.value})",
        )
    now = datetime.now(timezone.utc)
    source = ApprovedSource(
        id=uuid4().hex, kind="upload", upload_id=upload.id, review_status=SourceReviewStatus.PENDING,
        created_at=now, updated_at=now,
    )
    _sources_repo().create(source)
    return source.model_dump(mode="json")


@router.post("/sources/from-url")
def register_source_from_url(payload: RegisterUrlSourceRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    try:
        validate_url_scheme_and_host(payload.url)
        fetched = fetch_url_safely(payload.url, http_client=httpx.Client())
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    now = datetime.now(timezone.utc)
    upload_id = uuid4().hex
    repo = _uploads_repo()
    # Fetched content is treated as inert data — same quarantine path as a
    # direct upload, never executed/interpreted, never re-served publicly.
    upload = UploadedSource(
        id=upload_id, uploaded_by=user["uid"], original_filename=fetched.final_url,
        declared_content_type=fetched.content_type or "text/plain", detected_kind="txt",
        size_bytes=len(fetched.content), sha256=hashlib.sha256(fetched.content).hexdigest(),
        status=UploadStatus.QUARANTINED, storage_ref=f"kt-uploads/{upload_id}", created_at=now, updated_at=now,
    )
    repo.create(upload)

    now2 = datetime.now(timezone.utc)
    source = ApprovedSource(
        id=uuid4().hex, kind="url", upload_id=upload_id, url=fetched.final_url,
        review_status=SourceReviewStatus.PENDING, created_at=now2, updated_at=now2,
    )
    _sources_repo().create(source)
    return {"source": source.model_dump(mode="json"), "upload_id": upload_id}


@router.get("/sources/{source_id}")
def get_source(source_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    source = _sources_repo().get(source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return source.model_dump(mode="json")


@router.post("/sources/{source_id}/review")
def review_source(source_id: str, payload: ReviewSourceRequest, user: dict = Depends(require_exam_faculty)) -> dict:
    repo = _sources_repo()
    source = repo.get(source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")

    now = datetime.now(timezone.utc)
    try:
        if payload.decision == "approve":
            if not payload.rights_statement:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rights_statement is required to approve")
            evidence = RightsEvidence(statement=payload.rights_statement, attested_by=user["uid"], attested_at=now)
            updated = decide_approve_source(source, reviewer=user["uid"], rights_evidence=evidence, now=now)
        else:
            updated = decide_reject_source(
                source, reviewer=user["uid"], reason=payload.rejection_reason or "rejected", now=now
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    repo.save(updated)
    return updated.model_dump(mode="json")
