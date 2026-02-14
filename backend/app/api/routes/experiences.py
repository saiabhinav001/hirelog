from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import firestore

from app.api.dependencies import get_current_user
from app.core.firebase import db
from app.models.schemas import AddQuestionsRequest, ExperienceCreate, ExperienceMetadataUpdate
from app.services.faiss_store import faiss_store
from app.services.nlp import pipeline
from app.utils.serialization import serialize_doc
from app.api.routes.dashboard import update_dashboard_stats_async


router = APIRouter(prefix="/api/experiences", tags=["experiences"])

# Placeholder for anonymous submissions - preserves real UID for moderation
ANONYMOUS_DISPLAY_ID = "anonymous"


def _require_ownership(experience_id: str, user_uid: str) -> dict:
    """Fetch a document and verify the current user owns it."""
    snapshot = db.collection("interview_experiences").document(experience_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experience not found.")
    data = snapshot.to_dict() or {}
    if data.get("created_by") != user_uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own contributions.",
        )
    return data


def _build_user_question_objects(question_texts: list[str], now: str) -> list[dict]:
    """Build structured question dicts for user-provided questions.

    Every user question is stored verbatim. No filtering, merging, or dropping.
    """
    results: list[dict] = []
    for q_text in question_texts:
        q_text = q_text.strip()
        if not q_text:
            continue
        results.append({
            "question_text": q_text,
            "question": q_text,  # Legacy field
            "topic": "General",  # Classification happens async
            "category": "theory",
            "confidence": 1.0,
            "question_type": "extracted",
            "source": "user",
            "added_later": False,
            "created_at": now,
            "updated_at": now,
        })
    return results


def _run_background_nlp(doc_id: str, raw_text: str, user_questions: list[dict]) -> None:
    """Background NLP enrichment — runs in a separate thread.

    Phases:
      1. Extract AI questions from raw text
      2. Classify topics on all questions (user + AI)
      3. Generate summary
      4. Compute + store FAISS embedding
      5. Write enrichment results back to Firestore

    RULE: User-provided questions are NEVER removed, filtered, or modified.
    AI-extracted questions are stored separately.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        processed = pipeline.process(raw_text)

        # Stamp AI questions
        ai_questions: list[dict] = []
        for q in processed["questions"]:
            q["created_at"] = now
            q["updated_at"] = now
            q["source"] = "ai"
            q["question_type"] = "extracted"
            ai_questions.append(q)

        # Classify user questions through the topic pipeline (non-destructive)
        enriched_user: list[dict] = []
        for uq in user_questions:
            classified = pipeline.classify_single_question(uq["question_text"])
            # Preserve all original user fields, only ADD classification
            enriched = {**uq}
            enriched["topic"] = classified.get("topic", "General")
            enriched["category"] = classified.get("category", "theory")
            enriched["source"] = "user"  # Always keep source=user
            enriched["updated_at"] = now
            enriched_user.append(enriched)

        # Merge for legacy flat list: user first, then AI
        combined_flat = enriched_user + ai_questions

        # Build nested questions structure
        questions_nested = {
            "user_provided": enriched_user,
            "ai_extracted": ai_questions,
        }

        # Explicit stats
        stats = {
            "user_question_count": len(enriched_user),
            "extracted_question_count": len(ai_questions),
            "total_question_count": len(enriched_user) + len(ai_questions),
        }

        # Re-derive topics from all questions + raw text
        all_topics: set[str] = set(processed["topics"] or [])
        for q in combined_flat:
            topic = q.get("topic", "General")
            if topic and topic != "General":
                all_topics.add(topic)

        # FAISS embedding from full context
        all_question_texts = " ".join(
            q.get("question_text", "") for q in combined_flat
        )
        full_text = f"{raw_text} {all_question_texts}".strip()
        try:
            embedding = pipeline.embed(full_text)
            embedding_id = faiss_store.add_vector(embedding, doc_id)
        except Exception:
            embedding_id = None

        # Write enrichment back to Firestore
        update_data: dict = {
            "extracted_questions": combined_flat,
            "questions": questions_nested,
            "stats": stats,
            "topics": list(all_topics),
            "summary": processed["summary"],
            "nlp_status": "done",
            "edit_history": firestore.ArrayUnion([{
                "timestamp": now,
                "field": "classification",
                "action": "ai_enrichment",
                "old_value": None,
                "new_value": (
                    f"{len(enriched_user)} user-provided question(s)"
                    + (f", {len(ai_questions)} AI-extracted" if ai_questions else "")
                    + (f", topics: {', '.join(all_topics)}" if all_topics else "")
                ),
            }]),
        }
        if embedding_id is not None:
            update_data["embedding_id"] = embedding_id

        # ANONYMITY INVARIANT: Background NLP must NEVER overwrite identity fields.
        _IDENTITY_FIELDS = {"is_anonymous", "author", "show_name", "contributor_name", "created_by"}
        assert not _IDENTITY_FIELDS.intersection(update_data), (
            f"BUG: NLP enrichment tried to write identity fields: "
            f"{_IDENTITY_FIELDS.intersection(update_data)}"
        )

        db.collection("interview_experiences").document(doc_id).update(update_data)

        # Refresh dashboard stats after enrichment
        update_dashboard_stats_async()

    except Exception as exc:
        # Mark as failed so the UI can show status
        try:
            db.collection("interview_experiences").document(doc_id).update({
                "nlp_status": "failed",
            })
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# GET my contributions
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/mine")
def get_my_contributions(user: dict = Depends(get_current_user)) -> dict:
    """Return all contributions belonging to the current user (active + hidden)."""
    try:
        # Preferred: uses composite index (created_by ASC, created_at DESC)
        snapshots = list(
            db.collection("interview_experiences")
            .where("created_by", "==", user["uid"])
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .stream()
        )
    except Exception:
        # Fallback: if composite index doesn't exist yet, query without
        # server-side ordering and sort in Python instead.
        snapshots = list(
            db.collection("interview_experiences")
            .where("created_by", "==", user["uid"])
            .stream()
        )
        snapshots.sort(
            key=lambda s: (s.to_dict() or {}).get("created_at", ""),
            reverse=True,
        )
    results = [serialize_doc(s) for s in snapshots]
    return {"results": results, "total": len(results)}


# ─────────────────────────────────────────────────────────────────────────────
# CREATE — fast ingestion, background NLP
# ─────────────────────────────────────────────────────────────────────────────

@router.post("")
def create_experience(payload: ExperienceCreate, user: dict = Depends(get_current_user)) -> dict:
    """Create a new experience contribution.

    PHASE 1 — Ingestion (blocking, fast, <200ms target):
      • Save experience metadata + user-provided questions verbatim
      • Return success immediately

    PHASE 2 — NLP enrichment (async, background thread):
      • Extract AI questions from raw text
      • Classify topics on all questions
      • Generate summary & FAISS embedding
      • Write results back to Firestore

    User questions are AUTHORITATIVE — never filtered, merged, or dropped.
    """
    # Auto-upgrade viewer → contributor on first submission
    if user.get("role") != "contributor":
        db.collection("users").document(user["uid"]).set(
            {"role": "contributor"}, merge=True
        )
        user["role"] = "contributor"

    now = datetime.now(timezone.utc).isoformat()
    doc_ref = db.collection("interview_experiences").document()

    # Build user question objects (verbatim, no NLP yet)
    user_question_objects = _build_user_question_objects(payload.user_questions, now)

    # Initial stats based on what we know right now
    initial_stats = {
        "user_question_count": len(user_question_objects),
        "extracted_question_count": 0,  # AI hasn't run yet
        "total_question_count": len(user_question_objects),
    }

    # Initial questions structure
    initial_questions = {
        "user_provided": user_question_objects,
        "ai_extracted": [],  # Populated by background NLP
    }

    contributor_name = user.get("name", "")

    # Fetch current display_name from the user document (source of truth)
    user_doc = db.collection("users").document(user["uid"]).get()
    user_data = user_doc.to_dict() or {} if user_doc.exists else {}
    display_name = user_data.get("display_name", "")
    if not display_name and contributor_name:
        # Derive on the fly if not yet stored
        parts = contributor_name.strip().split()
        if len(parts) >= 2:
            display_name = f"{parts[0]} {parts[-1][0].upper()}."
        elif parts:
            display_name = parts[0]

    # Build author identity model — IMMUTABLE SNAPSHOT at time of submission
    if payload.show_name and display_name:
        author = {
            "uid": user["uid"],
            "visibility": "public",
            "public_label": display_name,
        }
    else:
        # ANONYMOUS: no uid reference, no identity data — by design
        author = {
            "visibility": "anonymous",
        }

    doc_data = {
        "company": payload.company,
        "role": payload.role,
        "year": payload.year,
        "round": payload.round,
        "difficulty": payload.difficulty,
        "raw_text": payload.raw_text,
        "extracted_questions": user_question_objects,  # Legacy flat list — user questions only initially
        "questions": initial_questions,
        "stats": initial_stats,
        "topics": [],  # Populated by background NLP
        "summary": "",  # Populated by background NLP
        "embedding_id": None,
        "created_by": user["uid"],
        "contributor_name": contributor_name,
        "author": author,
        "show_name": payload.show_name,
        "created_at": firestore.SERVER_TIMESTAMP,
        "is_anonymous": payload.is_anonymous,
        "is_active": True,
        "nlp_status": "pending",
        # Contact details — only stored when non-anonymous and explicitly opted in
        "allow_contact": False if payload.is_anonymous else payload.allow_contact,
        "contact_linkedin": None if payload.is_anonymous else (payload.contact_linkedin or None),
        "contact_email": None if payload.is_anonymous else (payload.contact_email or None),
        "edit_history": [{
            "timestamp": now,
            "field": "creation",
            "action": "extracted",
            "old_value": None,
            "new_value": f"Experience submitted with {len(user_question_objects)} user-provided question(s)",
        }],
    }

    doc_ref.set(doc_data)

    # PHASE 2: Background NLP enrichment — does NOT block the response
    thread = threading.Thread(
        target=_run_background_nlp,
        args=(doc_ref.id, payload.raw_text, user_question_objects),
        daemon=True,
    )
    thread.start()

    result = serialize_doc(doc_ref.get())

    # Mask the creator ID if anonymous (for public display)
    if payload.is_anonymous:
        result["created_by"] = ANONYMOUS_DISPLAY_ID

    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET single experience
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{experience_id}")
def get_experience(experience_id: str) -> dict:
    snapshot = db.collection("interview_experiences").document(experience_id).get()
    if not snapshot.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experience not found.")
    return serialize_doc(snapshot)


# ─────────────────────────────────────────────────────────────────────────────
# SOFT DELETE / RESTORE
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{experience_id}")
def soft_delete_experience(experience_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Soft-delete: hides from search & analytics but preserves data."""
    _require_ownership(experience_id, user["uid"])
    now = datetime.now(timezone.utc).isoformat()
    db.collection("interview_experiences").document(experience_id).update({
        "is_active": False,
        "edit_history": firestore.ArrayUnion([{
            "timestamp": now,
            "field": "is_active",
            "action": "visibility_change",
            "old_value": "active",
            "new_value": "hidden",
        }]),
    })
    threading.Thread(target=update_dashboard_stats_async, daemon=True).start()
    return {"status": "hidden", "experience_id": experience_id}


@router.post("/{experience_id}/restore")
def restore_experience(experience_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Restore a soft-deleted contribution back to active."""
    _require_ownership(experience_id, user["uid"])
    now = datetime.now(timezone.utc).isoformat()
    db.collection("interview_experiences").document(experience_id).update({
        "is_active": True,
        "edit_history": firestore.ArrayUnion([{
            "timestamp": now,
            "field": "is_active",
            "action": "visibility_change",
            "old_value": "hidden",
            "new_value": "active",
        }]),
    })
    threading.Thread(target=update_dashboard_stats_async, daemon=True).start()
    return {"status": "active", "experience_id": experience_id}


# ─────────────────────────────────────────────────────────────────────────────
# EDIT METADATA (role, year, round, difficulty)
# ─────────────────────────────────────────────────────────────────────────────

# Immutable fields — these must NEVER be accepted in a metadata update.
_IMMUTABLE_FIELDS = frozenset({
    "raw_text", "summary", "extracted_questions", "questions", "topics",
    "created_by", "created_at", "embedding_id", "edit_history", "stats",
})


@router.patch("/{experience_id}")
def update_experience_metadata(
    experience_id: str,
    payload: ExperienceMetadataUpdate,
    user: dict = Depends(get_current_user),
) -> dict:
    """Update allowed metadata fields with edit history tracking.

    Only role, year, round, and difficulty may be changed.
    The original AI-extracted narrative, questions, summary, and topics
    are immutable institutional records and cannot be modified.
    """
    existing = _require_ownership(experience_id, user["uid"])
    now = datetime.now(timezone.utc).isoformat()

    updates: dict = {}
    history_entries: list[dict] = []

    for field_name in ("role", "year", "round", "difficulty"):
        new_val = getattr(payload, field_name, None)
        if new_val is None:
            continue
        old_val = existing.get(field_name)
        if str(new_val) == str(old_val):
            continue
        updates[field_name] = new_val
        history_entries.append({
            "timestamp": now,
            "field": field_name,
            "action": "metadata_change",
            "old_value": str(old_val) if old_val is not None else None,
            "new_value": str(new_val),
        })

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update.",
        )

    updates["edit_history"] = firestore.ArrayUnion(history_entries)
    db.collection("interview_experiences").document(experience_id).update(updates)
    threading.Thread(target=update_dashboard_stats_async, daemon=True).start()

    result = serialize_doc(
        db.collection("interview_experiences").document(experience_id).get()
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ADD QUESTIONS (memory-aware — questions remembered after submission)
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/{experience_id}/questions")
def add_questions(
    experience_id: str,
    payload: AddQuestionsRequest,
    user: dict = Depends(get_current_user),
) -> dict:
    """Add questions remembered after initial submission.

    TIER 1 (blocking, fast, <200ms):
      • Save raw user questions verbatim to Firestore
      • Update stats with new counts
      • Return immediately

    TIER 2 (async, background thread):
      • NLP topic classification on new questions
      • Re-derive topics list
      • Regenerate FAISS embedding
      • Refresh dashboard analytics

    User-provided questions are NEVER modified, filtered, or removed.
    """
    existing = _require_ownership(experience_id, user["uid"])
    now = datetime.now(timezone.utc).isoformat()

    # ── TIER 1: Instant save ─────────────────────────────────────────────────

    # Build raw question objects (no NLP yet — just verbatim save)
    new_questions: list[dict] = []
    for q_text in payload.questions:
        q_text = q_text.strip()
        if not q_text or len(q_text) < 5:
            continue
        new_questions.append({
            "question_text": q_text,
            "question": q_text,  # Legacy field
            "topic": "General",  # Classified in background
            "category": "theory",
            "confidence": 1.0,
            "question_type": "extracted",
            "source": "user",
            "added_later": True,
            "added_at": now,
            "created_at": now,
            "updated_at": now,
        })

    if not new_questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid questions provided.",
        )

    # Append to existing flat list
    existing_flat = existing.get("extracted_questions") or []
    combined_flat = existing_flat + new_questions

    # Update nested questions structure
    existing_nested = existing.get("questions") or {}
    user_provided = (existing_nested.get("user_provided") or []) + new_questions
    ai_extracted = existing_nested.get("ai_extracted") or []
    updated_nested = {
        "user_provided": user_provided,
        "ai_extracted": ai_extracted,
    }

    # Update explicit stats
    updated_stats = {
        "user_question_count": len(user_provided),
        "extracted_question_count": len(ai_extracted),
        "total_question_count": len(user_provided) + len(ai_extracted),
    }

    history_entry = {
        "timestamp": now,
        "field": "questions",
        "action": "added_later",
        "old_value": f"{len(existing_flat)} questions",
        "new_value": f"{len(combined_flat)} questions (+{len(new_questions)} added by user)",
    }

    # Fast write — no NLP, no FAISS, no analytics
    db.collection("interview_experiences").document(experience_id).update({
        "extracted_questions": combined_flat,
        "questions": updated_nested,
        "stats": updated_stats,
        "edit_history": firestore.ArrayUnion([history_entry]),
    })

    # Read back the saved doc for immediate response
    result = serialize_doc(
        db.collection("interview_experiences").document(experience_id).get()
    )

    # ── TIER 2: Background enrichment ────────────────────────────────────────

    thread = threading.Thread(
        target=_run_background_question_enrichment,
        args=(experience_id, existing),
        daemon=True,
    )
    thread.start()

    return result


def _run_background_question_enrichment(doc_id: str, existing_data: dict) -> None:
    """Background enrichment after adding questions.

    Phases:
      1. Re-read the document (to get the most current state)
      2. Classify all unclassified user questions through NLP
      3. Re-derive topics list
      4. Regenerate FAISS embedding from full context
      5. Refresh dashboard analytics
    """
    try:
        # Re-read current state
        snapshot = db.collection("interview_experiences").document(doc_id).get()
        if not snapshot.exists:
            return
        data = snapshot.to_dict() or {}

        now = datetime.now(timezone.utc).isoformat()
        combined_flat = data.get("extracted_questions") or []
        raw_text = data.get("raw_text", "")

        # Classify any unclassified questions (topic == "General" and source == "user")
        enriched_flat: list[dict] = []
        for q in combined_flat:
            if isinstance(q, dict) and q.get("source") == "user" and q.get("topic") == "General":
                classified = pipeline.classify_single_question(q.get("question_text", ""))
                enriched = {**q}
                enriched["topic"] = classified.get("topic", "General")
                enriched["category"] = classified.get("category", "theory")
                enriched["updated_at"] = now
                enriched_flat.append(enriched)
            else:
                enriched_flat.append(q)

        # Re-derive nested structure
        user_provided = [q for q in enriched_flat if isinstance(q, dict) and q.get("source") == "user"]
        ai_extracted = [q for q in enriched_flat if isinstance(q, dict) and q.get("source") != "user"]
        enriched_nested = {
            "user_provided": user_provided,
            "ai_extracted": ai_extracted,
        }
        enriched_stats = {
            "user_question_count": len(user_provided),
            "extracted_question_count": len(ai_extracted),
            "total_question_count": len(user_provided) + len(ai_extracted),
        }

        # Re-derive topics from all questions
        all_topics: set[str] = set(data.get("topics") or [])
        for q in enriched_flat:
            topic = q.get("topic", "General") if isinstance(q, dict) else "General"
            if topic and topic != "General":
                all_topics.add(topic)

        # Regenerate FAISS embedding from full document context
        all_question_texts = " ".join(
            q.get("question_text", "") if isinstance(q, dict) else str(q)
            for q in enriched_flat
        )
        full_text = f"{raw_text} {all_question_texts}".strip()
        try:
            new_embedding = pipeline.embed(full_text)
            faiss_store.add_vector(new_embedding, doc_id)
        except Exception:
            pass  # Non-critical: existing embedding still serves

        # Write enrichment back
        _enrichment_update = {
            "extracted_questions": enriched_flat,
            "questions": enriched_nested,
            "stats": enriched_stats,
            "topics": list(all_topics),
        }

        # ANONYMITY INVARIANT: Background enrichment must NEVER overwrite identity fields.
        _IDENTITY_FIELDS = {"is_anonymous", "author", "show_name", "contributor_name", "created_by"}
        assert not _IDENTITY_FIELDS.intersection(_enrichment_update), (
            f"BUG: Question enrichment tried to write identity fields: "
            f"{_IDENTITY_FIELDS.intersection(_enrichment_update)}"
        )

        db.collection("interview_experiences").document(doc_id).update(_enrichment_update)

        update_dashboard_stats_async()

    except Exception:
        pass  # Background — never crash the main thread
