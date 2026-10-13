"""Child-process entry point for parsing one document: `python -m app.domain.kt.parse_worker <kind>`.

Reads the document bytes from stdin and writes ONE JSON object to stdout. It runs
with a scrubbed environment (no credentials), applies an address-space limit on
POSIX, and is killed by the parent on timeout - so a hostile file can hang or crash
only this process.
"""

from __future__ import annotations

import json
import os
import sys

MAX_OUTPUT_BYTES = 60 * 1024 * 1024


def _apply_limits() -> None:
    if os.name != "posix":
        return
    try:
        import resource

        memory_mb = int(os.environ.get("KT_PARSE_MEMORY_MB", "1024"))
        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def _emit(payload: dict) -> int:
    data = json.dumps(payload, ensure_ascii=False)
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps({"ok": False, "code": "output_too_large", "message": "The extracted text is too large to process."}).encode("utf-8")
    # Raw UTF-8 bytes: the Windows text stream is cp1252 and cannot encode arbitrary document characters.
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


def main(argv: list[str]) -> int:
    _apply_limits()
    kind = argv[1] if len(argv) > 1 else ""
    content = sys.stdin.buffer.read()

    from app.domain.kt.parsers import PARSERS_BY_KIND, ParseRejectedError, require_text

    parser = PARSERS_BY_KIND.get(kind)
    if parser is None:
        return _emit({"ok": False, "code": "unsupported_kind", "message": f"No parser for {kind!r}."})
    try:
        result = require_text(parser.parse(content), kind)
    except ParseRejectedError as exc:
        return _emit({"ok": False, "code": exc.code, "message": exc.message})
    except Exception as exc:  # noqa: BLE001 - report the type only; messages can echo document content
        return _emit({
            "ok": False, "code": "corrupt",
            "message": f"The document could not be read ({type(exc).__name__}). It may be damaged or use an unsupported feature.",
        })
    return _emit({
        "ok": True,
        "anchors": [a.model_dump(mode="json") for a in result.anchors],
        "total_pages": result.total_pages,
        "truncated": result.truncated,
        "warnings": [w.model_dump(mode="json") for w in result.warnings],
        "stats": result.stats,
    })


if __name__ == "__main__":
    sys.exit(main(sys.argv))
