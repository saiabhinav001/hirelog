"""Hard limits for KT (knowledge-transfer document) ingestion.

Every limit here is enforced BEFORE the corresponding expensive/risky work
(parsing, decompression) happens, not after — a file that fails a limit is
rejected without ever being handed to pypdf/python-docx.
"""

from __future__ import annotations

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB — matches typical lecture-note PDFs
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024  # zip-bomb guard for DOCX (a zip container)
MAX_ZIP_ENTRY_COUNT = 2000
MAX_ZIP_COMPRESSION_RATIO = 100  # reject if uncompressed/compressed exceeds this
PARSE_TIMEOUT_SECONDS = 20.0
MAX_PAGES = 500
MAX_EXTRACTED_TEXT_BYTES = 20 * 1024 * 1024

ALLOWED_CONTENT_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
}

# URL resolver (source_registry / url_resolver)
URL_FETCH_TIMEOUT_SECONDS = 10.0
URL_MAX_REDIRECTS = 3
URL_MAX_RESPONSE_BYTES = MAX_UPLOAD_BYTES
ALLOWED_URL_SCHEMES = frozenset({"https"})
