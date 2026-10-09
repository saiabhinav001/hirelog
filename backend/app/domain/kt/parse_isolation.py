"""Run a KT parser in a killable child process (ADR 0020).

A thread timeout cannot stop a stuck native call; killing a process can. The
child gets no environment secrets, a memory cap (POSIX) and a wall-clock
deadline. Failures are classified so the teacher sees an actionable reason:
`parse_timeout`, `parse_crash`, plus whatever the parser itself rejects with
(`encrypted`, `no_text`, `corrupt`, ...).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.domain.kt.limits import PARSE_TIMEOUT_SECONDS
from app.domain.kt.models import ExtractedAnchor, ExtractionWarning
from app.domain.kt.parsers import ParseResult

BACKEND_ROOT = Path(__file__).resolve().parents[3]
MAX_CHILD_OUTPUT_BYTES = 64 * 1024 * 1024


class IngestionParseError(Exception):
    """A document that cannot be turned into text. `code` is stable; `message` is shown to the teacher."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _child_env(memory_mb: int) -> dict[str, str]:
    env = {
        "PYTHONPATH": str(BACKEND_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "KT_PARSE_MEMORY_MB": str(memory_mb),
    }
    for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def parse_in_subprocess(
    kind: str, content: bytes, *, timeout: float = PARSE_TIMEOUT_SECONDS, memory_mb: int = 1024
) -> ParseResult:
    try:
        completed = subprocess.run(
            [sys.executable, "-s", "-m", "app.domain.kt.parse_worker", kind],
            input=content, capture_output=True, timeout=timeout, cwd=str(BACKEND_ROOT), env=_child_env(memory_mb),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestionParseError(
            "parse_timeout",
            f"Reading the document took longer than {int(timeout)} seconds and was stopped. Try a smaller or simpler file.",
            retryable=True,
        ) from exc

    out = completed.stdout
    if completed.returncode != 0 or not out or len(out) > MAX_CHILD_OUTPUT_BYTES:
        raise IngestionParseError(
            "parse_crash",
            "The document reader stopped unexpectedly. The file may be damaged; re-export it and upload again.",
            retryable=True,
        )
    try:
        payload = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IngestionParseError("parse_crash", "The document reader returned an unreadable result.", retryable=True) from exc

    if not payload.get("ok"):
        raise IngestionParseError(str(payload.get("code", "corrupt")), str(payload.get("message", "The document could not be read.")))
    return ParseResult(
        anchors=[ExtractedAnchor.model_validate(a) for a in payload["anchors"]],
        total_pages=payload.get("total_pages"),
        truncated=bool(payload.get("truncated")),
        warnings=[ExtractionWarning.model_validate(w) for w in payload.get("warnings", [])],
        stats=payload.get("stats") or {},
    )
