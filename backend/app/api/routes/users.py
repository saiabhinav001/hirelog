from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import firestore

from app.api.dependencies import get_current_user
from app.core.firebase import db
from app.models.schemas import NameUpdate, UserCreate
from app.utils.serialization import serialize_doc


router = APIRouter(prefix="/api/users", tags=["users"])

# Name changes are limited to once every 30 days
_NAME_COOLDOWN_DAYS = 30


def _derive_display_name(full_name: str) -> str:
    """Derive a public display name from a full name.

    "Abhishek Sharma" → "Abhishek S."
    "Ravi" → "Ravi"
    """
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0].upper()}."
    elif parts:
        return parts[0]
    return ""


def _enrich_user_response(data: dict) -> dict:
    """Add computed fields for the frontend: can_edit_name, next_name_edit_date, display_name."""
    now = datetime.now(timezone.utc)

    # Ensure display_name is always present
    if not data.get("display_name"):
        data["display_name"] = _derive_display_name(data.get("name", ""))

    # Compute cooldown status
    last_updated = data.get("name_last_updated_at")
    if last_updated:
        if isinstance(last_updated, str):
            try:
                last_dt = datetime.fromisoformat(last_updated)
            except (ValueError, TypeError):
                last_dt = now - timedelta(days=_NAME_COOLDOWN_DAYS + 1)
        elif isinstance(last_updated, datetime):
            last_dt = last_updated
        else:
            last_dt = now - timedelta(days=_NAME_COOLDOWN_DAYS + 1)

        # Ensure timezone-aware comparison
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)

        next_eligible = last_dt + timedelta(days=_NAME_COOLDOWN_DAYS)
        data["can_edit_name"] = now >= next_eligible
        data["next_name_edit_date"] = next_eligible.isoformat() if now < next_eligible else None
    else:
        # Never changed — always eligible
        data["can_edit_name"] = True
        data["next_name_edit_date"] = None

    return data


@router.get("/me")
def get_me(user: dict = Depends(get_current_user)) -> dict:
    snapshot = db.collection("users").document(user["uid"]).get()
    if snapshot.exists:
        result = serialize_doc(snapshot)
        return _enrich_user_response(result)
    return _enrich_user_response(user)


@router.get("/profile")
def get_profile(user: dict = Depends(get_current_user)) -> dict:
    """Aggregated user profile with contribution stats, practice activity, and privacy summary."""
    uid = user["uid"]

    # ── User identity ────────────────────────────────────────────────────
    user_snapshot = db.collection("users").document(uid).get()
    identity = serialize_doc(user_snapshot) if user_snapshot.exists else user
    identity = _enrich_user_response(identity)

    # ── Contribution stats ───────────────────────────────────────────────
    experience_snapshots = list(
        db.collection("interview_experiences")
        .where("created_by", "==", uid)
        .stream()
    )

    total_experiences = len(experience_snapshots)
    active_count = 0
    hidden_count = 0
    total_questions_extracted = 0
    questions_added_later = 0
    anonymous_count = 0
    companies: set[str] = set()
    topics_set: set[str] = set()

    for snap in experience_snapshots:
        data = snap.to_dict() or {}
        is_active = data.get("is_active", True)
        if is_active:
            active_count += 1
        else:
            hidden_count += 1
        if data.get("is_anonymous", False):
            anonymous_count += 1

        questions = data.get("extracted_questions") or []
        total_questions_extracted += len(questions)
        for q in questions:
            if isinstance(q, dict) and q.get("added_later"):
                questions_added_later += 1

        company = data.get("company")
        if company:
            companies.add(company)
        for t in (data.get("topics") or []):
            topics_set.add(t)

    contribution_summary = {
        "total_experiences": total_experiences,
        "active": active_count,
        "hidden": hidden_count,
        "questions_extracted": total_questions_extracted,
        "questions_added_later": questions_added_later,
        "anonymous_contributions": anonymous_count,
        "companies_covered": sorted(companies),
        "topics_covered": sorted(topics_set),
    }

    # ── Practice activity ────────────────────────────────────────────────
    list_snapshots = list(
        db.collection("practice_lists")
        .where("user_id", "==", uid)
        .stream()
    )

    total_lists = len(list_snapshots)
    total_questions = 0
    revised_total = 0
    practicing_total = 0
    unvisited_total = 0

    for ls in list_snapshots:
        ld = ls.to_dict() or {}
        total_questions += ld.get("question_count", 0)
        revised_total += ld.get("revised_count", 0)
        practicing_total += ld.get("practicing_count", 0)
        unvisited_total += ld.get("unvisited_count", 0)

    practice_summary = {
        "total_lists": total_lists,
        "total_questions": total_questions,
        "revised": revised_total,
        "practicing": practicing_total,
        "unvisited": unvisited_total,
    }

    return {
        "identity": identity,
        "contribution_summary": contribution_summary,
        "practice_summary": practice_summary,
    }


@router.patch("/me/name")
def update_display_name(
    payload: NameUpdate,
    user: dict = Depends(get_current_user),
) -> dict:
    """Update the user's display name.

    - Limited to once every 30 days.
    - Updates full_name, display_name, and name_last_updated_at.
    - Does NOT retroactively update past contributions (they are immutable snapshots).
    """
    doc_ref = db.collection("users").document(user["uid"])
    snapshot = doc_ref.get()

    if not snapshot.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found.",
        )

    data = snapshot.to_dict() or {}
    now = datetime.now(timezone.utc)

    # Check 30-day cooldown
    last_updated = data.get("name_last_updated_at")
    if last_updated:
        if isinstance(last_updated, str):
            try:
                last_dt = datetime.fromisoformat(last_updated)
            except (ValueError, TypeError):
                last_dt = now - timedelta(days=_NAME_COOLDOWN_DAYS + 1)
        elif isinstance(last_updated, datetime):
            last_dt = last_updated
        else:
            last_dt = now - timedelta(days=_NAME_COOLDOWN_DAYS + 1)

        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)

        next_eligible = last_dt + timedelta(days=_NAME_COOLDOWN_DAYS)
        if now < next_eligible:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Name changes are limited to once every {_NAME_COOLDOWN_DAYS} days. "
                       f"Next eligible: {next_eligible.strftime('%B %d, %Y')}.",
            )

    new_display = _derive_display_name(payload.name)

    doc_ref.update({
        "name": payload.name,
        "display_name": new_display,
        "name_last_updated_at": now.isoformat(),
    })

    result = serialize_doc(doc_ref.get())
    return _enrich_user_response(result)


@router.post("")
def create_or_update_user(payload: UserCreate, user: dict = Depends(get_current_user)) -> dict:
    doc_ref = db.collection("users").document(user["uid"])
    snapshot = doc_ref.get()

    display_name = _derive_display_name(payload.name)

    base = {
        "uid": user["uid"],
        "name": payload.name,
        "display_name": display_name,
        "email": user.get("email", ""),
        "role": payload.role,
    }

    if snapshot.exists:
        # Preserve existing identity + role on re-auth — never regress contributor → viewer
        existing = snapshot.to_dict() or {}
        if existing.get("display_name"):
            base["display_name"] = existing["display_name"]
        if existing.get("name"):
            base["name"] = existing["name"]
        if existing.get("role") == "contributor":
            base["role"] = "contributor"
        doc_ref.set({**base}, merge=True)
    else:
        doc_ref.set({**base, "created_at": firestore.SERVER_TIMESTAMP}, merge=True)

    result = serialize_doc(doc_ref.get())
    return _enrich_user_response(result)
