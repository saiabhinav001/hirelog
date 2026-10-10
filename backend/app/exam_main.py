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
    exams_gradebook,
    exams_institution_admin,
    exams_institution_auth,
    exams_judge,
    exams_kt,
    exams_live,
    exams_lockdown,
    exams_me,
    exams_proctoring,
    exams_student,
)
from app.core import background
from app.core.background import AdaptivePoller
from app.core.config import settings
from app.core.firebase import db
from app.core.logging_config import configure_logging
from app.domain.exams.clock import system_clock
from app.domain.exams.outbox import CompositeOutboxHandler, LoggingNoopHandler, drain_outbox_once
from app.domain.exams.repository import OutboxRepository

configure_logging(json_logs=settings.ENV.strip().lower() == "production")
logger = logging.getLogger("hirelog.exams.api")

_health_cache: dict = {}


def _firestore_health_check(*, use_cache: bool = True) -> dict:
    """Deliberately NOT importing `app.core.health_checks` here — that module
    pulls in `app.services.nlp`/`faiss_store` (spaCy/sentence-transformers/
    torch), the exact legacy startup weight this entry point exists to avoid.
    """
    import time as _time

    now = _time.monotonic()
    cached = _health_cache.get("ok")
    if use_cache and cached and now - cached[0] < settings.EXAM_HEALTH_CACHE_SECONDS:
        return cached[1]  # readiness probes must not burn free-tier reads

    started = _time.perf_counter()
    try:
        db.collection("exam_health").document("ping").get()
        report = {"status": "ok", "latency_ms": round((_time.perf_counter() - started) * 1000.0, 2)}
        _health_cache["ok"] = (now, report)
        return report
    except Exception as exc:  # noqa: BLE001
        _health_cache.pop("ok", None)
        return {"status": "error", "detail": str(exc)}


_judge_fake_provider_singleton = None


def _judge_provider():
    """Real provider only if BOTH base_url and api_key are configured (same
    pattern as the generation provider) — an institutional decision, never a
    code change. See docs/adr/0011 for the hardened-compute requirements a
    real provider must satisfy before this is safe to point at it.

    Unlike the other fake dev providers (generation/review/feedback — pure
    functions of their input, fine to reconstruct per call), the fake judge
    is STATEFUL: `submit()` stores a pending result keyed by token, and a
    LATER poll request must find that same token via `get_result()`. A real
    Judge0 instance keeps that state on its own server regardless of how
    many client instances call it; reconstructing a fresh
    `DeterministicFakeJudgeProvider()` on every request (its `_pending` dict
    always starts empty) permanently breaks that contract — every poll
    after the first would find nothing and report "still processing"
    forever (found live: a real public-test run that never resolved, not
    even after far more polls than a genuine async judge would ever need).
    Process-wide singleton keeps `_pending` alive across requests, matching
    what a real external judge naturally provides for free.
    """
    from app.domain.judge.provider import DeterministicFakeJudgeProvider, Judge0HttpAdapter

    if settings.EXAM_JUDGE_PROVIDER_BASE_URL and settings.EXAM_JUDGE_PROVIDER_API_KEY:
        return Judge0HttpAdapter(
            base_url=settings.EXAM_JUDGE_PROVIDER_BASE_URL, api_key=settings.EXAM_JUDGE_PROVIDER_API_KEY
        )
    global _judge_fake_provider_singleton
    if _judge_fake_provider_singleton is None:
        _judge_fake_provider_singleton = DeterministicFakeJudgeProvider()
    return _judge_fake_provider_singleton


def _similarity_provider():
    """Real provider only if a JPlag jar path is configured — same pattern
    as `_judge_provider`. See docs/adr/0014 for what "configured" actually
    requires (a real, separately-validated JPlag CLI installation; this
    setting alone does not create or validate one)."""
    from app.domain.gradebook.similarity_provider import DeterministicFakeSimilarityProvider, JPlagCliAdapter

    if settings.EXAM_JPLAG_JAR_PATH:
        return JPlagCliAdapter(
            jplag_jar_path=settings.EXAM_JPLAG_JAR_PATH, java_executable=settings.EXAM_JPLAG_JAVA_EXECUTABLE
        )
    return DeterministicFakeSimilarityProvider()


def _make_outbox_tick():
    from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler
    from app.domain.judge.runtime_allowlist import default_allowlist

    repo = OutboxRepository(db)
    handler = CompositeOutboxHandler(
        [LoggingNoopHandler(), GradingTriggerOutboxHandler(db, _judge_provider(), default_allowlist())]
    )

    def tick() -> int:
        handled = drain_outbox_once(
            repo,
            handler,
            now=system_clock.now(),
            limit=25,
            lease_seconds=settings.EXAM_OUTBOX_LEASE_SECONDS,
            max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS,
        )
        if handled:
            background.wake("judge")  # the grading trigger just queued FINAL_GRADE submissions
        return handled

    return tick


def _make_judge_sweep_tick():
    """Completes FINAL_GRADE submissions the outbox handler created (nobody
    polls those over HTTP) and any PUBLIC_RUN submission a student stopped
    polling (e.g. closed the tab) — bounded, idempotent, safe after a crash
    (see `JudgeService.sweep_pending_once` docstring)."""
    from app.domain.judge.service import JudgeService

    service = JudgeService(db, _judge_provider())
    return lambda: service.sweep_pending_once(limit=25)


def _make_proctoring_sweep_tick():
    """Closes departure incidents for students who left the tab entirely
    without ever sending an end event — otherwise those incidents would
    stay open (and uncounted) forever."""
    from app.domain.proctoring.ledger_service import ProctoringLedgerService

    service = ProctoringLedgerService(db)

    def tick() -> int:
        closed = service.sweep_stale_incidents_once(
            grace_seconds=settings.EXAM_PROCTORING_GRACE_SECONDS,
            stale_after_seconds=settings.EXAM_PROCTORING_STALE_INCIDENT_SECONDS,
        )
        return closed or service.last_open_incident_count  # stay fast while incidents are still open

    return tick


def _make_retention_sweep_tick():
    """Deletes evidence (storage bytes + Firestore record together) once
    past the configured retention window — bounded, idempotent."""
    from app.api.routes.exams_proctoring import _storage
    from app.domain.proctoring.evidence_service import EvidenceService
    from app.domain.proctoring.retention import RetentionSweeper

    sweeper = RetentionSweeper(EvidenceService(db, _storage()))

    def tick() -> bool:
        sweeper.sweep_once(retention_days=settings.EXAM_EVIDENCE_RETENTION_DAYS, limit=100)
        return False

    return tick


def start_background_workers() -> None:
    """Free-tier friendly: each loop backs off while idle and is woken by
    in-process work (see `app.core.background`)."""
    workers = [
        AdaptivePoller("outbox", _make_outbox_tick(), min_interval=settings.EXAM_OUTBOX_POLL_MIN_SECONDS,
                       max_interval=settings.EXAM_OUTBOX_POLL_MAX_SECONDS),
        AdaptivePoller("judge", _make_judge_sweep_tick(), min_interval=settings.EXAM_JUDGE_SWEEP_MIN_SECONDS,
                       max_interval=settings.EXAM_JUDGE_SWEEP_MAX_SECONDS),
        AdaptivePoller("proctoring", _make_proctoring_sweep_tick(), burst_seconds=180.0,
                       min_interval=settings.EXAM_PROCTORING_SWEEP_MIN_SECONDS,
                       max_interval=settings.EXAM_PROCTORING_SWEEP_MAX_SECONDS),
        AdaptivePoller("retention", _make_retention_sweep_tick(), min_interval=3600.0, max_interval=3600.0),
    ]
    for worker in workers:
        background.start_daemon(worker)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.EXAMS_ENABLED:
        logger.warning("EXAMS_ENABLED is false — exam API is up but all mutating routes return 503")
    if not settings.EXAM_STORAGE_BOUNDARY:
        logger.warning("EXAM_STORAGE_BOUNDARY is off — exam code is NOT confined to exam collections")

    def _bg_bootstrap() -> None:
        report = _firestore_health_check(use_cache=False)
        if report.get("status") == "ok":
            logger.info("Exam API: Firestore connection verified")
        else:
            logger.error("Exam API: Firestore check failed — %s", report.get("detail"))

    threading.Thread(target=_bg_bootstrap, daemon=True).start()
    if settings.EXAMS_ENABLED:
        start_background_workers()

    logger.info("Exam API startup complete — ENV=%s EXAMS_ENABLED=%s", settings.ENV, settings.EXAMS_ENABLED)
    yield
    background.stop_all()
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
    return {
        "status": "ready" if ready else "degraded",
        "firestore": report,
        "exams_enabled": settings.EXAMS_ENABLED,
        "storage_boundary": {
            "enforced": settings.EXAM_STORAGE_BOUNDARY,
            "collection_prefix": settings.EXAM_COLLECTION_PREFIX,
        },
    }


app.include_router(exams_faculty.router)
app.include_router(exams_kt.router)
app.include_router(exams_generation.router)
app.include_router(exams_institution_auth.router)
app.include_router(exams_institution_admin.router)
app.include_router(exams_me.router)
app.include_router(exams_student.router)
app.include_router(exams_judge.student_router)
app.include_router(exams_judge.faculty_router)
app.include_router(exams_live.router)
app.include_router(exams_proctoring.student_router)
app.include_router(exams_proctoring.faculty_router)
app.include_router(exams_gradebook.student_router)
app.include_router(exams_gradebook.faculty_router)
app.include_router(exams_lockdown.router)
