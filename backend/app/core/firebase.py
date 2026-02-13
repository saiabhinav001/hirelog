from __future__ import annotations

from pathlib import Path

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials, firestore

from app.core.config import BASE_DIR, settings


def initialize_firebase() -> firestore.Client:
    if not firebase_admin._apps:
        service_path = Path(settings.FIREBASE_SERVICE_ACCOUNT_PATH)
        if not service_path.is_absolute():
            service_path = (BASE_DIR / service_path).resolve()
        cred = credentials.Certificate(str(service_path))
        firebase_admin.initialize_app(cred, {"projectId": settings.FIREBASE_PROJECT_ID})
    return firestore.client()


db = initialize_firebase()


__all__ = ["db", "firebase_auth"]
