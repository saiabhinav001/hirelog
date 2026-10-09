"""Structural validation for uploaded KT files - runs BEFORE any parser sees
the bytes. Pure functions over raw bytes; no Firestore, no I/O beyond the
in-memory zip inspection needed for the decompression-bomb guard.

Three things must agree for an upload to be accepted: the real signature
(magic bytes), the declared MIME type and the file extension. The client's
MIME type and filename are never trusted on their own.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Optional

from app.domain.kt.limits import (
    MAX_UNCOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_ZIP_COMPRESSION_RATIO,
    MAX_ZIP_ENTRY_COUNT,
)

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# A .docx with a VBA project embedded is functionally a macro-bearing
# document even though the extension/content-type claims plain .docx.
_MACRO_ENTRY_NAMES = {"word/vbaProject.bin", "vbaProject.bin"}
_ENCRYPTED_DOCX_MARKER = "EncryptedPackage"
_REQUIRED_DOCX_ENTRIES = ("[Content_Types].xml", "word/document.xml")

EXTENSIONS_BY_KIND = {"pdf": (".pdf",), "docx": (".docx",), "txt": (".txt", ".text"), "html": (".html", ".htm")}


@dataclass(frozen=True)
class FileValidationResult:
    ok: bool
    detected_kind: Optional[str] = None  # "pdf" | "docx" | "txt" | "html"
    reason: Optional[str] = None


def _looks_like_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _inspect_zip_for_bomb_and_macros(content: bytes) -> Optional[str]:
    """Returns a rejection reason, or None if the zip container is safe to
    hand to python-docx. Never fully decompresses - reads only the central
    directory's declared sizes."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRY_COUNT:
                return f"zip contains {len(infos)} entries (limit {MAX_ZIP_ENTRY_COUNT})"

            names = {info.filename for info in infos}
            total_uncompressed = 0
            for info in infos:
                if info.filename in _MACRO_ENTRY_NAMES:
                    return "document contains an embedded macro project (vbaProject.bin)"
                if _ENCRYPTED_DOCX_MARKER in info.filename:
                    return "document is password-protected/encrypted"
                total_uncompressed += info.file_size
                if info.compress_size > 0:
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > MAX_ZIP_COMPRESSION_RATIO:
                        return "zip entry compression ratio exceeds safety limit (possible zip bomb)"
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    return f"uncompressed size exceeds {MAX_UNCOMPRESSED_BYTES} bytes (possible zip bomb)"
            if any(required not in names for required in _REQUIRED_DOCX_ENTRIES):
                return "the file is a zip archive but not a Word (.docx) document"
    except zipfile.BadZipFile:
        return "not a valid zip/docx container"
    return None


def _check_pdf(content: bytes) -> FileValidationResult:
    # Encryption is checked properly by the PDF parser itself (needs a real PDF
    # object model); a fast reject on the common marker catches most cases here.
    if b"/Encrypt" in content[:4096] or b"/Encrypt" in content[-4096:]:
        return FileValidationResult(ok=False, reason="PDF appears to be encrypted")
    return FileValidationResult(ok=True, detected_kind="pdf")


def _check_docx(content: bytes) -> FileValidationResult:
    reason = _inspect_zip_for_bomb_and_macros(content)
    if reason:
        return FileValidationResult(ok=False, reason=reason)
    return FileValidationResult(ok=True, detected_kind="docx")


def _extension_matches(filename: Optional[str], kind: str) -> bool:
    if not filename:
        return True  # callers that cannot supply a name (URL fetches) skip this check
    return filename.lower().endswith(EXTENSIONS_BY_KIND[kind])


def validate_upload_bytes(
    content: bytes, *, declared_content_type: str, filename: Optional[str] = None
) -> FileValidationResult:
    if len(content) == 0:
        return FileValidationResult(ok=False, reason="empty file")
    if len(content) > MAX_UPLOAD_BYTES:
        return FileValidationResult(ok=False, reason=f"file exceeds {MAX_UPLOAD_BYTES} byte limit")

    if content.startswith(_PDF_MAGIC):
        if declared_content_type not in ("application/pdf",):
            return FileValidationResult(ok=False, reason="content-type does not match PDF magic bytes")
        result = _check_pdf(content)
        kind = "pdf"
    elif content.startswith(_ZIP_MAGIC):
        if declared_content_type not in (_DOCX_TYPE,):
            return FileValidationResult(ok=False, reason="content-type does not match DOCX magic bytes")
        result = _check_docx(content)
        kind = "docx"
    elif declared_content_type == "text/plain" and _looks_like_text(content):
        result, kind = FileValidationResult(ok=True, detected_kind="txt"), "txt"
    else:
        return FileValidationResult(ok=False, reason="unsupported or unrecognized file type")

    if result.ok and not _extension_matches(filename, kind):
        expected = " or ".join(EXTENSIONS_BY_KIND[kind])
        return FileValidationResult(ok=False, reason=f"file extension does not match its content (a {kind.upper()} file must end in {expected})")
    return result


def validate_fetched_bytes(content: bytes, *, content_type: Optional[str]) -> FileValidationResult:
    """A fetched web resource: the server's Content-Type is a hint, the bytes decide."""
    if len(content) == 0:
        return FileValidationResult(ok=False, reason="the page was empty")
    if len(content) > MAX_UPLOAD_BYTES:
        return FileValidationResult(ok=False, reason=f"resource exceeds {MAX_UPLOAD_BYTES} byte limit")
    if content.startswith(_PDF_MAGIC):
        return _check_pdf(content)
    if content.startswith(_ZIP_MAGIC):
        return _check_docx(content)
    if not _looks_like_text(content):
        return FileValidationResult(ok=False, reason="unsupported resource type (only PDF, DOCX, HTML and plain text are read)")
    declared = (content_type or "").split(";")[0].strip().lower()
    head = content[:2048].lower()
    if declared in ("text/html", "application/xhtml+xml") or b"<!doctype html" in head or b"<html" in head:
        return FileValidationResult(ok=True, detected_kind="html")
    return FileValidationResult(ok=True, detected_kind="txt")
