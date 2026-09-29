from __future__ import annotations

import hashlib
import threading
import time

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import firestore

from app.core.config import settings
from app.core.firebase import db, firebase_auth


security = HTTPBearer()

# ── In-memory auth token cache ───────────────────────────────────────────────
# Avoids repeated verify_id_token() + Firestore user doc read on every request.
_auth_cache: dict[str, tuple[float, int, dict]] = {}
_AUTH_CACHE_TTL = 300  # 5 minutes (Firebase tokens live ~60 min)
_AUTH_CACHE_MAX = 200
_auth_cache_lock = threading.Lock()

_ROLE_PRIORITY = {
    "viewer": 0,
    "contributor": 1,
    "placement_cell": 2,
}


def _higher_role(left: str, right: str) -> str:
    if _ROLE_PRIORITY.get(right, 0) > _ROLE_PRIORITY.get(left, 0):
        return right
    return left


def _placement_cell_email(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in settings.placement_cell_emails_set()


def _resolve_role(existing_role: str, email: str | None) -> str:
    target = existing_role or "viewer"
    if _placement_cell_email(email):
        target = _higher_role(target, "placement_cell")
    return target


def _get_or_create_user(uid: str, email: str | None, name: str | None = None) -> dict:
    doc_ref = db.collection("users").document(uid)
    snapshot = doc_ref.get()
    if snapshot.exists:
        data = snapshot.to_dict() or {}
        current_role = str(data.get("role") or "viewer")
        resolved_role = _resolve_role(current_role, email)
        if resolved_role != current_role:
            doc_ref.set({"role": resolved_role}, merge=True)
            data["role"] = resolved_role
        data["uid"] = uid
        return data

    initial_role = _resolve_role("viewer", email)
    user_data = {
        "uid": uid,
        "name": name or "",
        "email": email or "",
        "role": initial_role,
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    doc_ref.set(user_data)
    return user_data


def _verify_firebase_token(token: str) -> dict:
    """Pure Firebase ID token verification — no Firestore read, no caching.

    Kept separate from role/authorization resolution so authorization-sensitive
    call sites (see `get_current_user_uncached`) can verify identity without
    also inheriting the legacy 5-minute role cache below.
    """
    try:
        return firebase_auth.verify_id_token(token)
    except Exception as exc:  # pragma: no cover - Firebase SDK provides rich error info
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
        ) from exc


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Legacy dependency: verifies the token and resolves the user, caching the
    result (incl. role) for up to `_AUTH_CACHE_TTL` (5 minutes).

    This cache trades a small staleness window for read cost on legacy,
    lower-stakes routes. Do NOT reuse it as-is for new high-stakes
    authorization decisions (e.g. exam faculty/admission gates) — use
    `get_current_user_uncached` there instead, per docs/adr/0007.
    """
    token = credentials.credentials
    now = int(time.time())

    # Check in-memory cache first (keyed by token hash)
    cache_key = hashlib.sha256(token.encode()).hexdigest()[:16]
    with _auth_cache_lock:
        entry = _auth_cache.get(cache_key)
        if entry:
            cached_at, token_exp, cached_user = entry
            if (now - cached_at) < _AUTH_CACHE_TTL and now < token_exp:
                return cached_user.copy()
            _auth_cache.pop(cache_key, None)

    decoded = _verify_firebase_token(token)

    uid = decoded.get("uid")
    email = decoded.get("email")
    name = decoded.get("name") or decoded.get("user_id")
    token_exp = int(decoded.get("exp") or (now + _AUTH_CACHE_TTL))
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
        _auth_cache[cache_key] = (now, token_exp, user_data.copy())

    return user_data


def get_current_user_uncached(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Always re-verifies the token and re-reads the Firestore role fresh.

    Intended for new authorization-sensitive routes (exam faculty/admission
    checks and similar) that must not act on a role that could be up to 5
    minutes stale — e.g. immediately after a revocation or role change.
    """
    decoded = _verify_firebase_token(credentials.credentials)
    uid = decoded.get("uid")
    email = decoded.get("email")
    name = decoded.get("name") or decoded.get("user_id")
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload.")
    user_data = _get_or_create_user(uid, email, name)
    user_data.setdefault("email", email or "")
    user_data.setdefault("name", name or "")
    return user_data


def require_contributor(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in {"contributor", "placement_cell"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Contributor role required to submit experiences.",
        )
    return user


def require_placement_cell(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "placement_cell":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Placement cell role required.",
        )
    return user
