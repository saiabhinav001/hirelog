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
from typing import Optional

from fastapi import FastAPI
from fastapi import Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.api_core import exceptions as gcloud_exceptions

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
    exams_ops,
    exams_proctoring,
    exams_student,
)
from app.core import background
from app.core.background import AdaptivePoller
from app.core.config import settings
from app.core.firebase import exam_db as db
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


def _run_adaptive(
    name: str, tick, *, min_interval: float, max_interval: float, burst_seconds: float = 30.0, hint=None
) -> None:
    """Runs one worker until `stop_exam_workers()`. Every empty poll costs a Firestore read and the free plan allows
    50,000 a day, so a worker polls fast only while it finds work and is woken at once by `background.wake(name)`."""
    if _stop_event.is_set():
        return
    poller = AdaptivePoller(
        name, tick, min_interval=min_interval, max_interval=max_interval, burst_seconds=burst_seconds, hint=hint
    )
    background.register(poller).run()


def _outbox_drain_loop() -> None:
    from app.domain.judge.outbox_handler import GradingTriggerOutboxHandler
    from app.domain.judge.runtime_allowlist import configured_allowlist

    repo = OutboxRepository(db)
    handler = CompositeOutboxHandler(
        [LoggingNoopHandler(), GradingTriggerOutboxHandler(db, _judge_provider(), configured_allowlist())]
    )

    def tick() -> int:
        handled = drain_outbox_once(
            repo,
            handler,
            now=system_clock.now(),
            limit=25,
            lease_seconds=settings.EXAM_OUTBOX_LEASE_SECONDS,
            max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS,
            exclude_kinds=(KT_INGEST_KIND,),
        )
        if handled:
            background.wake("judge")  # a grading trigger may just have queued FINAL_GRADE submissions
        return handled

    _run_adaptive(
        "outbox", tick,
        min_interval=settings.EXAM_OUTBOX_POLL_MIN_SECONDS, max_interval=settings.EXAM_OUTBOX_POLL_MAX_SECONDS,
    )


def _kt_ingestion_loop() -> None:
    """Scan + parse uploaded sources, one at a time, on the shared outbox. Parsing can take ~a minute, so it
    has its own loop and a longer lease: grading and run commands never wait behind a document."""
    handler = _kt_ingestion_handler()
    if handler is None:
        return
    repo = OutboxRepository(db)

    def tick() -> int:
        return drain_outbox_once(
            repo, handler, now=system_clock.now(), limit=1,
            lease_seconds=max(120, settings.EXAM_OUTBOX_LEASE_SECONDS * 2),
            max_attempts=settings.EXAM_OUTBOX_MAX_ATTEMPTS, kinds=[KT_INGEST_KIND],
        )

    _run_adaptive(
        "kt", tick,
        min_interval=settings.EXAM_OUTBOX_POLL_MIN_SECONDS, max_interval=settings.EXAM_OUTBOX_POLL_MAX_SECONDS,
    )


def _judge_sweep_loop() -> None:
    """Completes FINAL_GRADE submissions the outbox handler created (nobody
    polls those over HTTP) and any PUBLIC_RUN submission a student stopped
    polling (e.g. closed the tab) — bounded, idempotent, safe after a crash
    (see `JudgeService.sweep_pending_once` docstring)."""
    from app.domain.judge.service import JudgeService

    service = JudgeService(db, _judge_provider())
    _run_adaptive(
        "judge", lambda: service.sweep_pending_once(limit=25),
        min_interval=settings.EXAM_JUDGE_SWEEP_MIN_SECONDS, max_interval=settings.EXAM_JUDGE_SWEEP_MAX_SECONDS,
    )


def _proctoring_stale_incident_sweep_loop() -> None:
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
        return closed or service.last_open_incident_count  # stay on the fast cadence while incidents are still open

    _run_adaptive(
        "proctoring", tick, burst_seconds=180.0,
        min_interval=settings.EXAM_PROCTORING_SWEEP_MIN_SECONDS, max_interval=settings.EXAM_PROCTORING_SWEEP_MAX_SECONDS,
    )


def _finalization_sweep_loop() -> None:
    """Server-side auto-finalization (Prompt 3, docs/adr/0019): seals attempts
    whose own (accommodated/extended) deadline has passed, with a receipt and a
    grading trigger, and - only when EXAM_SCHEDULE_AUTOMATION_ENABLED - performs
    scheduled release/close. Each seal is its own transaction and every write
    re-checks state, so running this in several instances is safe."""
    from app.domain.exams.finalization import ExamFinalizationSweeper

    sweeper = ExamFinalizationSweeper(db)
    interval = max(1.0, float(settings.EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS))

    def tick() -> int:
        applied = 0
        if settings.EXAM_SCHEDULE_AUTOMATION_ENABLED:
            applied = sweeper.run_schedule_automation_once()
        sealed = sweeper.sweep_active_runs_once(batch_size=settings.EXAM_FINALIZATION_BATCH_SIZE)
        if sealed:
            background.wake("outbox")  # sealing enqueued grading triggers
        return sealed + applied

    def hint() -> Optional[float]:
        if settings.EXAM_SCHEDULE_AUTOMATION_ENABLED:
            return 0.0  # scheduled release/close is clock-driven: keep the configured cadence
        return sweeper.seconds_until_next_deadline()

    _run_adaptive(
        "finalization", tick, hint=hint, min_interval=interval,
        max_interval=max(interval, float(settings.EXAM_FINALIZATION_SWEEP_MAX_INTERVAL_SECONDS)),
    )


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


def _export_expiry_sweep_loop() -> None:
    """Deletes the stored content of gradebook exports past `EXAM_EXPORT_RETENTION_HOURS` (the job record stays).
    Unlike the evidence sweep it also runs without a bucket, because exports then sit in process memory and must still
    be dropped. Bounded and idempotent; safe in several instances."""
    from app.api.routes.exams_proctoring import _storage
    from app.domain.gradebook.export_service import GradebookExportService

    service = GradebookExportService(db, _storage(), retention_hours=settings.EXAM_EXPORT_RETENTION_HOURS)
    while not _stop_event.is_set():
        try:
            service.sweep_expired()
        except Exception:
            logger.exception("export expiry sweep tick failed")
        _stop_event.wait(900.0)


_workers_started = False


def start_exam_workers() -> None:
    """Starts the exam background loops once per process. Used by this entry point's lifespan and by `app.main`
    when the exam API is mounted into it (a mounted sub-app's own lifespan never runs)."""
    global _workers_started
    if _workers_started or not settings.EXAMS_ENABLED:
        return
    _workers_started = True
    _stop_event.clear()
    threading.Thread(target=_outbox_drain_loop, daemon=True).start()
    threading.Thread(target=_kt_ingestion_loop, daemon=True).start()
    threading.Thread(target=_judge_sweep_loop, daemon=True).start()
    threading.Thread(target=_proctoring_stale_incident_sweep_loop, daemon=True).start()
    threading.Thread(target=_evidence_retention_sweep_loop, daemon=True).start()
    threading.Thread(target=_export_expiry_sweep_loop, daemon=True).start()
    if settings.EXAM_FINALIZATION_SWEEP_ENABLED:
        threading.Thread(target=_finalization_sweep_loop, daemon=True).start()
    else:
        logger.warning("EXAM_FINALIZATION_SWEEP_ENABLED is false - expired attempts will NOT be auto-finalized")


def stop_exam_workers() -> None:
    global _workers_started
    _stop_event.set()
    background.stop_all()
    _workers_started = False


def mount_exam_api(parent: FastAPI) -> None:
    """Serves the exam API from an existing FastAPI app (the main HireLog backend) without touching its routes,
    middleware or error handling: the exam app keeps its own handlers and no-store middleware. Mounted last, so
    every existing route still matches first."""
    parent.mount("/", app)


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
    start_exam_workers()
    logger.info("Exam API startup complete — ENV=%s EXAMS_ENABLED=%s", settings.ENV, settings.EXAMS_ENABLED)
    yield
    stop_exam_workers()
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


_RETRYABLE_BACKEND_ERRORS = (
    gcloud_exceptions.Aborted,
    gcloud_exceptions.ServiceUnavailable,
    gcloud_exceptions.DeadlineExceeded,
    gcloud_exceptions.ResourceExhausted,
    gcloud_exceptions.RetryError,
)


def _temporarily_unavailable() -> JSONResponse:
    """A database outage or exhausted transaction retries is a retryable 503, never a bare 500: nothing was committed,
    and every write the client retries carries an idempotency key or an expected version."""
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "temporarily_unavailable", "message": "The exam service is busy or its database is temporarily unavailable. Retry the same request."}},
        headers={"Retry-After": "2", "Cache-Control": "no-store"},
    )


@app.exception_handler(gcloud_exceptions.GoogleAPIError)
async def _backend_error_handler(request, exc):
    logger.error("datastore error on %s %s: %s", request.method, request.url.path, exc.__class__.__name__, exc_info=exc)
    if isinstance(exc, _RETRYABLE_BACKEND_ERRORS):
        return _temporarily_unavailable()
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"}, headers={"Cache-Control": "no-store"})


@app.exception_handler(ValueError)
async def _value_error_handler(request, exc):
    # google-cloud-firestore raises a bare ValueError when a transaction keeps aborting (contention).
    if str(exc).startswith("Failed to commit transaction"):
        logger.warning("transaction contention on %s %s: %s", request.method, request.url.path, exc)
        return _temporarily_unavailable()
    logger.error("unhandled ValueError on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"}, headers={"Cache-Control": "no-store"})


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
app.include_router(exams_ops.router)
