"""Institutional auth routes: OTP challenge/verify (no existing session
required — this IS how a session is obtained) and verified account linking
(requires an existing legacy session). Legacy login is untouched — these are
entirely new, additive endpoints behind EXAMS_ENABLED.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.exams_dependencies import get_current_exam_user, require_exams_enabled
from app.core.firebase import db
from app.domain.institution.auth_service import InstitutionAuthService
from app.domain.institution.errors import AccountAlreadyLinkedError, IdentityAlreadyLinkedError

router = APIRouter(
    prefix="/api/exams/v1/auth",
    tags=["exams-institution-auth"],
    dependencies=[Depends(require_exams_enabled)],
)


def _service() -> InstitutionAuthService:
    return InstitutionAuthService(db)


class ChallengeRequest(BaseModel):
    institution_id: str
    identity_key: str = Field(min_length=1, max_length=64)
    contact_email: str = Field(min_length=3, max_length=254)


class VerifyRequest(BaseModel):
    institution_id: str
    identity_key: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=6, max_length=6)


class LinkRequest(BaseModel):
    institution_id: str
    identity_key: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=6, max_length=6)


@router.post("/challenge")
def challenge(payload: ChallengeRequest) -> dict:
    return _service().challenge(
        institution_id=payload.institution_id, identity_key=payload.identity_key, contact_email=payload.contact_email
    )


@router.post("/verify")
def verify(payload: VerifyRequest) -> dict:
    result = _service().verify_and_sign_in(
        institution_id=payload.institution_id, identity_key=payload.identity_key, code=payload.code
    )
    return {"custom_token": result.custom_token, "uid": result.uid}


@router.post("/link")
def link(payload: LinkRequest, user: dict = Depends(get_current_exam_user)) -> dict:
    try:
        _service().link_existing_account(
            institution_id=payload.institution_id, identity_key=payload.identity_key,
            code=payload.code, existing_uid=user["uid"],
        )
    except (IdentityAlreadyLinkedError, AccountAlreadyLinkedError) as exc:
        # Audited by the service; an operator resolves it with the `unlink` action.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return {"status": "linked", "uid": user["uid"]}
