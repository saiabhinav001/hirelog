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
    EXAM_DEFAULT_WARNING_THRESHOLD: int = 3
    EXAM_DEFAULT_LATE_ADMISSION_GRACE_SECONDS: int = 120
    EXAM_FINALIZATION_BATCH_SIZE: int = 50
    EXAM_OUTBOX_LEASE_SECONDS: int = 60
    EXAM_OUTBOX_MAX_ATTEMPTS: int = 5

    # ── Institution identity / roster / OTP fallback login (Prompt 2, ───────
    # docs/adr/0010) — off by default via EXAMS_ENABLED; legacy login is
    # completely untouched. EXAM_OTP_HMAC_SECRET should be set to a stable
    # shared secret in any multi-instance/production deployment — unset uses
    # a random per-process secret (dev-only, documented in auth_service.py).
    EXAM_OTP_HMAC_SECRET: Optional[str] = None
    EXAM_OTP_TTL_SECONDS: int = 300
    EXAM_OTP_MAX_ATTEMPTS: int = 5
    EXAM_STAFF_REAUTH_MAX_AGE_SECONDS: int = 900

    # ── KT ingestion / generation providers (Prompt 5, docs/adr/0009) ────────
    # Unset by default — falls back to the safe-by-default NullMalwareScanner
    # (never "clean") and DeterministicFakeProvider (test/dev only, no live
    # model). Configuring these is an institutional decision, not a code change.
    EXAM_MALWARE_SCANNER_ENDPOINT: Optional[str] = None
    EXAM_GENERATION_PROVIDER_BASE_URL: Optional[str] = None
    EXAM_GENERATION_PROVIDER_API_KEY: Optional[str] = None

    # ── Coding judge (Prompt 6, docs/adr/0011) ───────────────────────────────
    # Unset by default — falls back to a deterministic fake judge (test/dev
    # only, never executes submitted code). A real Judge0-compatible instance
    # requires separately provisioned hardened isolated compute (see ADR) —
    # this config alone does not create or validate that environment.
    EXAM_JUDGE_PROVIDER_BASE_URL: Optional[str] = None
    EXAM_JUDGE_PROVIDER_API_KEY: Optional[str] = None

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

    def exam_faculty_emails_set(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.EXAM_FACULTY_EMAILS.split(",")
            if email.strip()
        }

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
