"""Independently deployable exam API entry point (Prompt 3).

Deliberately separate from `app.main`: this process does NOT seed legacy
demo data, does not touch FAISS/search-index workers, and does not run the
NLP watchdog. Its only startup work is a lightweight Firestore reachability
check and starting the exam outbox drain loop. Feature-flagged end-to-end —
if `EXAMS_ENABLED` is false, every mutating route responds 503 (see
`app.api.exams_dependencies.require_exams_enabled`), so shipping this process
disabled is safe by construction (contract #12).
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    exams_faculty,
    exams_generation,
    exams_institution_admin,
    exams_institution_auth,
    exams_judge,
    exams_kt,
    exams_me,
    exams_student,
)
from app.core.config import settings
from app.core.firebase import db
from app.core.logging_config import configure_logging
from app.domain.exams.clock import system_clock
from app.domain.exams.outbox import CompositeOutboxHandler, LoggingNoopHandler, drain_outbox_once
from app.domain.exams.repository import OutboxRepository

configure_logging(json_logs=settings.ENV.strip().lower() == "production")
logger = logging.getLogger("hirelog.exams.api")

_stop_event = threading.Event()


def _firestore_health_check() -> dict:
    """Deliberately NOT importing `app.core.health_checks` here — that module
    pulls in `app.services.nlp`/`faiss_store` (spaCy/sentence-transformers/
    torch), the exact legacy startup weight this entry point exists to avoid.
    """
    import time as _time

    started = _time.perf_counter()
    try:
        db.collection("metadata").document("bootstrap").get()
        return {"status": "ok", "latency_ms": round((_time.perf_counter() - started) * 1000.0, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)}


def _judge_provider():
    """Real provider only if BOTH base_url and api_key are configured (same
    pattern as the generation provider) — an institutional decision, never a
    code change. See docs/adr/0011 for the hardened-compute requirements a
    real provider must satisfy before this is safe to point at it."""
    from app.domain.judge.provider import DeterministicFakeJudgeProvider, Judge0HttpAdapter

    if settings.EXAM_JUDGE_PROVIDER_BASE_URL and settings.EXAM_JUDGE_PROVIDER_API_KEY:
        return Judge0HttpAdapter(
            base_url=settings.EXAM_JUDGE_PROVIDER_BASE_URL, api_key=settings.EXAM_JUDGE_PROVIDER_API_KEY
        )
    return DeterministicFakeJudgeProvider()


def _outbox_drain_loop() -> None:
    from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler
    from app.domain.judge.runtime_allowlist import default_allowlist

    repo = OutboxRepository(db)
    handler = CompositeOutboxHandler(
        [LoggingNoopHandler(), GradingTriggerOutboxHandler(db, _judge_provider(), default_allowlist())]
    )
    while not _stop_event.is_set():
        try:
            drain_outbox_once(
                repo,
                handler,
                now=system_clock.now(),
                limit=25,
                lease_seconds=settings.EXAM_OUTBOX_LEASE_SECONDS,
                max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS,
            )
        except Exception:
            logger.exception("exam outbox drain tick failed")
        _stop_event.wait(2.0)


def _judge_sweep_loop() -> None:
    """Completes FINAL_GRADE submissions the outbox handler created (nobody
    polls those over HTTP) and any PUBLIC_RUN submission a student stopped
    polling (e.g. closed the tab) — bounded, idempotent, safe after a crash
    (see `JudgeService.sweep_pending_once` docstring)."""
    from app.domain.judge.service import JudgeService

    service = JudgeService(db, _judge_provider())
    while not _stop_event.is_set():
        try:
            service.sweep_pending_once(limit=25)
        except Exception:
            logger.exception("judge submission sweep tick failed")
        _stop_event.wait(2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.EXAMS_ENABLED:
        logger.warning("EXAMS_ENABLED is false — exam API is up but all mutating routes return 503")

    def _bg_bootstrap() -> None:
        report = _firestore_health_check()
        if report.get("status") == "ok":
            logger.info("Exam API: Firestore connection verified")
        else:
            logger.error("Exam API: Firestore check failed — %s", report.get("detail"))

    threading.Thread(target=_bg_bootstrap, daemon=True).start()
    if settings.EXAMS_ENABLED:
        threading.Thread(target=_outbox_drain_loop, daemon=True).start()
        threading.Thread(target=_judge_sweep_loop, daemon=True).start()

    logger.info("Exam API startup complete — ENV=%s EXAMS_ENABLED=%s", settings.ENV, settings.EXAMS_ENABLED)
    yield
    _stop_event.set()
    logger.info("Exam API shutting down")


app = FastAPI(title="HireLog Exams API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_store_middleware(request, call_next):
    """Belt-and-suspenders: every exam API response is no-store, even ones a
    route handler forgot to mark explicitly (contract #9)."""
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/")
def root() -> dict:
    return {"status": "ok", "service": "hirelog-exams", "env": settings.ENV, "exams_enabled": settings.EXAMS_ENABLED}


@app.get("/health/live")
def health_live() -> dict:
    return {"status": "alive", "service": "exams-api", "env": settings.ENV}


@app.get("/health/ready")
def health_ready(response: FastAPIResponse) -> dict:
    report = _firestore_health_check()
    ready = report.get("status") == "ok"
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "degraded", "firestore": report, "exams_enabled": settings.EXAMS_ENABLED}


app.include_router(exams_faculty.router)
app.include_router(exams_kt.router)
app.include_router(exams_generation.router)
app.include_router(exams_institution_auth.router)
app.include_router(exams_institution_admin.router)
app.include_router(exams_me.router)
app.include_router(exams_student.router)
app.include_router(exams_judge.student_router)
app.include_router(exams_judge.faculty_router)
