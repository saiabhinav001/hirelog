"""Staff bootstrap binding and authorization audit (Prompt 1, ADR 0017).

An allowlisted, VERIFIED e-mail is bound to the first Firebase UID that proves
it. Later requests from a DIFFERENT uid claiming the same e-mail are denied
(and audited) until an operator resolves the conflict, so authority follows a
stable identity instead of an e-mail string. The binding document id is the
SHA-256 of the normalized e-mail, which makes "first writer wins" an atomic
`create()` rather than a read-then-write race.

Collections are private (Admin SDK only; covered by the default-deny rule).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Literal, Optional, Sequence
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.read_cache import cache_for
from app.domain.institution.normalization import normalize_email

logger = logging.getLogger("hirelog.exams.authz")

STAFF_BINDINGS_COLLECTION = "exam_staff_bindings"
AUTHZ_AUDIT_COLLECTION = "exam_authz_audit"
STAFF_BINDING_SCHEMA_VERSION = 1

BindOutcome = Literal["bound", "already_bound", "conflict"]

_CONFLICT_AUDIT_THROTTLE_SECONDS = 600.0
_conflict_audit_seen: dict[tuple[str, str], float] = {}
_conflict_audit_lock = threading.Lock()


class StaffBinding(BaseModel):
    schema_version: int = STAFF_BINDING_SCHEMA_VERSION
    email: str
    uid: str
    roles_at_bind: list[str] = Field(default_factory=list)
    sign_in_provider: Optional[str] = None
    bound_at: datetime


class AuthzAuditEvent(BaseModel):
    """Append-only; never edited or deleted by application code. Shared by the
    staff binding (Prompt 1) and institution identity administration (Prompt 2):
    `kind` is staff_bound, staff_binding_conflict, identity_*, membership_*,
    teacher_*, instructor_*, roster_*."""

    id: str
    kind: str
    uid: str = ""  # the subject uid, when there is one
    email: str = ""
    actor_uid: Optional[str] = None  # who performed an administrative action
    institution_id: Optional[str] = None
    detail: dict = Field(default_factory=dict)
    created_at: datetime


def build_audit_event(
    kind: str,
    *,
    uid: str = "",
    email: str = "",
    actor_uid: Optional[str] = None,
    institution_id: Optional[str] = None,
    detail: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> AuthzAuditEvent:
    return AuthzAuditEvent(
        id=uuid4().hex, kind=kind, uid=uid, email=email, actor_uid=actor_uid, institution_id=institution_id,
        detail=detail or {}, created_at=now or datetime.now(timezone.utc),
    )


def binding_doc_id(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


class StaffBindingRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, email: str):
        return self._db.collection(STAFF_BINDINGS_COLLECTION).document(binding_doc_id(email))

    def get(self, email: str) -> Optional[StaffBinding]:
        snap = self._ref(email).get()
        if not snap.exists:
            return None
        return StaffBinding.model_validate(snap.to_dict() or {})

    def create_if_absent(self, binding: StaffBinding) -> bool:
        """Atomic first-writer-wins; False when a binding already exists."""
        try:
            self._ref(binding.email).create(binding.model_dump(mode="json"))
        except AlreadyExists:
            return False
        return True


class AuthzAuditRepository:
    def __init__(self, db) -> None:
        self._db = db

    def append(self, event: AuthzAuditEvent) -> None:
        self._db.collection(AUTHZ_AUDIT_COLLECTION).document(event.id).set(event.model_dump(mode="json"))

    def append_in_transaction(self, transaction, event: AuthzAuditEvent) -> None:
        """Lets a state change and its audit record commit or fail together."""
        transaction.set(
            self._db.collection(AUTHZ_AUDIT_COLLECTION).document(event.id), event.model_dump(mode="json")
        )


class StaffBindingService:
    def __init__(self, db, *, now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self._db = db
        self._bindings = StaffBindingRepository(db)
        self._audit = AuthzAuditRepository(db)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def _ok_cache(self):
        return cache_for(self._db, "staff_binding_ok", ttl_seconds=settings.EXAM_STAFF_BINDING_CACHE_SECONDS, max_entries=256)

    def ensure_bound(
        self, *, email: str, uid: str, roles: Sequence[str], sign_in_provider: Optional[str] = None
    ) -> BindOutcome:
        """A confirmed binding is remembered for a minute (every staff request asks); a conflict is never cached."""
        normalized = normalize_email(email)
        key = (normalized, uid)
        if self._ok_cache().peek(key):
            return "already_bound"
        outcome = self._ensure_bound_uncached(normalized, uid, roles, sign_in_provider)
        if outcome != "conflict":
            self._ok_cache().put(key, True)
        return outcome

    def _ensure_bound_uncached(
        self, normalized: str, uid: str, roles: Sequence[str], sign_in_provider: Optional[str]
    ) -> BindOutcome:
        existing = self._bindings.get(normalized)
        if existing is not None:
            return self._outcome_for_existing(existing, normalized, uid)

        binding = StaffBinding(
            email=normalized, uid=uid, roles_at_bind=sorted(roles),
            sign_in_provider=sign_in_provider, bound_at=self._now(),
        )
        if not self._bindings.create_if_absent(binding):
            existing = self._bindings.get(normalized)
            if existing is None:  # deleted between the two calls — treat as unbound, let the next request retry
                return "conflict"
            return self._outcome_for_existing(existing, normalized, uid)

        self._record("staff_bound", uid=uid, email=normalized, detail={
            "roles": sorted(roles), "sign_in_provider": sign_in_provider,
        })
        logger.info("exam staff identity bound: uid=%s roles=%s", uid, sorted(roles))
        return "bound"

    def _outcome_for_existing(self, existing: StaffBinding, email: str, uid: str) -> BindOutcome:
        if existing.uid == uid:
            return "already_bound"
        self._record_conflict_once(email=email, uid=uid, bound_uid=existing.uid)
        return "conflict"

    def _record_conflict_once(self, *, email: str, uid: str, bound_uid: str) -> None:
        key = (email, uid)
        now = time.monotonic()
        with _conflict_audit_lock:
            last = _conflict_audit_seen.get(key)
            if last is not None and now - last < _CONFLICT_AUDIT_THROTTLE_SECONDS:
                return
            _conflict_audit_seen[key] = now
        logger.warning("exam staff binding conflict: email bound to uid=%s, presented by uid=%s", bound_uid, uid)
        self._record("staff_binding_conflict", uid=uid, email=email, detail={"bound_uid": bound_uid})

    def _record(self, kind: str, *, uid: str, email: str, detail: dict) -> None:
        self._audit.append(
            AuthzAuditEvent(id=uuid4().hex, kind=kind, uid=uid, email=email, detail=detail, created_at=self._now())
        )
