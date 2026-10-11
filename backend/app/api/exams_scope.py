"""Resource-scope authorization for exam routes (Prompt 1, ADR 0017).

`require_exam_faculty` only proves the caller holds a teacher/super-admin ROLE.
These dependencies add the missing half: the caller may act on THIS resource —
as its owner, as an ADMIN of the resource's institution, or as a super_admin.
Every faculty route that takes a resource id resolves it here (the id's owner
chain: appeal/snapshot/export/review/submission -> run -> definition;
candidate/request -> blueprint -> definition; source -> upload) so an id alone
is never authorization. Routes whose resource id arrives in the body call the
`authorize_*` helpers inline; `tests/test_exam_authz_route_guards.py` fails if a
route with a scoped path id forgets its dependency.

A missing resource is 404; an existing resource the caller may not touch is a
generic 403 (ids are random 128-bit values, so this does not enable probing).
"""

from __future__ import annotations

from typing import Optional, Sequence

from fastapi import Depends, HTTPException, status

from app.api.exams_dependencies import require_exam_faculty, resolve_principal
from app.core.firebase import exam_db as db
from app.domain.authz.policy import (
    InstitutionRequiredError,
    ScopeDeniedError,
    can_administer_institution,
    can_manage_resource,
    max_grantable_role_scope,
    resolve_creation_institution,
)
from app.domain.exams.repository import ExamDefinitionRepository, ExamRunRepository
from app.domain.generation.repository import GenerationRepository
from app.domain.gradebook.repository import ExportJobRepository, GradeAppealRepository
from app.domain.institution.models import MembershipStatus, RoleScope
from app.domain.institution.repository import InstitutionRepository, MembershipRepository
from app.domain.judge.repository import ShortAnswerReviewRepository, SubmissionRepository
from app.domain.kt.repository import ApprovedSourceRepository, UploadedSourceRepository
from app.domain.proctoring.repository import AppealRepository, EvidenceSnapshotRepository


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "scope_denied", "message": "Not permitted for this resource."},
    )


def _not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{kind} not found")


def _can_manage(
    user: dict, *, owner_uid: Optional[str], institution_id: Optional[str], co_owner_uids: Sequence[str] = ()
) -> bool:
    return can_manage_resource(
        resolve_principal(user), owner_uid=owner_uid, institution_id=institution_id, co_owner_uids=co_owner_uids
    )


# ── Exam resources ────────────────────────────────────────────────────────────

def authorize_definition(user: dict, definition_id: str):
    definition = ExamDefinitionRepository(db).get(definition_id)
    if definition is None:
        raise _not_found("exam definition")
    if not _can_manage(
        user, owner_uid=definition.owner_uid, institution_id=definition.institution_id,
        co_owner_uids=definition.instructor_uids,
    ):
        raise _forbidden()
    return definition


def authorize_instructor_assignment(user: dict, definition_id: str):
    """Who may change a definition's instructor list: a super-admin or an ADMIN of
    its institution — not the owner and not a co-instructor, so teaching access
    cannot be handed on by the people it was given to."""
    definition = ExamDefinitionRepository(db).get(definition_id)
    if definition is None:
        raise _not_found("exam definition")
    if not can_administer_institution(resolve_principal(user), definition.institution_id):
        raise _forbidden()
    return definition


def user_can_manage_run(user: dict, run) -> bool:
    if resolve_principal(user).is_super_admin:
        return True
    definition = ExamDefinitionRepository(db).get(run.definition_id)
    owner_uid = definition.owner_uid if definition else None
    institution_id = run.institution_id or (definition.institution_id if definition else None)
    return _can_manage(
        user, owner_uid=owner_uid, institution_id=institution_id,
        co_owner_uids=definition.instructor_uids if definition else (),
    )


def authorize_run(user: dict, run_id: str):
    run = ExamRunRepository(db).get(run_id)
    if run is None:
        raise _not_found("exam run")
    if not user_can_manage_run(user, run):
        raise _forbidden()
    return run


def authorize_blueprint(user: dict, blueprint_id: str):
    blueprint = GenerationRepository(db).get_blueprint(blueprint_id)
    if blueprint is None:
        raise _not_found("blueprint")
    definition = ExamDefinitionRepository(db).get(blueprint.exam_definition_id)
    owner_uid = definition.owner_uid if definition else blueprint.created_by
    institution_id = definition.institution_id if definition else None
    if not _can_manage(
        user, owner_uid=owner_uid, institution_id=institution_id,
        co_owner_uids=definition.instructor_uids if definition else (),
    ):
        raise _forbidden()
    return blueprint


def authorize_candidate(user: dict, candidate_id: str):
    candidate = GenerationRepository(db).get_candidate(candidate_id)
    if candidate is None:
        raise _not_found("candidate")
    authorize_blueprint(user, candidate.blueprint_id)
    return candidate


def authorize_generation_request(user: dict, request_id: str):
    request = GenerationRepository(db).get_request(request_id)
    if request is None:
        raise _not_found("request")
    authorize_blueprint(user, request.blueprint_id)
    return request


# ── Source library (private per teacher) ──────────────────────────────────────

def _source_owner(source) -> Optional[str]:
    if source.created_by:
        return source.created_by
    if source.upload_id:
        upload = UploadedSourceRepository(db).get(source.upload_id)
        if upload is not None:
            return upload.uploaded_by
    return None


def authorize_upload(user: dict, upload_id: str):
    upload = UploadedSourceRepository(db).get(upload_id)
    if upload is None:
        raise _not_found("upload")
    if not _can_manage(user, owner_uid=upload.uploaded_by, institution_id=None):
        raise _forbidden()
    return upload


def authorize_source(user: dict, source_id: str):
    source = ApprovedSourceRepository(db).get(source_id)
    if source is None:
        raise _not_found("source")
    if not _can_manage(user, owner_uid=_source_owner(source), institution_id=None):
        raise _forbidden()
    return source


def filter_accessible_sources(user: dict, sources: Sequence) -> list:
    principal = resolve_principal(user)
    if principal.is_super_admin:
        return list(sources)
    return [s for s in sources if can_manage_resource(principal, owner_uid=_source_owner(s), institution_id=None)]


# ── Run-owned records (resolved through their run) ────────────────────────────

def authorize_submission(user: dict, submission_id: str):
    submission = SubmissionRepository(db).get(submission_id)
    if submission is None:
        raise _not_found("submission")
    authorize_run(user, submission.run_id)
    return submission


def authorize_short_answer_review(user: dict, review_id: str):
    review = ShortAnswerReviewRepository(db).get(review_id)
    if review is None:
        raise _not_found("review")
    authorize_run(user, review.run_id)
    return review


def authorize_grade_appeal(user: dict, appeal_id: str):
    appeal = GradeAppealRepository(db).get(appeal_id)
    if appeal is None:
        raise _not_found("appeal")
    authorize_run(user, appeal.run_id)
    return appeal


def authorize_proctoring_appeal(user: dict, appeal_id: str):
    appeal = AppealRepository(db).get(appeal_id)
    if appeal is None:
        raise _not_found("appeal")
    authorize_run(user, appeal.run_id)
    return appeal


def authorize_export_job(user: dict, job_id: str):
    job = ExportJobRepository(db).get(job_id)
    if job is None:
        raise _not_found("export job")
    authorize_run(user, job.run_id)
    return job


def authorize_snapshot(user: dict, snapshot_id: str):
    snapshot = EvidenceSnapshotRepository(db).get(snapshot_id)
    if snapshot is None:
        raise _not_found("snapshot")
    authorize_run(user, snapshot.run_id)
    return snapshot


def filter_reviews_for_user(user: dict, reviews: Sequence) -> list:
    """Pending short-answer reviews are listed globally; keep only those on
    runs the caller may manage (one run lookup per distinct run)."""
    decisions: dict[str, bool] = {}
    allowed = []
    for review in reviews:
        if review.run_id not in decisions:
            run = ExamRunRepository(db).get(review.run_id)
            decisions[review.run_id] = bool(run) and user_can_manage_run(user, run)
        if decisions[review.run_id]:
            allowed.append(review)
    return allowed


# ── Institutions ──────────────────────────────────────────────────────────────

def _require_institution_exists(institution_id: str) -> None:
    if InstitutionRepository(db).get(institution_id) is None:
        raise _not_found("institution")


def authorize_institution_roster(user: dict, institution_id: str) -> RoleScope:
    """Roster import is allowed to super-admins, ADMINs of the institution
    (may grant up to ADMIN) and its FACULTY (may grant STUDENT only)."""
    scope = max_grantable_role_scope(resolve_principal(user), institution_id)
    if scope is None:
        raise _forbidden()
    _require_institution_exists(institution_id)
    return scope


def authorize_institution_admin(user: dict, institution_id: str) -> None:
    principal = resolve_principal(user)
    if not (principal.is_super_admin or institution_id in principal.admin_institution_ids):
        raise _forbidden()
    _require_institution_exists(institution_id)


# ── Creation-time scope ───────────────────────────────────────────────────────

def resolve_new_definition_institution(user: dict, requested: Optional[str]) -> Optional[str]:
    try:
        institution_id = resolve_creation_institution(resolve_principal(user), requested)
    except ScopeDeniedError as exc:
        raise _forbidden() from exc
    except InstitutionRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    if requested and institution_id:
        _require_institution_exists(institution_id)
    return institution_id


def validate_enrollment_scope(institution_id: Optional[str], uids: Sequence[str]) -> None:
    """An institution-scoped run may only enroll that institution's ACTIVE
    student members; unscoped (pilot) runs keep the uid-list behavior."""
    if not institution_id:
        return
    members = MembershipRepository(db).list_by_institution(institution_id)
    allowed = {
        m.uid for m in members if m.uid and m.status == MembershipStatus.ACTIVE and m.role_scope == RoleScope.STUDENT
    }
    offenders = [uid for uid in dict.fromkeys(uids) if uid not in allowed]
    if offenders:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "enrollment_outside_institution",
                "message": "Every enrolled uid must be an active student of the run's institution.",
                "uids": offenders[:20],
            },
        )


def validate_variant_targets(run, uids: Sequence[str]) -> None:
    unknown = [uid for uid in dict.fromkeys(uids) if uid not in run.enrolled_uids]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "uids_not_enrolled", "message": "Variants can only be reserved for enrolled students.", "uids": unknown[:20]},
        )


# ── FastAPI dependencies (path / query id -> scope check; each returns `user`) ─

def require_definition_scope(definition_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_definition(user, definition_id)
    return user


def require_instructor_admin_scope(definition_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_instructor_assignment(user, definition_id)
    return user


def require_run_scope(run_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_run(user, run_id)
    return user


def require_blueprint_scope(blueprint_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_blueprint(user, blueprint_id)
    return user


def require_candidate_scope(candidate_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_candidate(user, candidate_id)
    return user


def require_generation_request_scope(request_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_generation_request(user, request_id)
    return user


def require_source_scope(source_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_source(user, source_id)
    return user


def require_upload_scope(upload_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_upload(user, upload_id)
    return user


def require_submission_scope(submission_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_submission(user, submission_id)
    return user


def require_short_answer_review_scope(review_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_short_answer_review(user, review_id)
    return user


def require_grade_appeal_scope(appeal_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_grade_appeal(user, appeal_id)
    return user


def require_proctoring_appeal_scope(appeal_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_proctoring_appeal(user, appeal_id)
    return user


def require_export_job_scope(job_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_export_job(user, job_id)
    return user


def require_snapshot_scope(snapshot_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_snapshot(user, snapshot_id)
    return user


def require_institution_roster_scope(institution_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_institution_roster(user, institution_id)
    return user


def require_institution_admin_scope(institution_id: str, user: dict = Depends(require_exam_faculty)) -> dict:
    authorize_institution_admin(user, institution_id)
    return user
