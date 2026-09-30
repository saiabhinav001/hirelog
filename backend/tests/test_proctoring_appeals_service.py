"""`AppealsService` tests: submission, resolution, and review reversal
(re-resolving bumps version rather than editing history away)."""

from __future__ import annotations

from app.domain.proctoring.appeals_service import AppealsService
from app.domain.proctoring.models import Appeal, AppealStatus


class _FakeAppealRepo:
    def __init__(self) -> None:
        self.store: dict[str, Appeal] = {}

    def create(self, appeal):
        self.store[appeal.id] = appeal

    def get(self, appeal_id):
        return self.store.get(appeal_id)

    def update(self, appeal_id, update):
        self.store[appeal_id] = self.store[appeal_id].model_copy(update=update)

    def list_for_run(self, run_id):
        return [a for a in self.store.values() if a.run_id == run_id]


def _service() -> AppealsService:
    service = AppealsService.__new__(AppealsService)
    service.appeals = _FakeAppealRepo()
    return service


def test_submit_appeal_starts_pending() -> None:
    service = _service()
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="I lost connection, did not leave the page.")
    assert appeal.status == AppealStatus.PENDING
    assert appeal.version == 1


def test_resolve_appeal_overturned() -> None:
    service = _service()
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="...")
    resolved = service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-1", status=AppealStatus.OVERTURNED, resolution_notes="Evidence supports the student.")
    assert resolved.status == AppealStatus.OVERTURNED
    assert resolved.resolved_by == "fac-1"
    assert resolved.version == 1  # first resolution — not a reversal yet


def test_review_reversal_bumps_version_without_losing_history() -> None:
    service = _service()
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="...")
    first = service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-1", status=AppealStatus.UPHELD, resolution_notes="Initial review: upheld.")
    assert first.version == 1

    reversed_decision = service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-2", status=AppealStatus.OVERTURNED, resolution_notes="On further review, overturned.")
    assert reversed_decision.status == AppealStatus.OVERTURNED
    assert reversed_decision.version == 2


def test_list_for_run_scopes_to_that_run_only() -> None:
    service = _service()
    service.submit_appeal(run_id="run-1", uid="u1", student_statement="a")
    service.submit_appeal(run_id="run-2", uid="u2", student_statement="b")
    assert len(service.appeals.list_for_run("run-1")) == 1
