"""Validated exam policy schema (contract #5: "Default warning threshold is 3;
policy can customize it.")."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class AccommodationWindow(BaseModel):
    """Per-student accommodation — explicit extra time, never inferred."""

    extra_minutes: int = Field(ge=0, le=24 * 60)
    note: str = Field(default="", max_length=240)


class ExamPolicy(BaseModel):
    duration_minutes: int = Field(ge=1, le=24 * 60)
    warning_threshold: int = Field(default=3, ge=1, le=50)
    late_admission_grace_seconds: int = Field(default=120, ge=0, le=3600)
    allow_pause: bool = True
    # Keyed by uid — explicit, faculty-authored, never auto-derived.
    accommodations: dict[str, AccommodationWindow] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @field_validator("accommodations")
    @classmethod
    def _validate_accommodation_keys(cls, value: dict[str, AccommodationWindow]):
        for uid in value:
            if not uid or not uid.strip():
                raise ValueError("accommodation uid keys must be non-empty")
        return value

    def extra_minutes_for(self, uid: str) -> int:
        window = self.accommodations.get(uid)
        return window.extra_minutes if window else 0
