"""Resource scoping for exam routes: which institution owns an entity, and may
THIS caller act on it?

Ownership (`institution_id`) is stamped at creation and never changes, so
positive look-ups are cached in a small bounded LRU — which also keeps the
per-request Firestore read cost flat on the free (Spark) tier.

Forbidden and missing are deliberately indistinguishable (404) so ids cannot
be probed across institutions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import Depends, HTTPException, Request, status

from app.api.exams_dependencies import (
    active_membership,
    can_act_as_faculty,
    faculty_institution_ids,
    is_bootstrap_faculty,
    memberships_for,
    require_exam_faculty,
)
from app.core.config import settings
from app.core.firebase import db
from app.core.lookup_cache import LookupCache as _LookupCache
from app.domain.live.publisher import on_attempt_change

logger = logging.getLogger("hirelog.exams.scope")


@dataclass(frozen=True)
class RunAccess:
    """Facts about a run that never change after creation."""

    institution_id: Optional[str]
    enrolled_uids: frozenset[str]
    definition_id: str = ""
    revision_id: str = ""
    warning_threshold: int = 3


# Enrolment is a snapshot captured at run creation (ADR 0008), so a long TTL
# is only a safety net for a future "edit enrolment" feature.
_runs = _LookupCache(max_entries=128, ttl_seconds=900.0)
_owners = _LookupCache(max_entries=2048)
_lockdown = _LookupCache(max_entries=256, ttl_seconds=15.0)
# Faculty candidate-grid pages: every refetch of a 90-student grid is ~90 reads.
attempt_pages = _LookupCache(max_entries=64, ttl_seconds=max(settings.EXAM_ATTEMPT_LIST_CACHE_SECONDS, 0.001))
# A forced refresh is honoured only for an entry at least this old, so a refresh
# button cannot be used to re-read the whole grid in a loop.
ATTEMPT_PAGE_MIN_REFRESH_SECONDS = 3.0
# uid -> college ID for one institution (a roster listing is one read per member, so
# it is kept for a few minutes; a student's college ID never changes).
_college_ids = _LookupCache(max_entries=64, ttl_seconds=300.0)


def _drop_attempt_pages(run_id: str) -> None:
    attempt_pages.invalidate_where(lambda key: key[0] == run_id)


# Any committed attempt change (admit, submit, block, extension, ...) makes the cached
# grid for that run stale: drop it so the next refetch shows the change immediately.
on_attempt_change(_drop_attempt_pages)


def reset_caches() -> None:
    for cache in (_runs, _owners, _lockdown, attempt_pages, _college_ids):
        cache.clear()


def college_ids_by_uid(institution_id: Optional[str]) -> dict[str, str]:
    """Which college ID belongs to which account in an institution (empty for a pilot
    exam, which has no roster). Lets staff screens show people, not opaque ids."""
    if institution_id is None:
        return {}

    def load() -> dict[str, str]:
        from app.domain.institution.repository import MembershipRepository

        return {m.uid: m.identity_key for m in MembershipRepository(db).list_by_institution(institution_id) if m.uid}

    return _college_ids.get(institution_id, load)


# ── resolvers: entity id -> owning institution (None = pre-institution/pilot) ─

def run_access(run_id: str) -> RunAccess:
    def load() -> RunAccess:
        from app.domain.exams.repository import ExamRunRepository

        run = ExamRunRepository(db).get(run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        return RunAccess(
            run.institution_id, frozenset(run.enrolled_uids), run.definition_id, run.revision_id,
            run.policy.warning_threshold,
        )

    return _runs.get(run_id, load)


def run_institution(run_id: str) -> Optional[str]:
    return run_access(run_id).institution_id


def definition_institution(definition_id: str) -> Optional[str]:
    def load() -> Optional[str]:
        from app.domain.exams.repository import ExamDefinitionRepository

        definition = ExamDefinitionRepository(db).get(definition_id)
        if definition is None:
            raise LookupError(f"definition {definition_id} not found")
        return definition.institution_id

    return _owners.get(("definition", definition_id), load)


def _via(kind: str, entity_id: str, fetch: Callable[[str], Any], next_resolver: Callable[[str], Optional[str]], attr: str):
    def load() -> Optional[str]:
        entity = fetch(entity_id)
        if entity is None:
            raise LookupError(f"{kind} {entity_id} not found")
        return next_resolver(getattr(entity, attr))

    return _owners.get((kind, entity_id), load)


def blueprint_institution(blueprint_id: str) -> Optional[str]:
    from app.domain.generation.repository import GenerationRepository

    return _via("blueprint", blueprint_id, GenerationRepository(db).get_blueprint, definition_institution, "exam_definition_id")


def generation_request_institution(request_id: str) -> Optional[str]:
    from app.domain.generation.repository import GenerationRepository

    return _via("generation_request", request_id, GenerationRepository(db).get_request, blueprint_institution, "blueprint_id")


def candidate_institution(candidate_id: str) -> Optional[str]:
    from app.domain.generation.repository import GenerationRepository

    return _via("candidate", candidate_id, GenerationRepository(db).get_candidate, blueprint_institution, "blueprint_id")


def kt_source_institution(source_id: str) -> Optional[str]:
    from app.domain.kt.repository import ApprovedSourceRepository

    def load() -> Optional[str]:
        source = ApprovedSourceRepository(db).get(source_id)
        if source is None:
            raise LookupError(f"source {source_id} not found")
        return source.institution_id

    return _owners.get(("kt_source", source_id), load)


def kt_upload_institution(upload_id: str) -> Optional[str]:
    from app.domain.kt.repository import UploadedSourceRepository

    def load() -> Optional[str]:
        upload = UploadedSourceRepository(db).get(upload_id)
        if upload is None:
            raise LookupError(f"upload {upload_id} not found")
        return upload.institution_id

    return _owners.get(("kt_upload", upload_id), load)


def submission_institution(submission_id: str) -> Optional[str]:
    from app.domain.judge.repository import SubmissionRepository

    return _via("submission", submission_id, SubmissionRepository(db).get, run_institution, "run_id")


def short_answer_review_institution(review_id: str) -> Optional[str]:
    from app.domain.judge.repository import ShortAnswerReviewRepository

    return _via("short_answer_review", review_id, ShortAnswerReviewRepository(db).get, run_institution, "run_id")


def snapshot_institution(snapshot_id: str) -> Optional[str]:
    from app.domain.proctoring.repository import EvidenceSnapshotRepository

    return _via("snapshot", snapshot_id, EvidenceSnapshotRepository(db).get, run_institution, "run_id")


def proctoring_appeal_institution(appeal_id: str) -> Optional[str]:
    from app.domain.proctoring.repository import AppealRepository

    return _via("proctoring_appeal", appeal_id, AppealRepository(db).get, run_institution, "run_id")


def grade_appeal_institution(appeal_id: str) -> Optional[str]:
    from app.domain.gradebook.repository import GradeAppealRepository

    return _via("grade_appeal", appeal_id, GradeAppealRepository(db).get, run_institution, "run_id")


def export_job_institution(job_id: str) -> Optional[str]:
    from app.domain.gradebook.repository import ExportJobRepository

    return _via("export_job", job_id, ExportJobRepository(db).get, run_institution, "run_id")


def roster_job_institution(job_id: str) -> Optional[str]:
    from app.domain.institution.repository import RosterImportRepository

    def load() -> Optional[str]:
        job = RosterImportRepository(db).get(job_id)
        if job is None:
            raise LookupError(f"roster job {job_id} not found")
        return job.institution_id

    return _owners.get(("roster_job", job_id), load)


def lockdown_profile(run_id: str):
    from app.domain.lockdown.service import LockdownService

    return _lockdown.get(run_id, lambda: LockdownService(db).get_or_default_profile(run_id))


def invalidate_lockdown_profile(run_id: str) -> None:
    _lockdown.invalidate(run_id)


# ── student gate ──────────────────────────────────────────────────────────────

def assert_student_run_access(user: dict, run_id: str) -> RunAccess:
    try:
        access = run_access(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.") from exc
    if user["uid"] not in access.enrolled_uids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enrolled in this exam run.")
    if access.institution_id is not None and active_membership(user["uid"], access.institution_id) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "membership_not_active", "message": "Your institutional access is no longer active."},
        )
    return access


def accessible_institution_ids(uid: str) -> set[str]:
    from app.domain.institution.models import MembershipStatus

    return {m.institution_id for m in memberships_for(uid) if m.status == MembershipStatus.ACTIVE}


# ── faculty gate ──────────────────────────────────────────────────────────────

# Path parameters that name no scoped entity. `ticket_id` values are single-use
# capabilities issued only after the entity they point at passed this guard,
# and are bound to the issuing faculty uid by their own services.
NON_RESOURCE_PARAMS = frozenset({"uid", "command", "question_id", "slot_index", "ticket_id"})

DEFAULT_RESOLVERS: dict[str, Callable[[str], Optional[str]]] = {
    "run_id": run_institution,
    "definition_id": definition_institution,
    "blueprint_id": blueprint_institution,
    "request_id": generation_request_institution,
    "candidate_id": candidate_institution,
    "source_id": kt_source_institution,
    "upload_id": kt_upload_institution,
    "submission_id": submission_institution,
    "review_id": short_answer_review_institution,
    "snapshot_id": snapshot_institution,
    "job_id": export_job_institution,
    "institution_id": lambda value: value,
}


def assert_faculty_scope(user: dict, institution_id: Optional[str]) -> None:
    if not can_act_as_faculty(user, institution_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


def assert_faculty_scope_for(user: dict, resolver: Callable[[str], Optional[str]], entity_id: str) -> Optional[str]:
    try:
        institution_id = resolver(entity_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.") from exc
    assert_faculty_scope(user, institution_id)
    return institution_id


def make_faculty_scope_guard(overrides: Optional[dict[str, Callable[[str], Optional[str]]]] = None):
    """Router-level dependency: every path parameter must name an entity this
    faculty member may act on. A parameter with no registered resolver fails
    CLOSED, so a newly added route cannot silently skip scoping."""
    resolvers = {**DEFAULT_RESOLVERS, **(overrides or {})}

    def enforce_faculty_scope(request: Request, user: dict = Depends(require_exam_faculty)) -> dict:
        for name, value in request.path_params.items():
            if name in NON_RESOURCE_PARAMS:
                continue
            resolver = resolvers.get(name)
            if resolver is None:
                logger.error("faculty route %s has unscoped path parameter %r", request.url.path, name)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Route scope is not configured.")
            assert_faculty_scope_for(user, resolver, str(value))
        return user

    enforce_faculty_scope.__name__ = "enforce_faculty_scope"
    enforce_faculty_scope.scope_resolvers = resolvers  # type: ignore[attr-defined]
    return enforce_faculty_scope


def resolve_creation_institution(user: dict, requested: Optional[str]) -> Optional[str]:
    """Which institution a new definition/source belongs to. Institution
    faculty with a single institution get it implicitly; platform (bootstrap)
    faculty with no institution create pilot-scope records."""
    if requested:
        assert_faculty_scope(user, requested)
        from app.domain.institution.repository import InstitutionRepository

        if InstitutionRepository(db).get(requested) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
        return requested
    institutions = faculty_institution_ids(user)
    if len(institutions) == 1:
        return next(iter(institutions))
    if len(institutions) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "institution_required", "institutions": sorted(institutions)},
        )
    if is_bootstrap_faculty(user):
        return None
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Exam faculty authority required.")


def visible_institution_ids(user: dict) -> Optional[set[str]]:
    """Institutions whose records this faculty member may list. None = every
    institution (platform operator)."""
    return None if is_bootstrap_faculty(user) else faculty_institution_ids(user)
