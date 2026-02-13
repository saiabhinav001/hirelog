from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_ignore_empty=True)

    API_TITLE: str = "Placement Archive API"
    ENV: str = "development"

    FIREBASE_SERVICE_ACCOUNT_PATH: str
    FIREBASE_PROJECT_ID: str

    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://localhost:3002"

    FAISS_INDEX_PATH: str = "data/faiss/index.faiss"
    FAISS_MAPPING_PATH: str = "data/faiss/mapping.json"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIM: int = 384

    MAX_SEARCH_RESULTS: int = 20
    DASHBOARD_SAMPLE_LIMIT: int = 500

    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]


settings = Settings()
