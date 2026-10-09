"""Pure CSV -> roster rows (Prompt 2, docs/adr/0018). No I/O, no Firestore.

A college ID is an identifier, so every cell stays a string end to end — the
`csv` module never converts types, which is what preserves leading zeros. A
problem with the FILE (too large, not UTF-8, unknown or missing columns)
rejects it as a whole via `RosterCsvError`; a problem with one ROW becomes a
`RosterParseError` that the planner reports as a row-level error with its CSV
line number, so a single bad line never hides the rest of the report.

Only the three roster columns are accepted. An unknown column (a `password`
column, for example) is rejected rather than silently ignored.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Union

from pydantic import ValidationError

from app.domain.institution.models import RosterRowInput

MAX_CSV_BYTES = 1_048_576
MAX_CSV_ROWS = 5000
REQUIRED_COLUMNS = ("identity_key", "contact_email")
ALLOWED_COLUMNS = ("identity_key", "contact_email", "role_scope")
DEFAULT_ROLE_SCOPE = "student"
_CLIP = 64


class RosterCsvError(Exception):
    """The whole file is unusable (as opposed to one bad row)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RosterParseError:
    """One CSV row that could not become a `RosterRowInput`."""

    line_number: int
    identity_key: str
    error: str


RosterRowCandidate = Union[RosterRowInput, RosterParseError]


def _clip(value: str) -> str:
    return value[:_CLIP]


def _read_header(reader) -> list[str]:
    for fields in reader:
        if any(cell.strip() for cell in fields):
            header = [cell.strip().lower() for cell in fields]
            break
    else:
        raise RosterCsvError("empty_file", "The CSV has no header row.")

    if len(set(header)) != len(header):
        raise RosterCsvError("duplicate_columns", "The CSV header repeats a column name.")
    unknown = [_clip(name) for name in header if name not in ALLOWED_COLUMNS]
    if unknown:
        raise RosterCsvError(
            "unknown_columns",
            f"Unsupported column(s): {', '.join(unknown[:5])}. Allowed: {', '.join(ALLOWED_COLUMNS)}.",
        )
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise RosterCsvError("missing_columns", f"Missing required column(s): {', '.join(missing)}.")
    return header


def parse_roster_csv(data: bytes) -> list[RosterRowCandidate]:
    if len(data) > MAX_CSV_BYTES:
        raise RosterCsvError("file_too_large", f"The CSV exceeds {MAX_CSV_BYTES} bytes.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RosterCsvError("not_utf8", "The CSV must be UTF-8 encoded.") from exc
    if "\x00" in text:
        raise RosterCsvError("invalid_content", "The CSV contains NUL bytes.")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[RosterRowCandidate] = []
    try:
        header = _read_header(reader)
        for fields in reader:
            if not any(cell.strip() for cell in fields):
                continue  # blank line or a row of only separators
            if len(rows) >= MAX_CSV_ROWS:
                raise RosterCsvError("too_many_rows", f"The CSV has more than {MAX_CSV_ROWS} data rows.")
            line_number = reader.line_num
            if len(fields) != len(header):
                rows.append(
                    RosterParseError(
                        line_number, _clip(fields[0].strip()),
                        f"csv_row_has_{len(fields)}_fields_expected_{len(header)}",
                    )
                )
                continue
            record = dict(zip(header, (cell.strip() for cell in fields)))
            if not record.get("role_scope"):
                record["role_scope"] = DEFAULT_ROLE_SCOPE
            try:
                rows.append(RosterRowInput(**record))
            except ValidationError as exc:
                errors = exc.errors()
                field = str(errors[0]["loc"][0]) if errors and errors[0].get("loc") else "row"
                rows.append(RosterParseError(line_number, _clip(record.get("identity_key", "")), f"invalid_{field}"))
    except csv.Error as exc:
        raise RosterCsvError("malformed_csv", f"The CSV is malformed near line {reader.line_num}: {exc}") from exc

    if not rows:
        raise RosterCsvError("no_rows", "The CSV has a header but no data rows.")
    return rows
