"""CSV roster parsing (Prompt 2, docs/adr/0018): leading zeros, hostile files and
row-level errors. Pure — no Firestore, no FastAPI."""

from __future__ import annotations

import pytest

from app.domain.institution.models import RosterRowInput
from app.domain.institution.roster_csv import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    RosterCsvError,
    RosterParseError,
    parse_roster_csv,
)


def _csv(*lines: str, newline: str = "\n") -> bytes:
    return newline.join(lines).encode("utf-8")


def _code(data: bytes) -> str:
    with pytest.raises(RosterCsvError) as exc:
        parse_roster_csv(data)
    return exc.value.code


def test_leading_zeros_survive_parsing_as_strings() -> None:
    rows = parse_roster_csv(_csv("identity_key,contact_email", "007A,a@college.edu", "00012,b@college.edu"))
    assert [r.identity_key for r in rows] == ["007A", "00012"]
    assert all(isinstance(r, RosterRowInput) for r in rows)


def test_header_is_case_insensitive_and_role_scope_is_optional() -> None:
    rows = parse_roster_csv(_csv("Identity_Key, Contact_Email ", "001,a@college.edu"))
    assert rows[0].role_scope == "student"


def test_blank_role_scope_cell_defaults_to_student() -> None:
    rows = parse_roster_csv(_csv("identity_key,contact_email,role_scope", "001,a@college.edu,", "002,b@college.edu,faculty"))
    assert [r.role_scope for r in rows] == ["student", "faculty"]


def test_bom_crlf_blank_lines_and_separator_only_rows_are_tolerated() -> None:
    data = b"\xef\xbb\xbfidentity_key,contact_email\r\n\r\n001,a@college.edu\r\n,\r\n002,b@college.edu\r\n"
    rows = parse_roster_csv(data)
    assert [r.identity_key for r in rows] == ["001", "002"]


def test_quoted_fields_may_contain_commas() -> None:
    rows = parse_roster_csv(_csv("identity_key,contact_email", '"21,BCE,001",a@college.edu'))
    assert rows[0].identity_key == "21,BCE,001"


def test_a_password_column_is_rejected_not_ignored() -> None:
    """College IDs are identifiers, never passwords — and a column that looks like
    a secret must never be silently accepted."""
    assert _code(_csv("identity_key,contact_email,password", "001,a@college.edu,hunter2")) == "unknown_columns"


def test_missing_required_columns_and_duplicate_columns_are_file_errors() -> None:
    assert _code(_csv("identity_key", "001")) == "missing_columns"
    assert _code(_csv("identity_key,contact_email,identity_key", "1,a@college.edu,2")) == "duplicate_columns"


def test_empty_file_header_only_and_no_header_are_file_errors() -> None:
    assert _code(b"") == "empty_file"
    assert _code(b"   \n\n") == "empty_file"
    assert _code(_csv("identity_key,contact_email")) == "no_rows"


def test_non_utf8_and_nul_bytes_are_file_errors() -> None:
    assert _code(b"identity_key,contact_email\n\xff\xfe,a@college.edu\n") == "not_utf8"
    assert _code(b"identity_key,contact_email\n001\x00,a@college.edu\n") == "invalid_content"


def test_oversized_and_overlong_files_are_file_errors() -> None:
    assert _code(b"x" * (MAX_CSV_BYTES + 1)) == "file_too_large"
    lines = ["identity_key,contact_email"] + [f"{i},a{i}@college.edu" for i in range(MAX_CSV_ROWS + 1)]
    assert _code(_csv(*lines)) == "too_many_rows"
    assert len(parse_roster_csv(_csv(*lines[:-1]))) == MAX_CSV_ROWS  # exactly the limit is fine


def test_unterminated_quote_is_a_malformed_file() -> None:
    assert _code(_csv("identity_key,contact_email", '001,"a@college.edu')) == "malformed_csv"


def test_bad_rows_become_row_level_errors_with_csv_line_numbers() -> None:
    rows = parse_roster_csv(
        _csv(
            "identity_key,contact_email",   # line 1
            "001,a@college.edu",            # line 2
            "",                              # line 3 (blank, skipped)
            "002",                           # line 4: too few fields
            ",b@college.edu",               # line 5: empty identity key
            f"{'9' * 65},c@college.edu",    # line 6: identity key too long
            "003,d@college.edu,extra",      # line 7: too many fields
            "004,e@college.edu",            # line 8: fine again
        )
    )
    assert isinstance(rows[0], RosterRowInput)
    errors = [r for r in rows if isinstance(r, RosterParseError)]
    assert [(e.line_number, e.error) for e in errors] == [
        (4, "csv_row_has_1_fields_expected_2"),
        (5, "invalid_identity_key"),
        (6, "invalid_identity_key"),
        (7, "csv_row_has_3_fields_expected_2"),
    ]
    assert isinstance(rows[-1], RosterRowInput) and rows[-1].identity_key == "004"


def test_row_errors_do_not_hide_the_valid_rows_around_them() -> None:
    rows = parse_roster_csv(_csv("identity_key,contact_email", "001,a@college.edu", "bad", "002,b@college.edu"))
    assert [type(r) for r in rows] == [RosterRowInput, RosterParseError, RosterRowInput]
