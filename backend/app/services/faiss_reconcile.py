from __future__ import annotations

import logging

from firebase_admin import firestore

from app.core.firebase import db
from app.services.faiss_store import faiss_store
from app.services.nlp import pipeline

logger = logging.getLogger(__name__)


def _embedding_source(data: dict) -> str:
    """Reconstruct the text an experience was embedded from.

    Mirrors the embedding input built at submission / index-upsert time so a
    rebuilt vector matches what live search produces.
    """
    questions = data.get("extracted_questions") or []
    question_bits: list[str] = []
    for question in questions[:16]:
        if isinstance(question, dict):
            text = str(question.get("question_text") or question.get("question") or "").strip()
        else:
            text = str(question).strip()
        if text:
            question_bits.append(text)

    return " ".join(
        value
        for value in [
            str(data.get("raw_text") or ""),
            str(data.get("summary") or ""),
            " ".join(data.get("topics") or []),
            " ".join(question_bits),
        ]
        if value
    )[:5000]


def rebuild_faiss_from_firestore(*, force: bool = False, min_docs: int = 1) -> int:
    """Rebuild the FAISS index from Firestore when it is empty or lost.

    FAISS is persisted to local disk, which is ephemeral on several hosts
    (e.g. Hugging Face Spaces without a mounted volume). On a wiped/fresh disk
    the index loads empty while Firestore still holds every experience, which
    silently breaks semantic search with no recovery path. This re-embeds
    active experiences and rebuilds the index so semantic search self-heals on
    boot.

    Skips work when the index already holds vectors (a normal restart with a
    persisted index) unless ``force=True``. Returns the number of vectors
    rebuilt (0 if skipped).
    """
    try:
        current = faiss_store.index.ntotal
    except Exception:
        logger.exception("FAISS reconcile: could not read current index size")
        return 0

    if current > 0 and not force:
        logger.info(
            "FAISS reconcile: index already holds %d vector(s); skipping rebuild", current
        )
        return 0

    try:
        snapshots = (
            db.collection("interview_experiences")
            .where(filter=firestore.FieldFilter("is_active", "==", True))
            .stream()
        )
    except Exception:
        logger.exception("FAISS reconcile: active-only query failed; scanning all documents")
        snapshots = db.collection("interview_experiences").stream()

    vectors = []
    doc_ids: list[str] = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        if not data.get("is_active", True):
            continue
        source = _embedding_source(data)
        if not source:
            continue
        try:
            vectors.append(pipeline.embed(source))
            doc_ids.append(snapshot.id)
        except Exception:
            logger.exception("FAISS reconcile: embedding failed for doc_id=%s", snapshot.id)

    if len(doc_ids) < min_docs:
        logger.info(
            "FAISS reconcile: found %d embeddable document(s) (min %d); not rebuilding",
            len(doc_ids),
            min_docs,
        )
        return 0

    faiss_store.rebuild(vectors, doc_ids)
    logger.info("FAISS reconcile: rebuilt index with %d vector(s) from Firestore", len(doc_ids))
    return len(doc_ids)
