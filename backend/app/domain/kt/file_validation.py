"""Structural validation for uploaded KT files — runs BEFORE any parser sees
the bytes. Pure functions over raw bytes; no Firestore, no I/O beyond the
in-memory zip inspection needed for the decompression-bomb guard.
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
# A .docx with a VBA project embedded is functionally a macro-bearing
# document even though the extension/content-type claims plain .docx.
_MACRO_ENTRY_NAMES = {"word/vbaProject.bin", "vbaProject.bin"}
_ENCRYPTED_DOCX_MARKER = "EncryptedPackage"


@dataclass(frozen=True)
class FileValidationResult:
    ok: bool
    detected_kind: Optional[str] = None  # "pdf" | "docx" | "txt"
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
    hand to python-docx. Never fully decompresses — reads only the central
    directory's declared sizes."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRY_COUNT:
                return f"zip contains {len(infos)} entries (limit {MAX_ZIP_ENTRY_COUNT})"

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
    except zipfile.BadZipFile:
        return "not a valid zip/docx container"
    return None


def validate_upload_bytes(content: bytes, *, declared_content_type: str) -> FileValidationResult:
    if len(content) == 0:
        return FileValidationResult(ok=False, reason="empty file")
    if len(content) > MAX_UPLOAD_BYTES:
        return FileValidationResult(ok=False, reason=f"file exceeds {MAX_UPLOAD_BYTES} byte limit")

    if content.startswith(_PDF_MAGIC):
        if declared_content_type not in ("application/pdf",):
            return FileValidationResult(ok=False, reason="content-type does not match PDF magic bytes")
        # Encryption is checked properly by the PDF parser itself (needs a
        # real PDF object model); a fast reject on the common RC4/AES marker
        # catches most cases here without fully parsing.
        if b"/Encrypt" in content[: min(len(content), 4096)] or b"/Encrypt" in content[-4096:]:
            return FileValidationResult(ok=False, reason="PDF appears to be encrypted")
        return FileValidationResult(ok=True, detected_kind="pdf")

    if content.startswith(_ZIP_MAGIC):
        if declared_content_type not in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ):
            return FileValidationResult(ok=False, reason="content-type does not match DOCX magic bytes")
        bomb_or_macro_reason = _inspect_zip_for_bomb_and_macros(content)
        if bomb_or_macro_reason:
            return FileValidationResult(ok=False, reason=bomb_or_macro_reason)
        return FileValidationResult(ok=True, detected_kind="docx")

    if declared_content_type == "text/plain" and _looks_like_text(content):
        return FileValidationResult(ok=True, detected_kind="txt")

    return FileValidationResult(ok=False, reason="unsupported or unrecognized file type")
