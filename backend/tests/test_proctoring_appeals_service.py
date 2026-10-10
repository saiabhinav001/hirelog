"""`AppealsService` tests against the in-memory Firestore with optimistic transactions: filing (one pending per attempt),
resolution, and review reversal that keeps every earlier decision."""

from __future__ import annotations

import threading

import pytest

from app.domain.proctoring.appeals_service import AppealsService
from app.domain.proctoring.models import AppealStatus
from exam_inmemory import FakeFirestore, audit_kinds, install_optimistic_transactions


@pytest.fixture
def service(monkeypatch) -> AppealsService:
    db = FakeFirestore()
    install_optimistic_transactions(monkeypatch, db)
    return AppealsService(db)


def test_submit_appeal_starts_pending(service) -> None:
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="I lost connection, did not leave the page.")
    assert appeal.status == AppealStatus.PENDING and appeal.version == 1 and appeal.decisions == []


def test_a_student_has_one_pending_appeal_per_attempt(service) -> None:
    first = service.submit_appeal(run_id="run-1", uid="u1", student_statement="first")
    again = service.submit_appeal(run_id="run-1", uid="u1", student_statement="second try")
    assert again.id == first.id and again.student_statement == "first"
    assert len(service.appeals.list_for_run("run-1")) == 1


def test_two_simultaneous_filings_leave_one_appeal(service) -> None:
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.submit_appeal(run_id="run-1", uid="u1", student_statement="x"))) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(service.appeals.list_for_run("run-1")) == 1 and len({a.id for a in results}) == 1


def test_a_new_appeal_is_possible_once_the_earlier_one_is_decided(service) -> None:
    first = service.submit_appeal(run_id="run-1", uid="u1", student_statement="first")
    service.resolve_appeal(appeal_id=first.id, resolved_by="fac-1", status=AppealStatus.UPHELD, resolution_notes="block stands")
    second = service.submit_appeal(run_id="run-1", uid="u1", student_statement="new evidence")
    assert second.id != first.id and second.status == AppealStatus.PENDING


def test_resolve_appeal_overturned_is_audited(service) -> None:
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="...")
    resolved = service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-1", status=AppealStatus.OVERTURNED, resolution_notes="Evidence supports the student.")
    assert resolved.status == AppealStatus.OVERTURNED and resolved.resolved_by == "fac-1" and resolved.version == 1
    assert audit_kinds(service._db) == ["exam_appeal_resolved"]


def test_review_reversal_keeps_every_earlier_decision(service) -> None:
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="...")
    service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-1", status=AppealStatus.UPHELD, resolution_notes="Initial review: upheld.")
    final = service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-2", status=AppealStatus.OVERTURNED, resolution_notes="On further review, overturned.")

    assert final.status == AppealStatus.OVERTURNED and final.version == 2
    stored = service.appeals.get(appeal.id)
    assert [(d.status, d.decided_by, d.notes) for d in stored.decisions] == [
        (AppealStatus.UPHELD, "fac-1", "Initial review: upheld."),
        (AppealStatus.OVERTURNED, "fac-2", "On further review, overturned."),
    ]
    assert audit_kinds(service._db) == ["exam_appeal_resolved", "exam_appeal_resolved"]


def test_an_appeal_cannot_be_resolved_back_to_pending(service) -> None:
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="...")
    with pytest.raises(ValueError):
        service.resolve_appeal(appeal_id=appeal.id, resolved_by="fac-1", status=AppealStatus.PENDING, resolution_notes="reset")
    assert service.appeals.get(appeal.id).status == AppealStatus.PENDING


def test_two_reviewers_deciding_at_once_lose_neither_decision(service) -> None:
    appeal = service.submit_appeal(run_id="run-1", uid="u1", student_statement="...")
    threads = [
        threading.Thread(target=lambda who=who, status=status: service.resolve_appeal(appeal_id=appeal.id, resolved_by=who, status=status, resolution_notes="n"))
        for who, status in (("fac-1", AppealStatus.UPHELD), ("fac-2", AppealStatus.OVERTURNED))
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(d.decided_by for d in service.appeals.get(appeal.id).decisions) == ["fac-1", "fac-2"]


def test_resolving_an_unknown_appeal_is_a_lookup_error(service) -> None:
    with pytest.raises(LookupError):
        service.resolve_appeal(appeal_id="nope", resolved_by="fac-1", status=AppealStatus.UPHELD, resolution_notes="n")


def test_list_for_run_scopes_to_that_run_only(service) -> None:
    service.submit_appeal(run_id="run-1", uid="u1", student_statement="a")
    service.submit_appeal(run_id="run-2", uid="u2", student_statement="b")
    assert len(service.appeals.list_for_run("run-1")) == 1
