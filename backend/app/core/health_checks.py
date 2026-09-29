from __future__ import annotations

from datetime import datetime, timezone
import time

from app.core.firebase import db
from app.services.faiss_store import faiss_store
from app.services.index_queue import search_index_queue


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: object) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None


def firestore_health_check() -> dict:
    started = time.perf_counter()
    try:
        _ = db.collection("metadata").document("bootstrap").get()
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "status": "ok",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "status": "error",
            "latency_ms": latency_ms,
            "detail": str(exc),
        }


def _index_freshness_check(queue_snapshot: dict) -> dict:
    if not bool(queue_snapshot.get("enabled", False)):
        return {
            "status": "skipped",
            "detail": "index queue disabled",
        }

    freshness_seconds = queue_snapshot.get("freshness_seconds")
    last_success_at = queue_snapshot.get("last_success_at")

    if freshness_seconds is None:
        queued = int(queue_snapshot.get("queued", 0) or 0)
        if queued > 0:
            return {
                "status": "warn",
                "detail": "no successful indexing event recorded yet",
                "queued": queued,
                "last_success_at": last_success_at,
            }
        return {
            "status": "ok",
            "detail": "queue idle and no freshness signal required yet",
            "last_success_at": last_success_at,
        }

    if bool(queue_snapshot.get("stale", False)):
        return {
            "status": "warn",
            "detail": "index freshness exceeded warning threshold",
            "freshness_seconds": int(freshness_seconds),
            "last_success_at": last_success_at,
            "warn_after_seconds": queue_snapshot.get("freshness_warn_after_seconds"),
        }

    return {
        "status": "ok",
        "freshness_seconds": int(freshness_seconds),
        "last_success_at": last_success_at,
        "warn_after_seconds": queue_snapshot.get("freshness_warn_after_seconds"),
    }


def _overall_status(checks: dict) -> str:
    statuses = [
        str(value.get("status") or "unknown")
        for value in checks.values()
        if isinstance(value, dict)
    ]
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


def build_api_health_report(*, deep: bool) -> dict:
    firestore = firestore_health_check()
    queue = search_index_queue.status(include_stats=deep)
    index_freshness = _index_freshness_check(queue)

    checks = {
        "api": {
            "status": "ok",
            "timestamp": _utc_now().isoformat(),
        },
        "firestore": firestore,
        "queue": {
            "status": "ok" if queue.get("started") or not queue.get("enabled") else "warn",
            **queue,
        },
        "index_freshness": index_freshness,
    }

    if deep:
        checks["faiss"] = {
            "status": "ok",
            "vectors": int(faiss_store.index.ntotal),
        }

    status = _overall_status(checks)
    return {
        "service": "api",
        "overall_status": status,
        "ready": status != "error",
        "checks": checks,
    }


def build_worker_health_report(*, deep: bool) -> dict:
    firestore = firestore_health_check()
    queue = search_index_queue.status(include_stats=deep)
    index_freshness = _index_freshness_check(queue)

    checks = {
        "worker": {
            "status": "ok",
            "started": bool(queue.get("started", False)),
            "timestamp": _utc_now().isoformat(),
        },
        "firestore": firestore,
        "queue": {
            "status": "ok" if queue.get("started") or not queue.get("enabled") else "warn",
            **queue,
        },
        "index_freshness": index_freshness,
    }

    if deep:
        checks["worker_runtime"] = {
            "status": "ok",
            "last_success_at": queue.get("last_success_at"),
            "failed": queue.get("failed", 0),
            "processing": queue.get("processing", 0),
        }

    status = _overall_status(checks)
    return {
        "service": "worker",
        "overall_status": status,
        "ready": status != "error",
        "checks": checks,
    }
