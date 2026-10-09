"""KT ingestion domain models — private, server-only documents. No uploaded
file is ever served back to a browser directly (contract: "No uploaded file
is executable or public")."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class UploadStatus(str, Enum):
    QUARANTINED = "quarantined"  # just landed, not yet scanned
    SCANNING = "scanning"
    CLEAN = "clean"  # scanner says safe to parse
    INFECTED = "infected"  # terminal — never parsed, never served
    SCAN_UNAVAILABLE = "scan_unavailable"  # no scanner configured — fail closed, never parsed
    REJECTED = "rejected"  # failed structural validation (type/size/macro/encryption)
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    # Prompt 4 (ADR 0020)
    PARSING = "parsing"
    DELETED = "deleted"  # bytes and extracted text removed; metadata and audit kept


class SourceReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class UploadedSource(BaseModel):
    """One uploaded file, pre-parsing. Immutable identity; status is the only
    thing that changes as it moves through quarantine -> scan -> parse."""

    id: str
    uploaded_by: str
    original_filename: str = Field(max_length=260)
    declared_content_type: str
    detected_kind: Optional[str] = None  # "pdf" | "docx" | "txt", set after magic-byte sniff
    size_bytes: int
    sha256: str
    status: UploadStatus = UploadStatus.QUARANTINED
    rejection_reason: Optional[str] = None
    storage_ref: str  # private storage path — never a public URL
    created_at: datetime
    updated_at: datetime
    # Prompt 4 (ADR 0020) — all optional so legacy documents still validate.
    source_kind: str = "file"  # "file" | "url"
    origin_url: Optional[str] = None  # URL as submitted
    final_url: Optional[str] = None  # URL after validated redirects (the canonical URL recorded)
    fetched_at: Optional[datetime] = None
    ingest_attempt: int = 0  # bumped by retry; a queued job for an older attempt is ignored
    failure_code: Optional[str] = None  # stable machine code; `rejection_reason` is the human message
    latest_extracted_version_id: Optional[str] = None
    replaces_upload_id: Optional[str] = None
    superseded_by_upload_id: Optional[str] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None


class ExtractedAnchor(BaseModel):
    """One addressable location inside an extracted document."""

    anchor_id: str
    page: Optional[int] = None
    paragraph_index: int
    text: str = Field(max_length=4000)
    section: Optional[str] = None  # nearest preceding heading, when the format has headings


class ExtractionWarning(BaseModel):
    """Content the parser could NOT read, shown to the teacher so a silent
    omission is never mistaken for coverage."""

    code: str  # e.g. "tables_not_extracted", "images_not_extracted", "pages_without_text", "truncated"
    message: str = Field(max_length=500)
    count: Optional[int] = None
    pages: list[int] = Field(default_factory=list, max_length=200)


class ExtractedVersion(BaseModel):
    """Immutable once created — a new parse creates a NEW version, never
    edits a prior one, so anchors already cited by a published item stay
    stable."""

    id: str
    source_id: str
    version_number: int = Field(ge=1)
    created_at: datetime
    anchors: list[ExtractedAnchor] = Field(default_factory=list)
    total_pages: Optional[int] = None
    truncated: bool = False  # true if MAX_PAGES/MAX_EXTRACTED_TEXT_BYTES was hit
    # Prompt 4 (ADR 0020)
    content_sha256: Optional[str] = None  # hash of the extracted text, in anchor order
    parser: Optional[str] = None
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    ingest_attempt: Optional[int] = None
    anchor_count: Optional[int] = None
    chunk_count: int = 0  # > 0: anchors are stored in the `anchor_chunks` subcollection


class RightsEvidence(BaseModel):
    """Faculty-attested provenance/rights basis for a source — never
    auto-inferred."""

    statement: str = Field(min_length=1, max_length=1000)
    attested_by: str
    attested_at: datetime


class ApprovedSource(BaseModel):
    """Registry entry gating whether a source's anchors may ever be cited in
    a generation request. Uploaded/retrieved content is DATA, never
    instructions — nothing here executes text found in a source."""

    id: str
    kind: str  # "upload" | "url"
    upload_id: Optional[str] = None
    url: Optional[str] = None
    extracted_version_id: Optional[str] = None
    review_status: SourceReviewStatus = SourceReviewStatus.PENDING
    rights_evidence: Optional[RightsEvidence] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    # Registering teacher (Prompt 1, ADR 0017). Legacy rows have None; their
    # owner is then resolved through the linked upload's `uploaded_by`.
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
