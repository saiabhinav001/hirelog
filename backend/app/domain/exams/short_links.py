"""Short share links for exam runs (Prompt 7, ADR 0023).

A short code is only a LOCATOR: it names a run so a student can type or tap a short URL. It grants
nothing - the resolver still requires a signed-in, enrolled student, and admission, content and
submission keep their own checks. Codes are random (not sequential), collision-checked at creation,
revocable, and at most one is active per run.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Callable, Literal, Optional

from firebase_admin import firestore
from pydantic import BaseModel

from app.domain.authz.staff_binding import AuthzAuditRepository, build_audit_event
from app.domain.exams.clock import Clock, system_clock

logger = logging.getLogger("hirelog.exams.short_links")

SHORT_LINKS_COLLECTION = "exam_short_links"
# Crockford base32: no I, L, O, U, so a code read aloud or from a screen is hard to mistype.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
MAX_COLLISION_RETRIES = 5
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1"})


class ShortLinkError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ShortCodeCollision(Exception):
    """The generated code is already taken; the caller draws another."""


class ShortLink(BaseModel):
    code: str
    run_id: str
    status: Literal["active", "revoked"] = "active"
    created_by: str
    created_at: datetime
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None
    replaced_by: Optional[str] = None


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> Optional[str]:
    """Accepts `abcd-efgh`, spaces, lower case and the usual look-alike letters; None when it cannot be a code."""
    cleaned = "".join(ch for ch in raw.strip().upper() if ch not in "- ").translate(_CONFUSABLE)
    if len(cleaned) != CODE_LENGTH or any(ch not in ALPHABET for ch in cleaned):
        return None
    return cleaned


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


class ShortLinkRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, code: str):
        return self._db.collection(SHORT_LINKS_COLLECTION).document(code)

    def get(self, code: str) -> Optional[ShortLink]:
        snap = self._ref(code).get()
        return ShortLink.model_validate(snap.to_dict() or {}) if snap.exists else None

    def exists_in_transaction(self, transaction, code: str) -> bool:
        return self._ref(code).get(transaction=transaction).exists

    def _active_query(self, run_id: str):
        return self._db.collection(SHORT_LINKS_COLLECTION).where(
            filter=firestore.FieldFilter("run_id", "==", run_id)
        ).where(filter=firestore.FieldFilter("status", "==", "active"))

    def active_for_run(self, run_id: str) -> list[ShortLink]:
        return [ShortLink.model_validate(d.to_dict() or {}) for d in self._active_query(run_id).stream()]

    def active_for_run_in_transaction(self, transaction, run_id: str) -> list[ShortLink]:
        return [ShortLink.model_validate(d.to_dict() or {}) for d in self._active_query(run_id).stream(transaction=transaction)]

    def create_in_transaction(self, transaction, link: ShortLink) -> None:
        transaction.create(self._ref(link.code), link.model_dump(mode="json"))

    def update_in_transaction(self, transaction, code: str, update: dict) -> None:
        transaction.update(self._ref(code), update)


class ShortLinkService:
    def __init__(self, db, clock: Clock = system_clock, code_factory: Callable[[], str] = new_code) -> None:
        self._db = db
        self._clock = clock
        self._code_factory = code_factory
        self.links = ShortLinkRepository(db)
        self._audit = AuthzAuditRepository(db)

    def current(self, run_id: str) -> Optional[ShortLink]:
        active = self.links.active_for_run(run_id)
        return max(active, key=lambda link: link.created_at) if active else None

    def resolve(self, raw_code: str) -> Optional[ShortLink]:
        """The active link for a code, or None - unknown, malformed and revoked codes are indistinguishable."""
        code = normalize_code(raw_code)
        link = self.links.get(code) if code else None
        return link if link is not None and link.status == "active" else None

    def issue(self, run_id: str, *, actor_uid: str, institution_id: Optional[str] = None, regenerate: bool = False) -> tuple[ShortLink, bool]:
        """Returns (link, created). Without `regenerate` an existing active link is returned unchanged;
        with it the old link is revoked and a new code issued in the same transaction."""
        for _ in range(MAX_COLLISION_RETRIES):
            try:
                return self._issue_once(run_id, actor_uid, institution_id, regenerate)
            except ShortCodeCollision:
                logger.warning("short code collision for run %s; drawing another", run_id)
        raise ShortLinkError("code_space_exhausted", "Could not find a free short code; try again.")

    def _issue_once(self, run_id: str, actor_uid: str, institution_id: Optional[str], regenerate: bool) -> tuple[ShortLink, bool]:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> tuple[ShortLink, bool]:
            now = self._clock.now()
            active = self.links.active_for_run_in_transaction(transaction, run_id)
            if active and not regenerate:
                return max(active, key=lambda link: link.created_at), False
            code = self._code_factory()
            if self.links.exists_in_transaction(transaction, code):
                raise ShortCodeCollision(code)
            for old in active:
                self.links.update_in_transaction(
                    transaction, old.code,
                    {"status": "revoked", "revoked_at": now.isoformat(), "revoked_by": actor_uid, "replaced_by": code},
                )
            link = ShortLink(code=code, run_id=run_id, created_by=actor_uid, created_at=now)
            self.links.create_in_transaction(transaction, link)
            self._audit.append_in_transaction(transaction, build_audit_event(
                "exam_short_link_issued", actor_uid=actor_uid, institution_id=institution_id,
                detail={"run_id": run_id, "replaced": [old.code for old in active]}, now=now,
            ))
            return link, True

        return _txn(transaction)

    def revoke(self, run_id: str, *, actor_uid: str, institution_id: Optional[str] = None) -> int:
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(transaction) -> int:
            now = self._clock.now()
            active = self.links.active_for_run_in_transaction(transaction, run_id)
            for old in active:
                self.links.update_in_transaction(
                    transaction, old.code, {"status": "revoked", "revoked_at": now.isoformat(), "revoked_by": actor_uid}
                )
            if active:
                self._audit.append_in_transaction(transaction, build_audit_event(
                    "exam_short_link_revoked", actor_uid=actor_uid, institution_id=institution_id,
                    detail={"run_id": run_id, "codes": [old.code for old in active]}, now=now,
                ))
            return len(active)

        return _txn(transaction)
