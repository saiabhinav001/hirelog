from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass

from firebase_admin import firestore as firestore_admin

from app.core.config import settings
from app.core.firebase import db
from app.services.indexed_document import build_index_document
from app.services.nlp import pipeline
from app.services.typesense_store import typesense_store

logger = logging.getLogger(__name__)


@dataclass
class IndexTask:
    action: str
    doc_id: str
    attempts: int = 0
    source: str = "memory"
    task_ref: object | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


class SearchIndexQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue[IndexTask] = queue.Queue(
            maxsize=max(100, int(settings.SEARCH_INDEX_QUEUE_MAX))
        )
        self._started = False
        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []
        self._worker_id = f"idx-{uuid.uuid4().hex[:12]}"
        self._stats_lock = threading.Lock()
        self._processed_total = 0
        self._failed_total = 0
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def backend(self) -> str:
        candidate = str(settings.SEARCH_INDEX_QUEUE_BACKEND or "memory").strip().lower()
        if candidate in {"memory", "firestore"}:
            return candidate
        return "memory"

    @property
    def worker_mode(self) -> str:
        candidate = str(settings.SEARCH_INDEX_WORKER_MODE or "embedded").strip().lower()
        if candidate in {"embedded", "external"}:
            return candidate
        return "embedded"

    @property
    def distributed(self) -> bool:
        return self.backend == "firestore"

    @property
    def enabled(self) -> bool:
        return typesense_store.enabled

    def _task_collection(self):
        return db.collection(settings.SEARCH_INDEX_FIRESTORE_COLLECTION)

    def _runtime_doc(self):
        return db.collection("metadata").document("search_index_runtime")

    def _record_success(self, *, doc_id: str, action: str, now: datetime) -> None:
        with self._stats_lock:
            self._processed_total += 1
            self._last_success_at = now
            self._last_error = None

        if self.distributed:
            try:
                self._runtime_doc().set(
                    {
                        "last_success_at": now,
                        "last_task_doc_id": doc_id,
                        "last_task_action": action,
                        "processed_total": self._processed_total,
                        "failed_total": self._failed_total,
                        "updated_at": now,
                    },
                    merge=True,
                )
            except Exception:
                logger.exception("Failed to update distributed index runtime metadata")

    def _record_failure(self, *, error: str, now: datetime) -> None:
        trimmed = str(error).strip()[:500]
        with self._stats_lock:
            self._failed_total += 1
            self._last_failure_at = now
            self._last_error = trimmed

        if self.distributed:
            try:
                self._runtime_doc().set(
                    {
                        "last_failure_at": now,
                        "last_error": trimmed,
                        "processed_total": self._processed_total,
                        "failed_total": self._failed_total,
                        "updated_at": now,
                    },
                    merge=True,
                )
            except Exception:
                logger.exception("Failed to update distributed index runtime metadata")

    def _sync_runtime_stats_from_firestore(self) -> None:
        if not self.distributed:
            return

        try:
            snapshot = self._runtime_doc().get()
            if not snapshot.exists:
                return
            data = snapshot.to_dict() or {}
        except Exception:
            logger.exception("Failed to read distributed index runtime metadata")
            return

        remote_success = _coerce_datetime(data.get("last_success_at"))
        remote_failure = _coerce_datetime(data.get("last_failure_at"))
        remote_processed = int(data.get("processed_total") or 0)
        remote_failed = int(data.get("failed_total") or 0)
        remote_error = str(data.get("last_error") or "").strip() or None

        with self._stats_lock:
            if remote_success and (
                self._last_success_at is None or remote_success > self._last_success_at
            ):
                self._last_success_at = remote_success
            if remote_failure and (
                self._last_failure_at is None or remote_failure > self._last_failure_at
            ):
                self._last_failure_at = remote_failure
            self._processed_total = max(self._processed_total, remote_processed)
            self._failed_total = max(self._failed_total, remote_failed)
            if remote_error:
                self._last_error = remote_error

    def _firestore_task_counts(self, *, sample_limit: int = 600) -> tuple[dict[str, int], bool]:
        counts = {
            "queued": 0,
            "retry": 0,
            "processing": 0,
            "failed": 0,
            "done": 0,
        }
        sampled = False

        try:
            index = 0
            for index, snapshot in enumerate(self._task_collection().limit(sample_limit).stream(), start=1):
                data = snapshot.to_dict() or {}
                status = str(data.get("status") or "queued").strip().lower()
                if status in counts:
                    counts[status] += 1
            sampled = index >= sample_limit
        except Exception:
            logger.exception("Failed to read distributed queue sample counts")

        return counts, sampled

    def start(self) -> None:
        if not self.enabled:
            logger.info("Search index queue disabled (Typesense not configured)")
            return

        with self._start_lock:
            if self._started:
                return

            typesense_store.ensure_collection()
            worker_count = max(1, int(settings.SEARCH_INDEX_WORKERS))
            for idx in range(worker_count):
                worker = threading.Thread(
                    target=self._run_worker,
                    name=f"search-index-worker-{idx}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)

            self._started = True
            logger.info(
                "Search index queue started with %d worker(s) backend=%s mode=%s",
                worker_count,
                self.backend,
                self.worker_mode,
            )

    def status(self, *, include_stats: bool = False) -> dict:
        if include_stats and self.distributed:
            self._sync_runtime_stats_from_firestore()

        with self._stats_lock:
            processed_total = int(self._processed_total)
            failed_total = int(self._failed_total)
            last_success_at = self._last_success_at
            last_failure_at = self._last_failure_at
            last_error = self._last_error

        queued = self._queue.qsize() if not self.distributed else 0
        processing = 0
        sample_counts: dict[str, int] | None = None
        sampled = False

        if include_stats and self.distributed and self.enabled:
            sample_counts, sampled = self._firestore_task_counts()
            queued = int(sample_counts.get("queued", 0) + sample_counts.get("retry", 0))
            processing = int(sample_counts.get("processing", 0))
            failed_total = max(failed_total, int(sample_counts.get("failed", 0)))

        now = _utc_now()
        freshness_seconds = None
        if last_success_at is not None:
            freshness_seconds = max(0, int((now - last_success_at).total_seconds()))

        warn_after = max(60, int(settings.SEARCH_INDEX_FRESHNESS_WARN_SECONDS))

        payload = {
            "enabled": self.enabled,
            "started": self._started,
            "workers": len(self._workers),
            "backend": self.backend,
            "worker_mode": self.worker_mode,
            "distributed": self.distributed,
            "queued": queued,
            "processing": processing,
            "failed": failed_total,
            "processed_total": processed_total,
            "last_success_at": _iso(last_success_at),
            "last_failure_at": _iso(last_failure_at),
            "last_error": last_error,
            "freshness_seconds": freshness_seconds,
            "freshness_warn_after_seconds": warn_after,
            "stale": bool(
                freshness_seconds is not None
                and freshness_seconds > warn_after
            ),
        }

        if include_stats and sample_counts is not None:
            payload["sampled"] = sampled
            payload["sample_counts"] = sample_counts

        return payload

    def enqueue_upsert(self, doc_id: str) -> None:
        self._enqueue(IndexTask(action="upsert", doc_id=doc_id, source=self.backend))

    def enqueue_delete(self, doc_id: str) -> None:
        self._enqueue(IndexTask(action="delete", doc_id=doc_id, source=self.backend))

    def enqueue_backfill(self, *, limit: int = 3000) -> int:
        if not self.enabled:
            return 0

        count = 0
        snapshots = db.collection("interview_experiences").limit(limit).stream()
        for snapshot in snapshots:
            self.enqueue_upsert(snapshot.id)
            count += 1
        return count

    def _enqueue_firestore(self, task: IndexTask) -> None:
        now = _utc_now()
        task_id = f"{task.action}:{task.doc_id}"
        task_ref = self._task_collection().document(task_id)
        base_payload = {
            "task_id": task_id,
            "doc_id": task.doc_id,
            "action": task.action,
            "status": "queued",
            "next_attempt_at": now,
            "lease_until": None,
            "updated_at": now,
            "last_error": None,
            "worker_id": None,
        }

        try:
            snapshot = task_ref.get()
            if snapshot.exists:
                task_ref.set(base_payload, merge=True)
            else:
                task_ref.set(
                    {
                        **base_payload,
                        "attempts": 0,
                        "created_at": now,
                    },
                    merge=True,
                )
        except Exception:
            logger.exception(
                "Search index distributed enqueue failed action=%s doc_id=%s",
                task.action,
                task.doc_id,
            )

    def _enqueue(self, task: IndexTask) -> None:
        if not self.enabled:
            return

        if self.distributed:
            self._enqueue_firestore(task)
            return

        try:
            self._queue.put_nowait(task)
        except queue.Full:
            logger.warning(
                "Search index queue full; dropping task action=%s doc_id=%s",
                task.action,
                task.doc_id,
            )

    def _claim_firestore_task(self) -> IndexTask | None:
        if not self.distributed:
            return None

        now = _utc_now()
        try:
            sample_size = max(5, int(settings.SEARCH_INDEX_FIRESTORE_CLAIM_BATCH)) * 4
            snapshots = self._task_collection().where(
                "status",
                "in",
                ["queued", "retry"],
            ).limit(sample_size).stream()
        except Exception:
            logger.exception("Failed to query distributed index queue")
            return None

        for snapshot in snapshots:
            data = snapshot.to_dict() or {}
            next_attempt_at = _coerce_datetime(data.get("next_attempt_at"))
            if next_attempt_at and next_attempt_at > now:
                continue

            claimed = self._claim_task_document(snapshot.reference, now)
            if claimed is None:
                continue
            return claimed

        return None

    def _claim_task_document(self, task_ref, now: datetime) -> IndexTask | None:
        transaction = db.transaction()
        lease_seconds = max(15, int(settings.SEARCH_INDEX_LEASE_SECONDS))

        @firestore_admin.transactional
        def _claim(txn):
            snapshot = task_ref.get(transaction=txn)
            if not snapshot.exists:
                return None

            data = snapshot.to_dict() or {}
            status = str(data.get("status") or "").strip().lower()
            if status not in {"queued", "retry"}:
                return None

            next_attempt_at = _coerce_datetime(data.get("next_attempt_at"))
            if next_attempt_at and next_attempt_at > now:
                return None

            lease_until = _coerce_datetime(data.get("lease_until"))
            if lease_until and lease_until > now:
                return None

            action = str(data.get("action") or "upsert").strip().lower()
            doc_id = str(data.get("doc_id") or "").strip()
            attempts = int(data.get("attempts") or 0)
            if not doc_id:
                return None

            txn.set(
                task_ref,
                {
                    "status": "processing",
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                    "worker_id": self._worker_id,
                },
                merge=True,
            )

            return {
                "action": action,
                "doc_id": doc_id,
                "attempts": attempts,
            }

        try:
            payload = _claim(transaction)
        except Exception:
            logger.exception("Distributed queue claim transaction failed")
            return None

        if payload is None:
            return None

        return IndexTask(
            action=str(payload.get("action") or "upsert"),
            doc_id=str(payload.get("doc_id") or ""),
            attempts=int(payload.get("attempts") or 0),
            source="firestore",
            task_ref=task_ref,
        )

    def _complete_firestore_task(self, task: IndexTask) -> None:
        if task.task_ref is None:
            return

        now = _utc_now()
        try:
            if bool(settings.SEARCH_INDEX_DELETE_DONE_TASKS):
                task.task_ref.delete()
            else:
                task.task_ref.set(
                    {
                        "status": "done",
                        "lease_until": None,
                        "done_at": now,
                        "updated_at": now,
                        "last_error": None,
                    },
                    merge=True,
                )
        except Exception:
            logger.exception("Distributed queue task completion update failed")

    def _fail_firestore_task(self, task: IndexTask, error: str) -> None:
        if task.task_ref is None:
            return

        now = _utc_now()
        new_attempts = int(task.attempts) + 1
        max_attempts = max(1, int(settings.SEARCH_INDEX_MAX_ATTEMPTS))
        retry_delay_seconds = min(120, 2 ** min(new_attempts, 6))
        is_failed = new_attempts >= max_attempts

        payload = {
            "status": "failed" if is_failed else "retry",
            "attempts": new_attempts,
            "lease_until": None,
            "updated_at": now,
            "last_error": str(error).strip()[:500],
            "worker_id": self._worker_id,
        }
        if not is_failed:
            payload["next_attempt_at"] = now + timedelta(seconds=retry_delay_seconds)

        try:
            task.task_ref.set(payload, merge=True)
        except Exception:
            logger.exception("Distributed queue failure update failed")

    def _next_task(self) -> IndexTask | None:
        if self.distributed:
            task = self._claim_firestore_task()
            if task is None:
                time.sleep(max(0.05, float(settings.SEARCH_INDEX_POLL_INTERVAL_SECONDS)))
            return task

        try:
            return self._queue.get(timeout=0.4)
        except queue.Empty:
            return None

    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            task = self._next_task()
            if task is None:
                continue

            try:
                if task.action == "delete":
                    self._process_delete(task.doc_id)
                else:
                    self._process_upsert(task.doc_id)
                now = _utc_now()
                self._record_success(doc_id=task.doc_id, action=task.action, now=now)
                if task.source == "firestore":
                    self._complete_firestore_task(task)
            except Exception as exc:
                now = _utc_now()
                self._record_failure(error=str(exc), now=now)
                logger.exception(
                    "Search index task failed action=%s doc_id=%s attempt=%s",
                    task.action,
                    task.doc_id,
                    task.attempts,
                )
                if task.source == "firestore":
                    self._fail_firestore_task(task, str(exc))
                elif task.attempts < 2:
                    task.attempts += 1
                    time.sleep(0.2)
                    self._enqueue(task)
            finally:
                if task.source == "memory":
                    self._queue.task_done()

    def _process_delete(self, doc_id: str) -> None:
        typesense_store.delete_document(doc_id)

    def _process_upsert(self, doc_id: str) -> None:
        snapshot = db.collection("interview_experiences").document(doc_id).get()
        if not snapshot.exists:
            typesense_store.delete_document(doc_id)
            return

        source = snapshot.to_dict() or {}

        questions = source.get("extracted_questions") or []
        question_bits: list[str] = []
        for question in questions[:16]:
            if isinstance(question, dict):
                text = str(question.get("question_text") or question.get("question") or "").strip()
            else:
                text = str(question).strip()
            if text:
                question_bits.append(text)

        embedding_source = " ".join(
            value
            for value in [
                str(source.get("raw_text") or ""),
                str(source.get("summary") or ""),
                " ".join(source.get("topics") or []),
                " ".join(question_bits),
            ]
            if value
        )[:5000]

        vector: list[float] | None = None
        if embedding_source:
            try:
                vector = pipeline.embed(embedding_source).astype("float32").tolist()
            except Exception:
                logger.exception("Embedding generation failed for index task doc_id=%s", doc_id)

        indexed = build_index_document(doc_id=doc_id, source=source, embedding=vector)
        typesense_store.upsert_document(indexed)


search_index_queue = SearchIndexQueue()
