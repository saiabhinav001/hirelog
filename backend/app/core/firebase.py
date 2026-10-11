from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials, firestore

from app.core.config import BASE_DIR, settings

logger = logging.getLogger(__name__)
_firebase_init_lock = threading.Lock()

# Set gRPC keep-alive environment variables BEFORE any gRPC channel is created.
# This keeps Firestore connections warm and avoids cold-connect latency on
# subsequent requests (~50-100ms saved per reconnect).
os.environ.setdefault("GRPC_KEEPALIVE_TIME_MS", "30000")        # ping every 30s
os.environ.setdefault("GRPC_KEEPALIVE_TIMEOUT_MS", "10000")      # wait 10s for pong
os.environ.setdefault("GRPC_KEEPALIVE_PERMIT_WITHOUT_CALLS", "1")  # ping even when idle


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
        with _firebase_init_lock:
            if not firebase_admin._apps:
                cred = _build_credential()
                options = {"projectId": settings.FIREBASE_PROJECT_ID} if settings.FIREBASE_PROJECT_ID else None
                if options:
                    firebase_admin.initialize_app(cred, options)
                else:
                    firebase_admin.initialize_app(cred)
    return firestore.client()


_db = None


def get_db() -> firestore.Client:
    """Lazy Firestore client — initializes on first call."""
    global _db
    if _db is None:
        _db = initialize_firebase()
    return _db


class _LazyDB:
    """Proxy that defers Firebase init until first attribute access."""
    def __getattr__(self, name):
        return getattr(get_db(), name)


db = _LazyDB()

_EXAM_APP_NAME = "exams"
_exam_db = None


def _build_exam_credential() -> credentials.Certificate:
    """Credential for the separate exam project; the main credential is reused only against the local emulator."""
    if settings.EXAM_FIREBASE_SERVICE_ACCOUNT_JSON:
        logger.info("Loading exam Firebase credentials from EXAM_FIREBASE_SERVICE_ACCOUNT_JSON env var")
        return credentials.Certificate(json.loads(settings.EXAM_FIREBASE_SERVICE_ACCOUNT_JSON))

    if settings.EXAM_FIREBASE_SERVICE_ACCOUNT_PATH:
        service_path = Path(settings.EXAM_FIREBASE_SERVICE_ACCOUNT_PATH)
        if not service_path.is_absolute():
            service_path = (BASE_DIR / service_path).resolve()
        if not service_path.exists():
            raise FileNotFoundError(f"Exam service account file not found: {service_path}")
        logger.info("Loading exam Firebase credentials from file: %s", service_path)
        return credentials.Certificate(str(service_path))

    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        return _build_credential()

    raise ValueError(
        "EXAM_FIREBASE_PROJECT_ID is set but no exam credentials were provided. "
        "Set EXAM_FIREBASE_SERVICE_ACCOUNT_JSON (production) or EXAM_FIREBASE_SERVICE_ACCOUNT_PATH (local dev)."
    )


def get_exam_db() -> firestore.Client:
    """Firestore client for exam data: the separate exam project when configured, otherwise the shared client."""
    global _exam_db
    if not settings.exam_data_is_separate():
        return get_db()
    if _exam_db is None:
        get_db()  # the main app (Auth and legacy data) must exist first; it takes the same lock
        with _firebase_init_lock:
            if _exam_db is None:
                try:
                    app = firebase_admin.get_app(_EXAM_APP_NAME)
                except ValueError:
                    app = firebase_admin.initialize_app(
                        _build_exam_credential(), {"projectId": settings.EXAM_FIREBASE_PROJECT_ID}, name=_EXAM_APP_NAME
                    )
                _exam_db = firestore.client(app=app)
                logger.info("Exam data uses its own Firebase project %r", settings.EXAM_FIREBASE_PROJECT_ID)
    return _exam_db


class _LazyExamDB:
    """Proxy that resolves the exam Firestore client on first attribute access."""
    def __getattr__(self, name):
        return getattr(get_exam_db(), name)


exam_db = _LazyExamDB()


__all__ = ["db", "get_db", "exam_db", "get_exam_db", "firebase_auth"]
