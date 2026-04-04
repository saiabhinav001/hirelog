from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_ignore_empty=True)

    API_TITLE: str = "HireLog API"
    ENV: str = "development"

    # Firebase — production uses JSON env var; local dev uses file path
    FIREBASE_SERVICE_ACCOUNT_JSON: Optional[str] = None
    FIREBASE_SERVICE_ACCOUNT_PATH: Optional[str] = None
    FIREBASE_PROJECT_ID: str

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

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    def placement_cell_emails_set(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.PLACEMENT_CELL_EMAILS.split(",")
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
