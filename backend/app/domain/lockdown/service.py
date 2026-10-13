"""Managed-lockdown admission service — thin imperative shell over the pure
`verify_seb_admission` decision. Every admission check (pass or fail) is
audited; never a silent decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.core.config import settings
from app.core.read_cache import cache_for
from app.domain.lockdown.decisions import verify_seb_admission
from app.domain.lockdown.errors import LockdownAdmissionDeniedError
from app.domain.lockdown.models import LockdownAdmissionLogEntry, LockdownAdmissionOutcome, LockdownProfile
from app.domain.lockdown.repository import LockdownAdmissionLogRepository, LockdownProfileRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LockdownService:
    def __init__(self, db) -> None:
        self._db = db
        self.profiles = LockdownProfileRepository(db)
        self.admission_log = LockdownAdmissionLogRepository(db)

    def _profile_cache(self):
        db = getattr(self, "_db", None)
        if db is None:
            return None
        return cache_for(db, "lockdown_profiles", ttl_seconds=settings.EXAM_LOCKDOWN_CACHE_SECONDS, max_entries=256)

    def get_or_default_profile(self, run_id: str) -> LockdownProfile:
        """Read by every admission check, so it is cached (also the default for runs without a profile)."""

        def load() -> LockdownProfile:
            return self.profiles.get(run_id) or LockdownProfile(run_id=run_id)

        cache = self._profile_cache()
        profile = load() if cache is None else cache.get_or_load(run_id, load)
        return profile.model_copy(deep=True)

    def configure_profile(
        self,
        *,
        run_id: str,
        enabled: bool,
        expected_config_key_hash: Optional[str],
        expected_browser_exam_key_hashes: list[str],
        piloted_platforms: list[str],
        updated_by: str,
    ) -> LockdownProfile:
        profile = LockdownProfile(
            run_id=run_id,
            enabled=enabled,
            expected_config_key_hash=expected_config_key_hash,
            expected_browser_exam_key_hashes=expected_browser_exam_key_hashes,
            piloted_platforms=piloted_platforms,
            updated_by=updated_by,
            updated_at=_utc_now(),
        )
        self.profiles.upsert(profile)
        cache = self._profile_cache()
        if cache is not None:
            cache.invalidate(run_id)
        return profile

    def check_admission(
        self,
        *,
        run_id: str,
        uid: str,
        url: str,
        config_key_hash_header: Optional[str],
        request_hash_header: Optional[str],
    ) -> LockdownAdmissionOutcome:
        """Raises `LockdownAdmissionDeniedError` on anything but ADMITTED —
        callers never have to remember to check the return value."""
        profile = self.get_or_default_profile(run_id)
        outcome = verify_seb_admission(
            profile, url=url, config_key_hash_header=config_key_hash_header, request_hash_header=request_hash_header
        )
        self.admission_log.create(
            LockdownAdmissionLogEntry(
                id=uuid4().hex,
                run_id=run_id,
                uid=uid,
                outcome=outcome,
                config_key_header_present=bool(config_key_hash_header),
                request_hash_header_present=bool(request_hash_header),
                at=_utc_now(),
            )
        )
        if outcome != LockdownAdmissionOutcome.ADMITTED:
            raise LockdownAdmissionDeniedError(outcome.value)
        return outcome

    def list_admission_log(self, run_id: str) -> list[LockdownAdmissionLogEntry]:
        return self.admission_log.list_for_run(run_id)
