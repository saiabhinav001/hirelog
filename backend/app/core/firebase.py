from __future__ import annotations

import json
import logging
from pathlib import Path

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials, firestore

from app.core.config import BASE_DIR, settings

logger = logging.getLogger(__name__)


def _build_credential() -> credentials.Certificate:
    """Build Firebase credential from JSON env var (production) or file path (local dev)."""
    # 1. Production: service account JSON string stored in env var
    if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        logger.info("Loading Firebase credentials from FIREBASE_SERVICE_ACCOUNT_JSON env var")
        service_info = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
        return credentials.Certificate(service_info)

    # 2. Local dev: file path to service account key
    if settings.FIREBASE_SERVICE_ACCOUNT_PATH:
        service_path = Path(settings.FIREBASE_SERVICE_ACCOUNT_PATH)
        if not service_path.is_absolute():
            service_path = (BASE_DIR / service_path).resolve()
        if not service_path.exists():
            raise FileNotFoundError(f"Service account file not found: {service_path}")
        logger.info("Loading Firebase credentials from file: %s", service_path)
        return credentials.Certificate(str(service_path))

    raise ValueError(
        "Firebase credentials not configured. "
        "Set FIREBASE_SERVICE_ACCOUNT_JSON (production) or FIREBASE_SERVICE_ACCOUNT_PATH (local dev)."
    )


def initialize_firebase() -> firestore.Client:
    if not firebase_admin._apps:
        cred = _build_credential()
        firebase_admin.initialize_app(cred, {"projectId": settings.FIREBASE_PROJECT_ID})
    return firestore.client()


db = initialize_firebase()


__all__ = ["db", "firebase_auth"]
