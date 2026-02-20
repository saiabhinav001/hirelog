from __future__ import annotations

import hashlib
import threading
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import firestore

from app.core.firebase import db, firebase_auth


security = HTTPBearer()

# ── In-memory auth token cache ───────────────────────────────────────────────
# Avoids repeated verify_id_token() + Firestore user doc read on every request.
_auth_cache: dict[str, tuple[float, dict]] = {}
_AUTH_CACHE_TTL = 300  # 5 minutes (Firebase tokens live ~60 min)
_AUTH_CACHE_MAX = 200
_auth_cache_lock = threading.Lock()


def _get_or_create_user(uid: str, email: str | None, name: str | None = None) -> dict:
    doc_ref = db.collection("users").document(uid)
    snapshot = doc_ref.get()
    if snapshot.exists:
        data = snapshot.to_dict() or {}
        data["uid"] = uid
        return data

    user_data = {
        "uid": uid,
        "name": name or "",
        "email": email or "",
        "role": "viewer",
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    doc_ref.set(user_data)
    return user_data


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token = credentials.credentials

    # Check in-memory cache first (keyed by token hash)
    cache_key = hashlib.sha256(token.encode()).hexdigest()[:16]
    with _auth_cache_lock:
        entry = _auth_cache.get(cache_key)
        if entry and time.time() - entry[0] < _AUTH_CACHE_TTL:
            return entry[1].copy()

    try:
        decoded = firebase_auth.verify_id_token(token)
    except Exception as exc:  # pragma: no cover - Firebase SDK provides rich error info
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
        ) from exc

    uid = decoded.get("uid")
    email = decoded.get("email")
    name = decoded.get("name") or decoded.get("user_id")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload.")
    user_data = _get_or_create_user(uid, email, name)
    user_data.setdefault("email", email or "")
    user_data.setdefault("name", name or "")

    # Store in cache
    with _auth_cache_lock:
        if len(_auth_cache) >= _AUTH_CACHE_MAX:
            oldest = min(_auth_cache, key=lambda k: _auth_cache[k][0])
            del _auth_cache[oldest]
        _auth_cache[cache_key] = (time.time(), user_data.copy())

    return user_data


def require_contributor(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "contributor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Contributor role required to submit experiences.",
        )
    return user
