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
from app.core.config import settings
from app.core.firebase import db
from app.core.logging_config import configure_logging
from app.domain.exams.clock import system_clock
from app.domain.exams.outbox import CompositeOutboxHandler, LoggingNoopHandler, drain_outbox_once
from app.domain.exams.repository import OutboxRepository
from app.domain.kt.ingestion import KT_INGEST_KIND

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
    from app.domain.judge.provider import DeterministicFakeJudgeProvider, Judge0HttpAdapter, UnconfiguredJudgeProvider

    if settings.EXAM_JUDGE_PROVIDER_BASE_URL and settings.EXAM_JUDGE_PROVIDER_API_KEY:
        return Judge0HttpAdapter(
            base_url=settings.EXAM_JUDGE_PROVIDER_BASE_URL, api_key=settings.EXAM_JUDGE_PROVIDER_API_KEY
        )
    if settings.is_production:
        # The fake answers "Accepted" to everything; in production that would award full marks to unexecuted code.
        return UnconfiguredJudgeProvider()
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


def _kt_ingestion_handler():
    """Scan + parse for uploaded sources (docs/adr/0020). Absent when private storage is not configured
    in production: uploads are refused at the route in that case, so there is nothing to process."""
    from app.api.routes.exams_kt import _scanner
    from app.domain.kt.ingestion import KtIngestionOutboxHandler, KtIngestionService
    from app.domain.kt.storage import StorageNotConfiguredError, kt_storage

    try:
        storage = kt_storage()
    except StorageNotConfiguredError:
        logger.warning("KT ingestion worker not started: EXAM_EVIDENCE_STORAGE_BUCKET is not configured")
        return None
    service = KtIngestionService(
        db, storage, _scanner(), max_active_uploads=settings.EXAM_KT_MAX_ACTIVE_UPLOADS_PER_USER,
        parse_memory_mb=settings.EXAM_KT_PARSE_MEMORY_MB,
    )
    return KtIngestionOutboxHandler(service, max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS)


def _outbox_drain_loop() -> None:
    from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler
    from app.domain.judge.runtime_allowlist import configured_allowlist

    repo = OutboxRepository(db)
    handler = CompositeOutboxHandler(
        [LoggingNoopHandler(), GradingTriggerOutboxHandler(db, _judge_provider(), configured_allowlist())]
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
                exclude_kinds=(KT_INGEST_KIND,),
            )
        except Exception:
            logger.exception("exam outbox drain tick failed")
        _stop_event.wait(2.0)


def _kt_ingestion_loop() -> None:
    """Scan + parse uploaded sources, one at a time, on the shared outbox. Parsing can take ~a minute, so it
    has its own loop and a longer lease: grading and run commands never wait behind a document."""
    handler = _kt_ingestion_handler()
    if handler is None:
        return
    repo = OutboxRepository(db)
    while not _stop_event.is_set():
        try:
            handled = drain_outbox_once(
                repo, handler, now=system_clock.now(), limit=1,
                lease_seconds=max(120, settings.EXAM_OUTBOX_LEASE_SECONDS * 2),
                max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS, kinds=[KT_INGEST_KIND],
            )
        except Exception:
            logger.exception("kt ingestion drain tick failed")
            handled = 0
        if not handled:
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


def _proctoring_stale_incident_sweep_loop() -> None:
    """Closes departure incidents for students who left the tab entirely
    without ever sending an end event — otherwise those incidents would
    stay open (and uncounted) forever."""
    from app.domain.proctoring.ledger_service import ProctoringLedgerService

    service = ProctoringLedgerService(db)
    while not _stop_event.is_set():
        try:
            service.sweep_stale_incidents_once(
                grace_seconds=settings.EXAM_PROCTORING_GRACE_SECONDS,
                stale_after_seconds=settings.EXAM_PROCTORING_STALE_INCIDENT_SECONDS,
            )
        except Exception:
            logger.exception("proctoring stale-incident sweep tick failed")
        _stop_event.wait(15.0)


def _finalization_sweep_loop() -> None:
    """Server-side auto-finalization (Prompt 3, docs/adr/0019): seals attempts
    whose own (accommodated/extended) deadline has passed, with a receipt and a
    grading trigger, and - only when EXAM_SCHEDULE_AUTOMATION_ENABLED - performs
    scheduled release/close. Each seal is its own transaction and every write
    re-checks state, so running this in several instances is safe."""
    from app.domain.exams.finalization import ExamFinalizationSweeper

    sweeper = ExamFinalizationSweeper(db)
    interval = max(1.0, float(settings.EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS))
    while not _stop_event.is_set():
        try:
            if settings.EXAM_SCHEDULE_AUTOMATION_ENABLED:
                sweeper.run_schedule_automation_once()
            sweeper.sweep_active_runs_once(batch_size=settings.EXAM_FINALIZATION_BATCH_SIZE)
        except Exception:
            logger.exception("exam finalization sweep tick failed")
        _stop_event.wait(interval)


def _evidence_retention_sweep_loop() -> None:
    """Deletes evidence (storage bytes + Firestore record together) once past the run's retention window - held while
    the attempt is blocked or an appeal is pending - and marks upload requests that can no longer complete as missing.
    Bounded and idempotent. Not started in production without a bucket: nothing real is stored there."""
    from app.api.routes.exams_proctoring import _storage
    from app.domain.exams.repository import AttemptRepository, ExamRunRepository
    from app.domain.proctoring.evidence_service import EvidenceService
    from app.domain.proctoring.repository import AppealRepository
    from app.domain.proctoring.retention import RetentionSweeper

    if settings.is_production and not settings.EXAM_EVIDENCE_STORAGE_BUCKET:
        logger.warning("evidence retention sweep not started: EXAM_EVIDENCE_STORAGE_BUCKET is not configured")
        return
    sweeper = RetentionSweeper(
        EvidenceService(db, _storage()), runs=ExamRunRepository(db), attempts=AttemptRepository(db), appeals=AppealRepository(db),
    )
    while not _stop_event.is_set():
        try:
            sweeper.expire_stale_requests(upload_ticket_ttl_seconds=settings.EXAM_EVIDENCE_UPLOAD_TICKET_TTL_SECONDS)
            sweeper.sweep_once(retention_days=settings.EXAM_EVIDENCE_RETENTION_DAYS, limit=100)
        except Exception:
            logger.exception("evidence retention sweep tick failed")
        _stop_event.wait(900.0)


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
        threading.Thread(target=_kt_ingestion_loop, daemon=True).start()
        threading.Thread(target=_judge_sweep_loop, daemon=True).start()
        threading.Thread(target=_proctoring_stale_incident_sweep_loop, daemon=True).start()
        threading.Thread(target=_evidence_retention_sweep_loop, daemon=True).start()
        if settings.EXAM_FINALIZATION_SWEEP_ENABLED:
            threading.Thread(target=_finalization_sweep_loop, daemon=True).start()
        else:
            logger.warning("EXAM_FINALIZATION_SWEEP_ENABLED is false - expired attempts will NOT be auto-finalized")
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
app.include_router(exams_live.router)
app.include_router(exams_proctoring.student_router)
app.include_router(exams_proctoring.faculty_router)
app.include_router(exams_gradebook.student_router)
app.include_router(exams_gradebook.faculty_router)
app.include_router(exams_lockdown.router)
