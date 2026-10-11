"""Faculty routes for configuring a run's managed-lockdown admission
profile and reviewing its admission audit log. Student admission itself is
enforced by `require_seb_admission` (composed directly into
`exams_student.py`'s content/answer routes), not by a route in this file.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.exams_dependencies import require_exams_enabled
from app.api.exams_scope import require_run_scope
from app.core.firebase import exam_db as db
from app.domain.lockdown.service import LockdownService

router = APIRouter(
    prefix="/api/exams/v1/faculty/lockdown", tags=["exams-lockdown-faculty"],
    dependencies=[Depends(require_exams_enabled)],
)


class ConfigureLockdownRequest(BaseModel):
    enabled: bool
    expected_config_key_hash: Optional[str] = Field(default=None, max_length=200)
    expected_browser_exam_key_hashes: list[str] = Field(default_factory=list, max_length=20)
    piloted_platforms: list[str] = Field(default_factory=list, max_length=50)


@router.get("/runs/{run_id}/profile")
def get_lockdown_profile(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    profile = LockdownService(db).get_or_default_profile(run_id)
    return profile.model_dump(mode="json")


@router.post("/runs/{run_id}/profile")
def configure_lockdown_profile(run_id: str, payload: ConfigureLockdownRequest, user: dict = Depends(require_run_scope)) -> dict:
    """Enabling with no keys configured is allowed to be SAVED (so an admin
    can stage the toggle before pasting keys) but `require_seb_admission`
    fails closed (`denied_not_configured`) for any student request against
    it until at least one key is present — never a silent pass-through."""
    profile = LockdownService(db).configure_profile(
        run_id=run_id,
        enabled=payload.enabled,
        expected_config_key_hash=payload.expected_config_key_hash,
        expected_browser_exam_key_hashes=payload.expected_browser_exam_key_hashes,
        piloted_platforms=payload.piloted_platforms,
        updated_by=user["uid"],
    )
    return profile.model_dump(mode="json")


@router.get("/runs/{run_id}/admission-log")
def list_admission_log(run_id: str, user: dict = Depends(require_run_scope)) -> dict:
    entries = LockdownService(db).list_admission_log(run_id)
    return {"results": [e.model_dump(mode="json") for e in entries]}
