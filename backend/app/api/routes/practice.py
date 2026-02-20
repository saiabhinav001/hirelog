"""Practice Lists API routes."""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore

from app.api.dependencies import get_current_user
from app.core.firebase import db
from app.models.schemas import (
    PracticeListCreate,
    PracticeListResponse,
    PracticeQuestionCreate,
    PracticeQuestionResponse,
    PracticeQuestionUpdate,
)

router = APIRouter(prefix="/api/practice-lists", tags=["practice"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """Reconciliation: scan ALL practice_lists and recompute counters.

    Called on backend startup to fix any stale data left by older code paths
    or interrupted writes.  Returns the number of lists repaired.
    """
    repaired = 0
    for list_doc in db.collection("practice_lists").stream():
        _recompute_and_store_list_stats(list_doc.id)
        repaired += 1
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
    lists_ref = db.collection("practice_lists").where("user_id", "==", user_id)

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
        "name": payload.name,
        "user_id": user_id,
        "created_at": now,
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
    
    doc_ref.update({"name": payload.name})
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
    
    # Delete all questions in the list
    questions_ref = doc_ref.collection("questions")
    for q in questions_ref.stream():
        q.reference.delete()
    
    # Delete the list
    doc_ref.delete()
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
        f"topic_distribution.{topic}": firestore.Increment(1),
    })
    batch.commit()

    # Lightweight: 1 parent-doc read to recompute revised_percent
    updated_data = list_ref.get().to_dict()
    total = updated_data.get("question_count", 0)
    revised = updated_data.get("revised_count", 0)
    list_ref.update({"revised_percent": _compute_revised_percent(revised, total)})

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
                counter_updates[f"topic_distribution.{old_topic}"] = firestore.Increment(-1)
                counter_updates[f"topic_distribution.{new_topic}"] = firestore.Increment(1)
            batch.update(list_ref, counter_updates)
            batch.commit()

            # Lightweight: 1 read to recompute revised_percent
            updated_data = list_ref.get().to_dict()
            total = updated_data.get("question_count", 0)
            revised = updated_data.get("revised_count", 0)
            list_ref.update({"revised_percent": _compute_revised_percent(revised, total)})
        else:
            question_ref.update(updates)
            # If topic changed, update topic_distribution incrementally
            if "topic" in updates:
                old_topic = old_data.get("topic", "General")
                new_topic = updates["topic"]
                if new_topic != old_topic:
                    list_ref.update({
                        f"topic_distribution.{old_topic}": firestore.Increment(-1),
                        f"topic_distribution.{new_topic}": firestore.Increment(1),
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
        f"topic_distribution.{old_topic}": firestore.Increment(-1),
    })
    batch.commit()

    # Lightweight: 1 parent-doc read to recompute revised_percent + clean up zero-count topics
    updated_data = list_ref.get().to_dict()
    total = updated_data.get("question_count", 0)
    revised = updated_data.get("revised_count", 0)
    fixups: dict = {"revised_percent": _compute_revised_percent(revised, total)}
    for t, c in (updated_data.get("topic_distribution") or {}).items():
        if isinstance(c, (int, float)) and c <= 0:
            fixups[f"topic_distribution.{t}"] = firestore.DELETE_FIELD
    list_ref.update(fixups)

    return {"status": "deleted"}
