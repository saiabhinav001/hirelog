from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi import Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from firebase_admin import firestore

from app.api.routes import dashboard, experiences, practice, search, users
from app.core.config import settings
from app.core.health_checks import build_api_health_report
from app.core.firebase import db
from app.core.rate_limit import SlidingWindowLimiter, client_identifier
from app.services.index_queue import search_index_queue
from app.services.seed_data import ensure_seeded
from app.api.routes.practice import repair_all_practice_list_stats
from app.api.routes.dashboard import update_dashboard_stats_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("hirelog")
_mutation_limiter = SlidingWindowLimiter(settings.MUTATION_RATE_LIMIT_PER_MINUTE, 60)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # ── Startup ──────────────────────────────────────────────────────────────
    # ALL heavy work runs in background threads so the port binds immediately.
    import threading

    def _bg_bootstrap():
        try:
            db.collection("metadata").document("bootstrap").set(
                {"bootstrapped_at": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
            logger.info("Firestore connection verified")
        except Exception:
            logger.exception("Firestore bootstrap write failed — continuing")

    def _bg_seed():
        try:
            seed_report = ensure_seeded()
            logger.info("Seed data status: %s", seed_report)
        except Exception:
            logger.exception("Seed data check failed — continuing")

    def _bg_dashboard():
        try:
            update_dashboard_stats_async()
            logger.info("Dashboard stats cache refresh triggered")
        except Exception:
            logger.exception("Dashboard stats refresh failed — continuing")

    def _bg_repair():
        try:
            repaired = repair_all_practice_list_stats()
            logger.info("Practice list stats reconciled: %d list(s)", repaired)
        except Exception:
            logger.exception("Practice list stats repair failed")

    def _bg_search_warmup():
        try:
            search.warmup_search_runtime()
            logger.info("Search runtime warmup completed")
        except Exception:
            logger.exception("Search runtime warmup failed")

    def _bg_search_index_bootstrap():
        try:
            if settings.SEARCH_INDEX_WORKER_MODE.strip().lower() == "external":
                logger.info("Search indexing workers are external; API will enqueue tasks only")
                return
            search_index_queue.start()
            if search_index_queue.enabled:
                enqueued = search_index_queue.enqueue_backfill(limit=3000)
                logger.info("Search indexing backfill queued: %d document(s)", enqueued)
        except Exception:
            logger.exception("Search indexing bootstrap failed")

    threading.Thread(target=_bg_bootstrap, daemon=True).start()
    threading.Thread(target=_bg_seed, daemon=True).start()
    threading.Thread(target=_bg_dashboard, daemon=True).start()
    threading.Thread(target=_bg_repair, daemon=True).start()
    threading.Thread(target=_bg_search_warmup, daemon=True).start()
    threading.Thread(target=_bg_search_index_bootstrap, daemon=True).start()
    logger.info("Background tasks started")

    logger.info("Startup complete — ENV=%s", settings.ENV)
    yield
    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down gracefully")


app = FastAPI(title=settings.API_TITLE, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip responses >500 bytes — search results compress ~70%
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    started_at = time.perf_counter()

    if request.method in _WRITE_METHODS and request.url.path.startswith("/api/"):
        try:
            _mutation_limiter.check(
                client_identifier(request),
                detail="Write rate limit exceeded. Please retry shortly.",
            )
        except HTTPException as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            logger.warning(
                "request request_id=%s method=%s path=%s status=%s duration_ms=%.2f limited=true",
                request_id,
                request.method,
                request.url.path,
                exc.status_code,
                duration_ms,
            )
            response = JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail, "request_id": request_id},
                headers=exc.headers,
            )
            response.headers["X-Request-ID"] = request_id
            return response

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started_at) * 1000.0
        logger.exception(
            "request request_id=%s method=%s path=%s status=500 duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - started_at) * 1000.0
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request request_id=%s method=%s path=%s status=%s duration_ms=%.2f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "hirelog", "env": settings.ENV}


@app.get("/health")
def health() -> dict:
    report = build_api_health_report(deep=False)
    return {
        "status": "healthy" if report.get("ready") else "degraded",
        "env": settings.ENV,
        "ready": report.get("ready", False),
    }


@app.get("/health/live")
def health_live() -> dict:
    return {
        "status": "alive",
        "service": "api",
        "env": settings.ENV,
    }


@app.get("/health/ready")
def health_ready(response: FastAPIResponse) -> dict:
    report = build_api_health_report(deep=False)
    if not report.get("ready", False):
        response.status_code = 503
    return report


@app.get("/health/deep")
def health_deep(response: FastAPIResponse) -> dict:
    report = build_api_health_report(deep=True)
    if report.get("overall_status") == "error":
        response.status_code = 503
    return report


app.include_router(users.router)
app.include_router(experiences.router)
app.include_router(search.router)
app.include_router(dashboard.router)
app.include_router(practice.router)
