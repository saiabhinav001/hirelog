"""Bounded document parsers producing immutable, anchored extracted text.

Each parser is wrapped with a wall-clock timeout (`run_with_timeout`) — on
Windows/CPython there is no clean way to hard-kill a stuck native call from
pure Python without a subprocess, so this is a best-effort bound, not a true
sandbox. Production should run parsing in an isolated worker process with a
hard kill, the same way Prompt 6 isolates the code judge (see docs/adr/0009
"Known limitations").
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Protocol
from uuid import uuid4

from app.domain.kt.limits import MAX_EXTRACTED_TEXT_BYTES, MAX_PAGES, PARSE_TIMEOUT_SECONDS
from app.domain.kt.models import ExtractedAnchor

_parse_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kt-parse")


class ParseTimeoutError(Exception):
    pass


class ParseResult:
    def __init__(self, anchors: list[ExtractedAnchor], total_pages: int | None, truncated: bool) -> None:
        self.anchors = anchors
        self.total_pages = total_pages
        self.truncated = truncated


class DocumentParser(Protocol):
    def parse(self, content: bytes) -> ParseResult: ...


def run_with_timeout(fn, *args, timeout: float = PARSE_TIMEOUT_SECONDS):
    future = _parse_pool.submit(fn, *args)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        raise ParseTimeoutError(f"parse job exceeded {timeout}s") from exc


def _make_anchor(page: int | None, paragraph_index: int, text: str) -> ExtractedAnchor:
    return ExtractedAnchor(
        anchor_id=uuid4().hex, page=page, paragraph_index=paragraph_index, text=text[:4000]
    )


class TxtParser:
    def parse(self, content: bytes) -> ParseResult:
        text = content.decode("utf-8", errors="replace")
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        anchors: list[ExtractedAnchor] = []
        total_bytes = 0
        truncated = False
        for idx, para in enumerate(paragraphs):
            total_bytes += len(para.encode("utf-8"))
            if total_bytes > MAX_EXTRACTED_TEXT_BYTES:
                truncated = True
                break
            anchors.append(_make_anchor(None, idx, para))
        return ParseResult(anchors=anchors, total_pages=None, truncated=truncated)


class DocxParser:
    def parse(self, content: bytes) -> ParseResult:
        import io

        import docx  # python-docx

        document = docx.Document(io.BytesIO(content))
        anchors: list[ExtractedAnchor] = []
        total_bytes = 0
        truncated = False
        for idx, paragraph in enumerate(document.paragraphs):
            text = paragraph.text.strip()
            if not text:
                continue
            total_bytes += len(text.encode("utf-8"))
            if total_bytes > MAX_EXTRACTED_TEXT_BYTES:
                truncated = True
                break
            anchors.append(_make_anchor(None, idx, text))
        return ParseResult(anchors=anchors, total_pages=None, truncated=truncated)


class PdfParser:
    def parse(self, content: bytes) -> ParseResult:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("PDF is encrypted")

        anchors: list[ExtractedAnchor] = []
        total_bytes = 0
        truncated = False
        total_pages = len(reader.pages)
        pages_to_read = min(total_pages, MAX_PAGES)
        if total_pages > MAX_PAGES:
            truncated = True

        paragraph_index = 0
        for page_num in range(pages_to_read):
            page_text = reader.pages[page_num].extract_text() or ""
            for para in (p.strip() for p in page_text.split("\n\n")):
                if not para:
                    continue
                total_bytes += len(para.encode("utf-8"))
                if total_bytes > MAX_EXTRACTED_TEXT_BYTES:
                    truncated = True
                    return ParseResult(anchors=anchors, total_pages=total_pages, truncated=True)
                anchors.append(_make_anchor(page_num + 1, paragraph_index, para))
                paragraph_index += 1

        return ParseResult(anchors=anchors, total_pages=total_pages, truncated=truncated)


PARSERS_BY_KIND: dict[str, DocumentParser] = {
    "txt": TxtParser(),
    "docx": DocxParser(),
    "pdf": PdfParser(),
}


def parse_bounded(kind: str, content: bytes) -> ParseResult:
    parser = PARSERS_BY_KIND.get(kind)
    if parser is None:
        raise ValueError(f"no parser registered for kind={kind!r}")
    return run_with_timeout(parser.parse, content)
