"""Practice Lists API routes."""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore
from google.cloud.firestore_v1.field_path import FieldPath

from app.api.dependencies import get_current_user
from app.core.config import PRACTICE_LIST_SCHEMA_VERSION
from app.core.firebase import db
from app.models.schemas import (
    PracticeListCreate,
    PracticeListResponse,
    PracticeQuestionCreate,
    PracticeQuestionResponse,
    PracticeQuestionUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/practice-lists", tags=["practice"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic_distribution_key(topic: str) -> str:
    """Build a Firestore field-path key for a topic's distribution counter.

    User-supplied `topic` must never be interpolated directly into a dotted
    field path (`f"topic_distribution.{topic}"`) — a value like `a.b` or
    `stats.total` would be parsed as nested path segments, letting a caller
    write arbitrary nested fields under `topic_distribution`. `FieldPath`
    escapes the segment (back-tick quoting dots/special chars) so the topic is
    always treated as a single literal key.
    """
    return FieldPath("topic_distribution", str(topic)).to_api_repr()


def _recompute_and_store_list_stats(list_id: str) -> dict:
    """Full recompute from subcollection — used ONLY for reconciliation/repair.

    Normal mutations use batched writes with atomic counter increments instead.
    """
    list_ref = db.collection("practice_lists").document(list_id)
    questions_ref = list_ref.collection("questions")
    questions = list(questions_ref.stream())

    total = len(questions)
    if total == 0:
        stats = {
            "question_count": 0,
            "revised_count": 0,
            "practicing_count": 0,
            "unvisited_count": 0,
            "topic_distribution": {},
            "revised_percent": 0.0,
        }
    else:
        topic_counts: dict[str, int] = {}
        revised = 0
        practicing = 0
        unvisited = 0
        for q in questions:
            data = q.to_dict()
            topic = data.get("topic", "General")
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            status = data.get("status", "unvisited")
            if status == "revised":
                revised += 1
            elif status == "practicing":
                practicing += 1
            else:
                unvisited += 1
        stats = {
            "question_count": total,
            "revised_count": revised,
            "practicing_count": practicing,
            "unvisited_count": unvisited,
            "topic_distribution": topic_counts,
            "revised_percent": round((revised / total) * 100, 1),
        }

    list_ref.set(stats, merge=True)
    return stats


def repair_all_practice_list_stats() -> int:
    """Reconciliation: recompute practice-list counters that may be stale.

    Called on backend startup to fix data left by older code paths or
    interrupted writes. A naive full-collection scan costs O(lists × questions)
    Firestore reads on *every* deployment, which is prohibitive at scale. So we
    only recompute lists mutated since the previous successful repair, tracked
    via ``metadata/practice_repair.last_repaired_at`` (compared against each
    list's ``updated_at`` stamp). The first run (no marker) — and any run whose
    incremental query fails — falls back to a full scan so nothing is missed.

    Returns the number of lists repaired.
    """
    marker_ref = db.collection("metadata").document("practice_repair")
    last_repaired_at = None
    try:
        marker = marker_ref.get()
        if marker.exists:
            last_repaired_at = (marker.to_dict() or {}).get("last_repaired_at")
    except Exception:
        logger.exception("Practice repair: could not read repair marker; full scan")

    # Capture a server-side "start" timestamp BEFORE scanning and promote it to
    # last_repaired_at only after a successful pass. Marking with the *start*
    # time (not the end time) means any list mutated *during* the scan still has
    # updated_at >= the new marker and is caught next run — closing the
    # lost-update window.
    started_at = None
    try:
        marker_ref.set({"repair_started_at": firestore.SERVER_TIMESTAMP}, merge=True)
        started_at = (marker_ref.get().to_dict() or {}).get("repair_started_at")
    except Exception:
        logger.exception("Practice repair: could not stamp repair start time")

    list_docs = None
    if last_repaired_at is not None:
        try:
            list_docs = list(
                db.collection("practice_lists")
                .where(filter=firestore.FieldFilter("updated_at", ">=", last_repaired_at))
                .stream()
            )
        except Exception:
            logger.exception("Practice repair: incremental query failed; full scan")
            list_docs = None

    if list_docs is None:
        list_docs = list(db.collection("practice_lists").stream())

    repaired = 0
    for list_doc in list_docs:
        _recompute_and_store_list_stats(list_doc.id)
        repaired += 1

    try:
        marker_ref.set(
            {"last_repaired_at": started_at or firestore.SERVER_TIMESTAMP}, merge=True
        )
    except Exception:
        logger.exception("Practice repair: could not update repair marker")

    return repaired


def _read_list_response(doc_id: str, data: dict) -> PracticeListResponse:
    """Build PracticeListResponse from a Firestore document dict."""
    return PracticeListResponse(
        id=doc_id,
        name=data.get("name", ""),
        user_id=data.get("user_id", ""),
        created_at=data.get("created_at", ""),
        question_count=data.get("question_count", 0),
        revised_count=data.get("revised_count", 0),
        practicing_count=data.get("practicing_count", 0),
        unvisited_count=data.get("unvisited_count", 0),
        topic_distribution=data.get("topic_distribution", {}),
        revised_percent=data.get("revised_percent", 0.0),
    )


def _compute_revised_percent(revised: int, total: int) -> float:
    """Safely compute revised percentage."""
    if total <= 0:
        return 0.0
    return round((revised / total) * 100, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Practice Lists CRUD
# ─────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=List[PracticeListResponse])
async def get_practice_lists(user: dict = Depends(get_current_user)):
    """Get all practice lists for the current user.

    Returns metadata only (name, cached question_count, topic_distribution,
    revised_percent).  Does NOT iterate question sub-collections → instant.
    """
    user_id = user["uid"]
    lists_ref = db.collection("practice_lists").where(
        filter=firestore.FieldFilter("user_id", "==", user_id)
    )

    results = []
    for doc in lists_ref.stream():
        data = doc.to_dict()
        results.append(_read_list_response(doc.id, data))

    results.sort(key=lambda x: x.created_at, reverse=True)
    return results


@router.post("", response_model=PracticeListResponse)
async def create_practice_list(
    payload: PracticeListCreate,
    user: dict = Depends(get_current_user),
):
    """Create a new practice list."""
    user_id = user["uid"]
    now = _now_iso()
    
    doc_ref = db.collection("practice_lists").document()
    doc_ref.set({
        "schema_version": PRACTICE_LIST_SCHEMA_VERSION,
        "name": payload.name,
        "user_id": user_id,
        "created_at": now,
        # Server timestamp used by the incremental startup repair to find lists
        # mutated since the last reconciliation.
        "updated_at": firestore.SERVER_TIMESTAMP,
        "question_count": 0,
        "revised_count": 0,
        "practicing_count": 0,
        "unvisited_count": 0,
        "topic_distribution": {},
        "revised_percent": 0.0,
    })
    
    return PracticeListResponse(
        id=doc_ref.id,
        name=payload.name,
        user_id=user_id,
        created_at=now,
        question_count=0,
        revised_count=0,
        practicing_count=0,
        unvisited_count=0,
        topic_distribution={},
        revised_percent=0.0,
    )


@router.put("/{list_id}", response_model=PracticeListResponse)
async def update_practice_list(
    list_id: str,
    payload: PracticeListCreate,
    user: dict = Depends(get_current_user),
):
    """Rename a practice list."""
    user_id = user["uid"]
    doc_ref = db.collection("practice_lists").document(list_id)
    doc = doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="List not found")
    
    data = doc.to_dict()
    if data.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    doc_ref.update({"name": payload.name, "updated_at": firestore.SERVER_TIMESTAMP})
    data["name"] = payload.name

    return _read_list_response(list_id, data)


@router.delete("/{list_id}")
async def delete_practice_list(
    list_id: str,
    user: dict = Depends(get_current_user),
):
    """Delete a practice list and all its questions."""
    user_id = user["uid"]
    doc_ref = db.collection("practice_lists").document(list_id)
    doc = doc_ref.get()
    
    if not doc.exists:
        raise HTTPException(status_code=404, detail="List not found")
    
    data = doc.to_dict()
    if data.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Delete all questions in the list using batched writes. One-by-one deletes
    # are slow and, if the process dies mid-loop, leave orphaned questions with
    # no parent. Batches (max 500 ops) commit atomically; the parent list is
    # removed in the same final batch as the last chunk of questions.
    questions_ref = doc_ref.collection("questions")
    _BATCH_LIMIT = 450  # headroom under Firestore's 500-op limit for the parent
    batch = db.batch()
    pending = 0
    for q in questions_ref.stream():
        batch.delete(q.reference)
        pending += 1
        if pending >= _BATCH_LIMIT:
            batch.commit()
            batch = db.batch()
            pending = 0

    # Delete the list itself in the final batch.
    batch.delete(doc_ref)
    batch.commit()
    return {"status": "deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# Practice Questions CRUD
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/{list_id}/questions", response_model=List[PracticeQuestionResponse])
async def get_questions(
    list_id: str,
    user: dict = Depends(get_current_user),
):
    """Get all questions in a practice list."""
    user_id = user["uid"]
    
    # Verify list ownership
    list_ref = db.collection("practice_lists").document(list_id)
    list_doc = list_ref.get()
    
    if not list_doc.exists:
        raise HTTPException(status_code=404, detail="List not found")
    
    if list_doc.to_dict().get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    questions_ref = list_ref.collection("questions")
    results = []
    
    for doc in questions_ref.stream():
        data = doc.to_dict()
        results.append(
            PracticeQuestionResponse(
                id=doc.id,
                list_id=list_id,
                question_text=data.get("question_text", ""),
                topic=data.get("topic", "General"),
                difficulty=data.get("difficulty"),
                status=data.get("status", "unvisited"),
                source=data.get("source", "manual"),
                source_experience_id=data.get("source_experience_id"),
                source_company=data.get("source_company"),
                created_at=data.get("created_at", ""),
            )
        )
    
    # Sort by created_at descending
    results.sort(key=lambda x: x.created_at, reverse=True)
    return results


@router.post("/{list_id}/questions", response_model=PracticeQuestionResponse)
async def add_question(
    list_id: str,
    payload: PracticeQuestionCreate,
    user: dict = Depends(get_current_user),
):
    """Add a question to a practice list."""
    user_id = user["uid"]
    
    # Verify list ownership
    list_ref = db.collection("practice_lists").document(list_id)
    list_doc = list_ref.get()
    
    if not list_doc.exists:
        raise HTTPException(status_code=404, detail="List not found")
    
    if list_doc.to_dict().get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    now = _now_iso()
    question_data = {
        "question_text": payload.question_text,
        "topic": payload.topic,
        "difficulty": payload.difficulty,
        "status": "unvisited",
        "source": payload.source,
        "source_experience_id": payload.source_experience_id,
        "source_company": payload.source_company,
        "created_at": now,
    }
    
    # Batched write: create question + update parent counters atomically
    topic = payload.topic or "General"
    doc_ref = list_ref.collection("questions").document()
    batch = db.batch()
    batch.set(doc_ref, question_data)
    batch.update(list_ref, {
        "question_count": firestore.Increment(1),
        "unvisited_count": firestore.Increment(1),
        _topic_distribution_key(topic): firestore.Increment(1),
    })
    batch.commit()

    # Lightweight: 1 parent-doc read to recompute revised_percent
    updated_data = list_ref.get().to_dict()
    total = updated_data.get("question_count", 0)
    revised = updated_data.get("revised_count", 0)
    list_ref.update({
        "revised_percent": _compute_revised_percent(revised, total),
        "updated_at": firestore.SERVER_TIMESTAMP,
    })

    return PracticeQuestionResponse(
        id=doc_ref.id,
        list_id=list_id,
        **question_data,
    )


@router.put("/{list_id}/questions/{question_id}", response_model=PracticeQuestionResponse)
async def update_question(
    list_id: str,
    question_id: str,
    payload: PracticeQuestionUpdate,
    user: dict = Depends(get_current_user),
):
    """Update a question in a practice list."""
    user_id = user["uid"]
    
    # Verify list ownership
    list_ref = db.collection("practice_lists").document(list_id)
    list_doc = list_ref.get()
    
    if not list_doc.exists:
        raise HTTPException(status_code=404, detail="List not found")
    
    if list_doc.to_dict().get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    question_ref = list_ref.collection("questions").document(question_id)
    question_doc = question_ref.get()
    
    if not question_doc.exists:
        raise HTTPException(status_code=404, detail="Question not found")
    
    # Build update dict
    updates = {}
    if payload.question_text is not None:
        updates["question_text"] = payload.question_text
    if payload.topic is not None:
        updates["topic"] = payload.topic
    if payload.difficulty is not None:
        updates["difficulty"] = payload.difficulty
    if payload.status is not None:
        updates["status"] = payload.status
    
    if updates:
        old_data = question_doc.to_dict()
        old_status = old_data.get("status", "unvisited")
        new_status = updates.get("status")

        # If status changed, batch the question update + counter adjustments
        if new_status and new_status != old_status:
            # Map status to counter field
            status_field = {
                "unvisited": "unvisited_count",
                "practicing": "practicing_count",
                "revised": "revised_count",
            }
            batch = db.batch()
            batch.update(question_ref, updates)
            counter_updates: dict = {
                status_field[old_status]: firestore.Increment(-1),
                status_field[new_status]: firestore.Increment(1),
            }
            # Handle topic change in the same batch
            old_topic = old_data.get("topic", "General")
            new_topic = updates.get("topic")
            if new_topic and new_topic != old_topic:
                counter_updates[_topic_distribution_key(old_topic)] = firestore.Increment(-1)
                counter_updates[_topic_distribution_key(new_topic)] = firestore.Increment(1)
            batch.update(list_ref, counter_updates)
            batch.commit()

            # Lightweight: 1 read to recompute revised_percent
            updated_data = list_ref.get().to_dict()
            total = updated_data.get("question_count", 0)
            revised = updated_data.get("revised_count", 0)
            list_ref.update({
        "revised_percent": _compute_revised_percent(revised, total),
        "updated_at": firestore.SERVER_TIMESTAMP,
    })
        else:
            question_ref.update(updates)
            # If topic changed, update topic_distribution incrementally
            if "topic" in updates:
                old_topic = old_data.get("topic", "General")
                new_topic = updates["topic"]
                if new_topic != old_topic:
                    list_ref.update({
                        _topic_distribution_key(old_topic): firestore.Increment(-1),
                        _topic_distribution_key(new_topic): firestore.Increment(1),
                        "updated_at": firestore.SERVER_TIMESTAMP,
                    })

    # Return updated question
    data = question_doc.to_dict()
    data.update(updates)
    
    return PracticeQuestionResponse(
        id=question_id,
        list_id=list_id,
        question_text=data.get("question_text", ""),
        topic=data.get("topic", "General"),
        difficulty=data.get("difficulty"),
        status=data.get("status", "unvisited"),
        source=data.get("source", "manual"),
        source_experience_id=data.get("source_experience_id"),
        source_company=data.get("source_company"),
        created_at=data.get("created_at", ""),
    )


@router.delete("/{list_id}/questions/{question_id}")
async def delete_question(
    list_id: str,
    question_id: str,
    user: dict = Depends(get_current_user),
):
    """Delete a question from a practice list."""
    user_id = user["uid"]
    
    # Verify list ownership
    list_ref = db.collection("practice_lists").document(list_id)
    list_doc = list_ref.get()
    
    if not list_doc.exists:
        raise HTTPException(status_code=404, detail="List not found")
    
    if list_doc.to_dict().get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    question_ref = list_ref.collection("questions").document(question_id)
    question_doc = question_ref.get()
    
    if not question_doc.exists:
        raise HTTPException(status_code=404, detail="Question not found")
    
    # Determine counter to decrement based on current status
    q_data = question_doc.to_dict()
    old_status = q_data.get("status", "unvisited")
    old_topic = q_data.get("topic", "General")
    status_field = {
        "unvisited": "unvisited_count",
        "practicing": "practicing_count",
        "revised": "revised_count",
    }

    # Batched write: delete question + decrement parent counters atomically
    batch = db.batch()
    batch.delete(question_ref)
    batch.update(list_ref, {
        "question_count": firestore.Increment(-1),
        status_field[old_status]: firestore.Increment(-1),
        _topic_distribution_key(old_topic): firestore.Increment(-1),
    })
    batch.commit()

    # Lightweight: 1 parent-doc read to recompute revised_percent + clean up zero-count topics
    updated_data = list_ref.get().to_dict()
    total = updated_data.get("question_count", 0)
    revised = updated_data.get("revised_count", 0)
    fixups: dict = {
        "revised_percent": _compute_revised_percent(revised, total),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    for t, c in (updated_data.get("topic_distribution") or {}).items():
        if isinstance(c, (int, float)) and c <= 0:
            fixups[_topic_distribution_key(t)] = firestore.DELETE_FIELD
    list_ref.update(fixups)

    return {"status": "deleted"}
