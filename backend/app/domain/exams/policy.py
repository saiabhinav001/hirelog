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


class ProctoringProfile(BaseModel):
    """Per-run proctoring settings (Prompt 9, ADR 0025). Every field is optional: unset = the deployment default
    (`EXAM_PROCTORING_*`, `EXAM_EVIDENCE_RETENTION_DAYS`), so a run without a profile behaves exactly as before.
    These tune what the server does with browser-REPORTED signals; they cannot make a browser enforce anything."""

    # A departure shorter than this is not counted.
    grace_seconds: Optional[float] = Field(default=None, ge=0, le=60)
    # A return-and-leave inside this window continues the same departure.
    cooldown_seconds: Optional[float] = Field(default=None, ge=0, le=60)
    # Extra counted departures tolerated after a reviewer allows the attempt to continue (the ledger is never reset).
    allowance_after_review: int = Field(default=0, ge=0, le=10)
    # How long evidence for this run is kept; never below a week, so an appeal can still be heard.
    evidence_retention_days: Optional[int] = Field(default=None, ge=7, le=365)
    # Who set the profile; stamped by the server when the run is created.
    set_by: Optional[str] = Field(default=None, max_length=128)

    model_config = {"extra": "forbid"}


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
    # Prompt 7 (ADR 0023): minutes before a student's deadline at which the workspace announces the time left.
    # Display only - the deadline itself is always the server's.
    time_warning_minutes: list[int] = Field(default_factory=lambda: [10], max_length=8)
    # Prompt 9 (ADR 0025): optional per-run proctoring settings; None = deployment defaults.
    proctoring_profile: Optional[ProctoringProfile] = None

    model_config = {"extra": "forbid"}

    @field_validator("time_warning_minutes")
    @classmethod
    def _validate_time_warnings(cls, value: list[int]):
        if any(m < 1 or m > 24 * 60 for m in value):
            raise ValueError("time warnings must be between 1 and 1440 minutes")
        return sorted(set(value), reverse=True)

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
