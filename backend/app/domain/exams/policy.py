"""Validated exam policy schema (contract #5: "Default warning threshold is 3;
policy can customize it.")."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ACCOMMODATIONS = 5000


class AccommodationWindow(BaseModel):
    """Per-student accommodation — explicit extra time, never inferred."""

    extra_minutes: int = Field(ge=0, le=24 * 60)
    # Institution-approved camera alternative (Prompt 10 accessibility
    # contract) — explicit, faculty-authored, never auto-derived or
    # inferred from a permission-denied event after the fact.
    camera_waived: bool = False
    note: str = Field(default="", max_length=240)


class ExamPolicy(BaseModel):
    duration_minutes: int = Field(ge=1, le=24 * 60)
    warning_threshold: int = Field(default=3, ge=1, le=50)
    late_admission_grace_seconds: int = Field(default=120, ge=0, le=3600)
    allow_pause: bool = True
    # Keyed by uid — explicit, faculty-authored, never auto-derived.
    accommodations: dict[str, AccommodationWindow] = Field(default_factory=dict)

    # ── Prompt 3 additive fields (ADR 0019) ───────────────────────────────
    # Every default reproduces pre-Prompt-3 behavior: a run created without
    # them has no schedule, no display timezone and one-variant-per-student.
    #
    # IANA zone used ONLY to display the schedule to people. Enforcement is
    # always UTC: deadlines, windows and the sweeper never read this field.
    timezone: Optional[str] = Field(default=None, max_length=64)
    # Admission window, tz-aware, normalised to UTC. With
    # EXAM_SCHEDULE_AUTOMATION_ENABLED the sweeper releases the run at
    # `scheduled_start` and closes it (stop admitting) at `scheduled_end`;
    # an attempt that was already admitted keeps its own server-computed
    # deadline, so accommodated students are never cut off by the window.
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    # How a slot's approved pool is spread across the cohort. "unique" is the
    # historical behavior (reservation is blocked when the pool is too small);
    # "balanced_reuse" pre-authorises explicit round-robin reuse.
    variant_policy: Literal["unique", "balanced_reuse"] = "unique"

    model_config = {"extra": "forbid"}

    @field_validator("accommodations")
    @classmethod
    def _validate_accommodation_keys(cls, value: dict[str, AccommodationWindow]):
        if len(value) > MAX_ACCOMMODATIONS:
            raise ValueError(f"at most {MAX_ACCOMMODATIONS} accommodations are allowed")
        for uid in value:
            if not uid or not uid.strip():
                raise ValueError("accommodation uid keys must be non-empty")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: Optional[str]):
        if value is None:
            return None
        name = value.strip()
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"{value!r} is not a known IANA time zone") from None
        return name

    @field_validator("scheduled_start", "scheduled_end")
    @classmethod
    def _require_aware_utc(cls, value: Optional[datetime]):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("schedule timestamps must include a UTC offset")
        return value.astimezone(dt_timezone.utc)

    @model_validator(mode="after")
    def _validate_window(self):
        if self.scheduled_start and self.scheduled_end and self.scheduled_end <= self.scheduled_start:
            raise ValueError("scheduled_end must be after scheduled_start")
        return self

    def extra_minutes_for(self, uid: str) -> int:
        window = self.accommodations.get(uid)
        return window.extra_minutes if window else 0

    def camera_waived_for(self, uid: str) -> bool:
        window = self.accommodations.get(uid)
        return window.camera_waived if window else False
