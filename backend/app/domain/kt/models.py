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


class ExtractedAnchor(BaseModel):
    """One addressable location inside an extracted document."""

    anchor_id: str
    page: Optional[int] = None
    paragraph_index: int
    text: str = Field(max_length=4000)


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
    created_at: datetime
    updated_at: datetime
