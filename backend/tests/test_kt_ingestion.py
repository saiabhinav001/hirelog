"""KT ingestion: file validation, malware-scan fail-closed default, and
bounded parsers (real txt/docx/pdf extraction, no ML deps)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.domain.kt.file_validation import validate_upload_bytes
from app.domain.kt.limits import MAX_UPLOAD_BYTES, MAX_ZIP_ENTRY_COUNT
from app.domain.kt.malware_scan import DeterministicFakeScanner, NullMalwareScanner
from app.domain.kt.parsers import ParseTimeoutError, parse_bounded, run_with_timeout


def _minimal_docx_bytes(*, with_macro: bool = False, entry_count: int = 3) -> bytes:
    """A structurally-valid-enough ZIP for the file_validation structural
    checks (entry count / macro / encryption markers) — NOT a real OPC
    document, so it must not be fed to python-docx itself."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>Hello world</w:t></w:r></w:p></w:body></w:document>",
        )
        for i in range(entry_count):
            zf.writestr(f"word/media/image{i}.png", b"fake-image-bytes")
        if with_macro:
            zf.writestr("word/vbaProject.bin", b"fake-vba")
    return buf.getvalue()


def _real_docx_bytes(paragraphs: list[str]) -> bytes:
    """A genuinely OPC-valid .docx, built by python-docx itself, for tests
    that exercise the real parser (not just structural validation)."""
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_reject_empty_file() -> None:
    result = validate_upload_bytes(b"", declared_content_type="text/plain")
    assert not result.ok
    assert "empty" in result.reason


def test_reject_oversized_file() -> None:
    content = b"a" * (MAX_UPLOAD_BYTES + 1)
    result = validate_upload_bytes(content, declared_content_type="text/plain")
    assert not result.ok
    assert "exceeds" in result.reason


def test_accept_plain_text() -> None:
    result = validate_upload_bytes(b"Some KT notes.\n\nMore notes.", declared_content_type="text/plain")
    assert result.ok
    assert result.detected_kind == "txt"


def test_reject_content_type_mismatch_for_pdf_magic() -> None:
    result = validate_upload_bytes(b"%PDF-1.4 fake pdf content", declared_content_type="text/plain")
    assert not result.ok
    assert "content-type" in result.reason


def test_reject_docx_with_embedded_macro() -> None:
    content = _minimal_docx_bytes(with_macro=True)
    result = validate_upload_bytes(
        content,
        declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert not result.ok
    assert "macro" in result.reason


def test_reject_docx_with_too_many_zip_entries() -> None:
    content = _minimal_docx_bytes(entry_count=MAX_ZIP_ENTRY_COUNT + 5)
    result = validate_upload_bytes(
        content,
        declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert not result.ok
    assert "entries" in result.reason


def test_accept_clean_docx() -> None:
    content = _real_docx_bytes(["Hello world"])
    result = validate_upload_bytes(
        content,
        declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.ok
    assert result.detected_kind == "docx"


def test_reject_encrypted_docx_marker() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EncryptedPackage", b"fake-encrypted-blob")
    result = validate_upload_bytes(
        buf.getvalue(),
        declared_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert not result.ok
    assert "encrypted" in result.reason


# ── Malware scan (fail-closed default) ────────────────────────────────────────

def test_null_scanner_never_reports_clean() -> None:
    """No scanner configured => permanently unusable, never silently trusted."""
    result = NullMalwareScanner().scan(b"totally harmless content")
    assert not result.clean


def test_fake_scanner_flags_marker_content() -> None:
    scanner = DeterministicFakeScanner()
    assert scanner.scan(b"clean content").clean
    assert not scanner.scan(b"prefix EICAR-TEST-MARKER suffix").clean


# ── Bounded parsers ────────────────────────────────────────────────────────────

def test_txt_parser_extracts_paragraph_anchors() -> None:
    content = b"Paragraph one.\n\nParagraph two.\n\nParagraph three."
    result = parse_bounded("txt", content)
    assert len(result.anchors) == 3
    assert result.anchors[0].text == "Paragraph one."
    assert result.anchors[0].page is None


def test_docx_parser_extracts_paragraphs() -> None:
    content = _real_docx_bytes(["Hello world", "Second paragraph"])
    result = parse_bounded("docx", content)
    assert any("Hello world" in a.text for a in result.anchors)


def test_parse_timeout_wraps_slow_jobs() -> None:
    import time

    def _slow(_content: bytes):
        time.sleep(0.5)
        return None

    with pytest.raises(ParseTimeoutError):
        run_with_timeout(_slow, b"x", timeout=0.05)


def test_unknown_parser_kind_raises() -> None:
    with pytest.raises(ValueError):
        parse_bounded("unknown-kind", b"data")
