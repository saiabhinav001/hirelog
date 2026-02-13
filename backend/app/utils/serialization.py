from __future__ import annotations

from datetime import datetime
from typing import Any


def _convert_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_convert_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _convert_value(val) for key, val in value.items()}
    return value


def _get_contributor_display(data: dict) -> str:
    """Derive the public contributor display string.

    STRICT ANONYMITY CONTRACT:
      - If author.visibility == "public" AND public_label exists → use public_label
      - Everything else → "Anonymous (CBIT)"

    HARD BANS:
      - No initials from email
      - No fallback to profile name
      - No UID-derived labels
      - If label is missing → treat as anonymous

    MIGRATION: Legacy docs without author.visibility are treated as anonymous.
    Any previously inferred labels (e.g. "S.A.S.") are stripped.
    """
    author = data.get("author") or {}
    visibility = author.get("visibility", "")

    # Only show a label if explicitly public AND a label was stored
    if visibility == "public":
        label = author.get("public_label", "").strip()
        if label:
            return label

    # Legacy fallback: check top-level is_anonymous flag
    # If is_anonymous is explicitly False AND show_name is True, use stored label
    if not author and data.get("is_anonymous") is False and data.get("show_name") is True:
        name = data.get("contributor_name") or ""
        if name:
            parts = name.strip().split()
            if len(parts) >= 2:
                return f"{parts[0]} {parts[-1][0].upper()}."
            elif parts:
                return parts[0]

    return "Anonymous (CBIT)"


def _ensure_questions_and_stats(result: dict) -> dict:
    """Ensure every experience doc has the nested questions + explicit stats.

    Handles backwards-compatible docs that only have the flat
    ``extracted_questions`` list by deriving the nested structure from it.
    """
    flat = result.get("extracted_questions") or []

    # Build nested questions if missing
    if not result.get("questions"):
        user_provided = [q for q in flat if isinstance(q, dict) and q.get("source") == "user"]
        ai_extracted = [q for q in flat if isinstance(q, dict) and q.get("source") != "user"]
        result["questions"] = {
            "user_provided": user_provided,
            "ai_extracted": ai_extracted,
        }

    # Build explicit stats if missing
    if not result.get("stats"):
        nested = result["questions"]
        up = nested.get("user_provided") or []
        ae = nested.get("ai_extracted") or []
        result["stats"] = {
            "user_question_count": len(up),
            "extracted_question_count": len(ae),
            "total_question_count": len(up) + len(ae),
        }

    return result


def serialize_doc(doc_snapshot, *, include_contributor: bool = False) -> dict:
    data = doc_snapshot.to_dict() or {}
    data["id"] = doc_snapshot.id
    result = _convert_value(data)
    if include_contributor:
        result["contributor_display"] = _get_contributor_display(data)
    # Always ensure nested questions + stats are present
    result = _ensure_questions_and_stats(result)
    return result
