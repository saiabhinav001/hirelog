from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Response as FastAPIResponse

from app.core.config import settings
from app.core.health_checks import build_worker_health_report
from app.services.index_queue import search_index_queue


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("hirelog-worker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    def _bg_index_worker() -> None:
        try:
            search_index_queue.start()
            if search_index_queue.enabled:
                enqueued = search_index_queue.enqueue_backfill(limit=3000)
                logger.info("Worker queued indexing backfill: %d", enqueued)
        except Exception:
            logger.exception("Worker indexing bootstrap failed")

    threading.Thread(target=_bg_index_worker, daemon=True).start()
    logger.info("Worker startup complete")
    yield
    logger.info("Worker shutting down")


app = FastAPI(title="HireLog Worker", lifespan=lifespan)


@app.get("/")
def root() -> dict:
    return {
        "status": "ok",
        "service": "worker",
        "env": settings.ENV,
    }


@app.get("/health")
def health() -> dict:
    report = build_worker_health_report(deep=False)
    return {
        "status": "healthy" if report.get("ready") else "degraded",
        "env": settings.ENV,
        "ready": report.get("ready", False),
    }


@app.get("/health/live")
def health_live() -> dict:
    return {
        "status": "alive",
        "service": "worker",
        "env": settings.ENV,
    }


@app.get("/health/ready")
def health_ready(response: FastAPIResponse) -> dict:
    report = build_worker_health_report(deep=False)
    if not report.get("ready", False):
        response.status_code = 503
    return report


@app.get("/health/deep")
def health_deep(response: FastAPIResponse) -> dict:
    report = build_worker_health_report(deep=True)
    if report.get("overall_status") == "error":
        response.status_code = 503
    return report
