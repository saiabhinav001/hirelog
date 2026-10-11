from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"

# ── Document schema versions ─────────────────────────────────────────────────
# Stamped onto newly-written documents so future migrations have a version
# signal to key off (bump when a collection's shape changes, then migrate
# documents with a lower version). Existing pre-versioning documents read back
# with no `schema_version` field, which callers treat as version 0.
EXPERIENCE_SCHEMA_VERSION = 1
PRACTICE_LIST_SCHEMA_VERSION = 1


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_ignore_empty=True)

    API_TITLE: str = "HireLog API"
    ENV: str = "development"

    # Firebase — production uses JSON env var; local dev uses file path
    FIREBASE_SERVICE_ACCOUNT_JSON: Optional[str] = None
    FIREBASE_SERVICE_ACCOUNT_PATH: Optional[str] = None
    FIREBASE_PROJECT_ID: Optional[str] = None

    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://localhost:3002"

    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIM: int = 384

    MAX_SEARCH_RESULTS: int = 20
    DASHBOARD_SAMPLE_LIMIT: int = 500
    SEARCH_KEYWORD_SCAN_LIMIT: int = 400
    SEARCH_RATE_LIMIT_PER_MINUTE: int = 120
    SEARCH_SEMANTIC_MAX_CONCURRENCY: int = 4
    SEARCH_SEMANTIC_SLOT_WAIT_SECONDS: float = 0.12
    SEARCH_SEMANTIC_FAILURE_WINDOW_SECONDS: int = 60
    SEARCH_SEMANTIC_FAILURE_THRESHOLD: int = 8
    SEARCH_SEMANTIC_COOLDOWN_SECONDS: int = 45
    SEARCH_ENABLE_WARMUP: bool = True
    SEARCH_WARMUP_QUERIES: str = "dsa interview questions,system design round,dbms sql joins,os process thread"
    SEARCH_ENGINE: str = "faiss"
    TYPESENSE_ENABLED: bool = False
    TYPESENSE_HOST: str = "localhost"
    TYPESENSE_PORT: int = 8108
    TYPESENSE_PROTOCOL: str = "http"
    TYPESENSE_API_KEY: Optional[str] = None
    TYPESENSE_COLLECTION: str = "interview_experiences"
    SEARCH_VECTOR_ALPHA: float = 0.72
    SEARCH_RRF_K: int = 60
    SEARCH_RRF_VECTOR_WEIGHT: float = 0.7
    SEARCH_RRF_LEXICAL_WEIGHT: float = 0.3
    SEARCH_INDEX_WORKERS: int = 2
    SEARCH_INDEX_QUEUE_MAX: int = 2000
    SEARCH_INDEX_WORKER_MODE: str = "embedded"
    SEARCH_INDEX_QUEUE_BACKEND: str = "memory"
    SEARCH_INDEX_FIRESTORE_COLLECTION: str = "search_index_tasks"
    SEARCH_INDEX_MAX_ATTEMPTS: int = 5
    SEARCH_INDEX_LEASE_SECONDS: int = 120
    SEARCH_INDEX_POLL_INTERVAL_SECONDS: float = 0.6
    SEARCH_INDEX_FIRESTORE_CLAIM_BATCH: int = 10
    SEARCH_INDEX_DELETE_DONE_TASKS: bool = True
    SEARCH_INDEX_FRESHNESS_WARN_SECONDS: int = 900
    SEARCH_REDIS_URL: Optional[str] = None
    SEARCH_CACHE_TTL_SECONDS: int = 180
    SEARCH_RERANK_ENABLED: bool = True
    SEARCH_RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    SEARCH_RERANK_TOP_K: int = 30
    SEARCH_RERANK_WEIGHT: float = 0.3
    SEARCH_LONG_LATENCY_MS: float = 1200.0
    SEARCH_ANALYTICS_TOP_QUERIES: int = 20
    DASHBOARD_RATE_LIMIT_PER_MINUTE: int = 90
    MUTATION_RATE_LIMIT_PER_MINUTE: int = 80

    FAISS_DIR: Optional[str] = None
    FAISS_INDEX_PATH: Optional[str] = None
    FAISS_MAPPING_PATH: Optional[str] = None

    PLACEMENT_CELL_EMAILS: str = ""

    # ── Exams module (feature-flagged, off by default) ───────────────────────
    # See docs/adr/0008-exam-domain-state-machine.md. Kept fully separate from
    # the legacy `role` field on `users/{uid}` — exam authority never lives in
    # a client-editable user record (contract #2).
    EXAMS_ENABLED: bool = False
    EXAM_FACULTY_EMAILS: str = ""
    # Platform-level exam administrators (role `super_admin`, docs/adr/0017).
    # Server-side allowlist only: comma-separated e-mails matched against the
    # VERIFIED token e-mail and bound to the first UID that proves it. No
    # address is hard-coded in source; empty means nobody is a super_admin.
    EXAM_SUPER_ADMIN_EMAILS: str = ""
    # Dev-only escape hatch: honour an allowlisted e-mail even when the token
    # says `email_verified=false`. Ignored when ENV=production.
    EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL: bool = False
    EXAM_DEFAULT_WARNING_THRESHOLD: int = 3
    EXAM_DEFAULT_LATE_ADMISSION_GRACE_SECONDS: int = 120
    EXAM_FINALIZATION_BATCH_SIZE: int = 50
    # Prompt 3 (docs/adr/0019). The sweeper is the server-side auto-finalizer the
    # contract requires (expired attempts are sealed, receipted and graded); it
    # is ON by default because without it expired attempts are never resolved.
    # Setting this false is the rollback lever, not a supported steady state.
    EXAM_FINALIZATION_SWEEP_ENABLED: bool = True
    EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS: float = 15.0
    # Idle cap for the sweeper: it sleeps until the earliest known attempt deadline, never longer than this.
    EXAM_FINALIZATION_SWEEP_MAX_INTERVAL_SECONDS: float = 120.0
    # Ended runs are still swept for this long after `ended_at` so a hard stop
    # never strands an unsealed attempt; older runs are not re-read every tick.
    EXAM_FINALIZATION_ENDED_RUN_WINDOW_HOURS: int = 24
    # Off by default: lets the sweeper release a PUBLISHED run at
    # policy.scheduled_start and stop admissions at policy.scheduled_end.
    EXAM_SCHEDULE_AUTOMATION_ENABLED: bool = False
    # Off by default (decision D-8): per-slot negative_marks are rejected while
    # false because grading does not apply them yet.
    EXAM_NEGATIVE_MARKING_ENABLED: bool = False
    EXAM_OUTBOX_LEASE_SECONDS: int = 60
    EXAM_OUTBOX_MAX_ATTEMPTS: int = 5
    # Worker polling: fast while there is work, backing off to the max while idle (each empty poll still costs a
    # Firestore read, and the free plan allows 50,000 a day). In-process work wakes the loops immediately.
    EXAM_OUTBOX_POLL_MIN_SECONDS: float = 2.0
    EXAM_OUTBOX_POLL_MAX_SECONDS: float = 120.0
    EXAM_JUDGE_SWEEP_MIN_SECONDS: float = 2.0
    EXAM_JUDGE_SWEEP_MAX_SECONDS: float = 120.0
    EXAM_PROCTORING_SWEEP_MIN_SECONDS: float = 15.0
    EXAM_PROCTORING_SWEEP_MAX_SECONDS: float = 600.0

    # ── Rollback lever (Prompt 11, docs/adr/0016) ────────────────────────────
    # A targeted alternative to flipping `EXAMS_ENABLED` off: blocks only NEW
    # admissions (`ExamStudentService.admit()`'s NOT_STARTED -> ADMITTED
    # transition) while leaving every already-admitted/in-progress attempt
    # completely unaffected (contract #12: "disable new admissions while
    # preserving safe completion"). Per-run pausing of an already-unsafe run
    # still goes through the existing `RunStatus.PAUSED` faculty command
    # (Prompt 3) — this flag is the *global, every-run* lever for an
    # operational rollback of the exams module itself.
    EXAM_NEW_ADMISSIONS_PAUSED: bool = False

    # ── Institution identity / roster / OTP fallback login (Prompt 2, ───────
    # docs/adr/0010) — off by default via EXAMS_ENABLED; legacy login is
    # completely untouched. EXAM_OTP_HMAC_SECRET should be set to a stable
    # shared secret in any multi-instance/production deployment — unset uses
    # a random per-process secret (dev-only, documented in auth_service.py).
    EXAM_OTP_HMAC_SECRET: Optional[str] = None
    EXAM_OTP_TTL_SECONDS: int = 300
    EXAM_OTP_MAX_ATTEMPTS: int = 5
    # Which OTP delivery adapter to use (Prompt 2, docs/adr/0018). Empty = not
    # chosen: the synthetic dev sink outside production, no delivery (OTP sign-in
    # unavailable) in production. No real provider adapter exists yet — that is
    # an institutional decision (SSO vs verified-e-mail OTP, D-6).
    EXAM_OTP_DELIVERY_PROVIDER: str = ""
    EXAM_STAFF_REAUTH_MAX_AGE_SECONDS: int = 900

    # ── KT ingestion / generation providers (Prompt 5, docs/adr/0009) ────────
    # Unset by default — falls back to the safe-by-default NullMalwareScanner
    # (never "clean") and DeterministicFakeProvider (test/dev only, no live
    # model). Configuring these is an institutional decision, not a code change.
    EXAM_MALWARE_SCANNER_ENDPOINT: Optional[str] = None
    # Prompt 4 (docs/adr/0020): `host:port` of a clamd daemon enables the ClamdScanner
    # adapter; unset/invalid keeps the fail-closed default. Uploaded bytes need private
    # storage: EXAM_EVIDENCE_STORAGE_BUCKET (same bucket, `kt-quarantine/` prefix) —
    # in production uploads are refused (503) while it is unset.
    EXAM_KT_MAX_ACTIVE_UPLOADS_PER_USER: int = 200
    EXAM_KT_PARSE_MEMORY_MB: int = 1024
    # Comma-separated host suffixes a URL source may come from; empty = any public host.
    EXAM_KT_URL_ALLOWED_HOSTS: str = ""
    EXAM_GENERATION_PROVIDER_BASE_URL: Optional[str] = None
    EXAM_GENERATION_PROVIDER_API_KEY: Optional[str] = None
    # Prompt 5 (docs/adr/0021): neutral JSON-over-HTTPS transport limits for the generation and review
    # adapters, and the default-off gate that keeps coding items unapprovable until the judge has run them.
    EXAM_GENERATION_HTTP_TIMEOUT_SECONDS: float = 60.0
    EXAM_GENERATION_HTTP_MAX_RETRIES: int = 2
    EXAM_GENERATION_MAX_ATTEMPTS: int = 3
    EXAM_REQUIRE_EXECUTED_CODING_VERIFICATION: bool = False
    # Prompt 6 (ADR 0022): let a student re-open questions they were already shown (default off: current slot only).
    EXAM_STUDENT_REVISIT_RELEASED_SLOTS: bool = False
    EXAM_DRAFT_MAX_BYTES: int = 100_000
    # Prompt 7 (ADR 0023): short share links for runs (default off).
    EXAM_SHORT_LINKS_ENABLED: bool = False

    # ── Independent item review (Backend model prompt B, docs/adr/0009) ─────
    # A separate provider/endpoint from generation above — may be the same
    # model or a different one, an institutional choice. Unset by default —
    # falls back to DeterministicFakeReviewProvider (test/dev only).
    EXAM_REVIEW_PROVIDER_BASE_URL: Optional[str] = None
    EXAM_REVIEW_PROVIDER_API_KEY: Optional[str] = None

    # ── Learning feedback provider (Backend model prompt C, docs/adr/0014) ───
    # Unset by default — feedback stays on the permanent, deterministic
    # TemplateFeedbackProvider (ADR 0014: "no live LLM call"). Configuring
    # this is an institution's own act of opting into a real provider once
    # "locally evaluated" (master prompt contract); any failure from it
    # still falls back to the template rather than breaking the page.
    EXAM_FEEDBACK_PROVIDER_BASE_URL: Optional[str] = None
    EXAM_FEEDBACK_PROVIDER_API_KEY: Optional[str] = None

    # ── Coding judge (Prompt 6, docs/adr/0011) ───────────────────────────────
    # Unset by default — falls back to a deterministic fake judge (test/dev
    # only, never executes submitted code). A real Judge0-compatible instance
    # requires separately provisioned hardened isolated compute (see ADR) —
    # this config alone does not create or validate that environment.
    EXAM_JUDGE_PROVIDER_BASE_URL: Optional[str] = None
    EXAM_JUDGE_PROVIDER_API_KEY: Optional[str] = None
    # Prompt 8 (ADR 0024). With a real provider configured, only the language keys listed here (comma-separated) are usable.
    # Add a key only after checking its judge0 id against the provider's /languages; empty = no language is usable.
    EXAM_JUDGE_VALIDATED_LANGUAGES: str = ""
    # Failed provider calls before a final-grade submission stops retrying and waits for an operator (never scored).
    EXAM_JUDGE_MAX_INFRA_ATTEMPTS: int = 5
    # A final-grade submission still unfinished after this long is handed to an operator instead of polling forever.
    EXAM_JUDGE_STALL_SECONDS: int = 900
    # Student-initiated test runs are refused (503) while this many submissions are already waiting on the judge.
    EXAM_JUDGE_MAX_IN_FLIGHT: int = 200

    # ── Live control (Prompt 7, docs/adr/0012) — WebSocket admission tickets.
    # Short TTL by design: a client mints a fresh ticket for every connect/
    # reconnect attempt rather than reusing one (single-use, contract:
    # "authenticated WebSocket ticket admission").
    EXAM_LIVE_TICKET_TTL_SECONDS: int = 60

    # ── Proctoring evidence (Prompt 8, docs/adr/0013) ────────────────────────
    # grace: a departure shorter than this never counts (e.g. a quick,
    # accidental blur). cooldown: a new departure starting within this long
    # after the previous one closed is treated as a CONTINUATION of the same
    # incident, not a new one (contract: "count one continuous departure
    # once"). The actual block THRESHOLD is per-run policy
    # (ExamPolicy.warning_threshold), never a global setting.
    EXAM_PROCTORING_GRACE_SECONDS: float = 2.0
    EXAM_PROCTORING_COOLDOWN_SECONDS: float = 5.0
    EXAM_PROCTORING_STALE_INCIDENT_SECONDS: float = 120.0
    EXAM_EVIDENCE_UPLOAD_TICKET_TTL_SECONDS: int = 120
    EXAM_EVIDENCE_VIEW_TICKET_TTL_SECONDS: int = 60
    EXAM_EVIDENCE_RETENTION_DAYS: int = 90
    # Unset by default — falls back to InMemoryEvidenceStorage (dev/test
    # only; content does not survive a process restart). A real deployment
    # sets this to a provisioned Firebase Storage bucket name.
    EXAM_EVIDENCE_STORAGE_BUCKET: Optional[str] = None

    # ── Gradebook / similarity / export (Prompt 9, docs/adr/0014) ───────────
    # Export downloads reuse EXAM_EVIDENCE_STORAGE_BUCKET (same bucket, a
    # different key prefix) — no separate bucket setting. Unset JPlag path
    # falls back to a deterministic fake similarity provider (test/dev only,
    # never performs real comparison). A real JPlag CLI jar requires a
    # separately provisioned Java runtime — this config alone does not
    # create or validate that environment (see ADR for requirements).
    EXAM_EXPORT_DOWNLOAD_TICKET_TTL_SECONDS: int = 300
    # How long a generated gradebook export stays downloadable before its content is deleted (job record kept).
    EXAM_EXPORT_RETENTION_HOURS: int = 24
    EXAM_JPLAG_JAR_PATH: Optional[str] = None
    EXAM_JPLAG_JAVA_EXECUTABLE: str = "java"

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        value = origin.strip()
        if not value:
            return ""
        if value == "*":
            return value
        return value.rstrip("/")

    def allowed_origins_list(self) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for origin in self.ALLOWED_ORIGINS.split(","):
            value = self._normalize_origin(origin)
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def placement_cell_emails_set(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.PLACEMENT_CELL_EMAILS.split(",")
            if email.strip()
        }

    def exam_kt_url_allowed_hosts(self) -> list[str]:
        return [h.strip().lower() for h in self.EXAM_KT_URL_ALLOWED_HOSTS.split(",") if h.strip()]

    def exam_faculty_emails_set(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.EXAM_FACULTY_EMAILS.split(",")
            if email.strip()
        }

    def exam_super_admin_emails_set(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.EXAM_SUPER_ADMIN_EMAILS.split(",")
            if email.strip()
        }

    def unverified_staff_email_allowed(self) -> bool:
        return bool(self.EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL) and self.ENV.strip().lower() != "production"

    def search_warmup_queries_list(self) -> list[str]:
        return [
            query.strip()
            for query in self.SEARCH_WARMUP_QUERIES.split(",")
            if query.strip()
        ]

    def _resolve_path(self, value: Optional[str], fallback: Path) -> Path:
        if not value:
            return fallback
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def faiss_dir_path(self) -> Path:
        fallback = BASE_DIR / "data" / "faiss"
        return self._resolve_path(self.FAISS_DIR, fallback)

    @property
    def faiss_index_path(self) -> Path:
        fallback = self.faiss_dir_path / "index.faiss"
        return self._resolve_path(self.FAISS_INDEX_PATH, fallback)

    @property
    def faiss_mapping_path(self) -> Path:
        fallback = self.faiss_dir_path / "mapping.json"
        return self._resolve_path(self.FAISS_MAPPING_PATH, fallback)


settings = Settings()
