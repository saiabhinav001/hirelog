"""The read economy: what the exam caches save, and that every write made through this process drops them again."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core import background, read_cache
from app.core.config import settings
from app.domain.authz.staff_binding import StaffBindingService
from app.domain.exams import attempts_view, sweep_hints
from app.domain.exams.models import AttemptStatus, OutboxEntry, RunStatus
from app.domain.exams.repository import AttemptRepository, ExamDefinitionRepository, ExamRunRepository, OutboxRepository
from app.domain.exams.student_service import ExamStudentService
from app.domain.institution.models import Membership
from app.domain.institution.repository import MembershipRepository
from app.domain.lockdown.service import LockdownService
import exam_inmemory as mem
from exam_inmemory import T0, make_env, seed_attempt, seed_run

QUESTION = "rev-1:slot:0"


@pytest.fixture(autouse=True)
def _caches_on(monkeypatch):
    monkeypatch.setattr(settings, "EXAM_READ_CACHE_ENABLED", True)
    read_cache.clear_all()
    attempts_view.clear_all()
    sweep_hints.reset()
    yield
    read_cache.clear_all()
    attempts_view.clear_all()
    sweep_hints.reset()


class _Reads:
    """Billable reads as Firestore counts them: one per document fetched, and at least one per query."""

    def __init__(self) -> None:
        self.gets = 0
        self.queries = 0
        self.query_docs = 0

    @property
    def billed(self) -> int:
        return self.gets + max(self.queries, self.query_docs)

    def reset(self) -> None:
        self.gets = self.queries = self.query_docs = 0


@pytest.fixture
def reads(monkeypatch) -> _Reads:
    counter = _Reads()
    original_get, original_stream = mem._DocRef.get, mem._Query.stream

    def get(self, transaction=None):
        counter.gets += 1
        return original_get(self, transaction=transaction)

    def stream(self, transaction=None):
        rows = original_stream(self, transaction=transaction)
        counter.queries += 1
        counter.query_docs += len(rows)
        return rows

    monkeypatch.setattr(mem._DocRef, "get", get)
    monkeypatch.setattr(mem._Query, "stream", stream)
    return counter


def _service(env) -> ExamStudentService:
    return ExamStudentService(env.db, clock=env.clock)


def _save(service, uid="s1", *, version=0, answer=None, question=QUESTION):
    return service.save_draft("run-1", uid, question, expected_version=version, answer=answer or {"text": "x"}, client_epoch=1)


# -- run / definition / revision ------------------------------------------------------------------------------------

def test_a_run_is_read_once_until_it_changes(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    repo = ExamRunRepository(env.db)
    reads.reset()
    assert repo.get("run-1").status == RunStatus.RELEASED
    assert repo.get("run-1").status == RunStatus.RELEASED
    assert reads.billed == 1


def test_a_lifecycle_command_is_visible_to_the_next_read(monkeypatch) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    repo = ExamRunRepository(env.db)
    assert repo.get("run-1").status == RunStatus.RELEASED  # now cached
    env.service.execute(run_id="run-1", command="pause", expected_version=1, idempotency_key="k1", actor_uid="f1")
    assert repo.get("run-1").status == RunStatus.PAUSED


def test_granting_access_is_visible_to_the_next_authorization_check(monkeypatch) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db, enrolled=("s1",))
    repo = ExamRunRepository(env.db)
    assert "s2" not in repo.get("run-1").enrolled_uids
    env.service.change_access(run_id="run-1", action="grant", uid="s2", idempotency_key="g1", actor_uid="f1", reason="late")
    assert "s2" in repo.get("run-1").enrolled_uids
    env.service.change_access(run_id="run-1", action="revoke", uid="s2", idempotency_key="r1", actor_uid="f1", reason="oops")
    assert "s2" not in repo.get("run-1").enrolled_uids


def test_a_cached_run_cannot_be_changed_through_the_returned_object(monkeypatch) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    repo = ExamRunRepository(env.db)
    repo.get("run-1").enrolled_uids.append("intruder")
    assert "intruder" not in repo.get("run-1").enrolled_uids


def test_a_published_revision_is_cached_and_a_missing_one_is_not(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    env.db.put("exam_definitions", "def-1", "revisions", "rev-1", {
        "definition_id": "def-1", "revision_number": 1, "title": "Midterm", "published_at": T0.isoformat(),
        "published_by": "f1", "slot_count": 1,
    })
    repo = ExamDefinitionRepository(env.db)
    reads.reset()
    assert repo.get_revision("def-1", "rev-1") is not None
    assert repo.get_revision("def-1", "rev-1") is not None
    assert reads.billed == 1
    assert repo.get_revision("def-1", "nope") is None
    assert repo.get_revision("def-1", "nope") is None
    assert reads.billed == 3  # the missing one was looked up both times


# -- the save path --------------------------------------------------------------------------------------------------

def test_a_draft_save_spends_its_reads_on_the_attempt_and_the_draft_only(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    service = _service(env)
    assert _save(service).accepted  # warms the run cache
    reads.reset()
    outcome = _save(service, version=1, answer={"text": "xy"})
    assert outcome.accepted and outcome.new_version == 2
    assert reads.billed == 2  # attempt + draft inside the transaction, no run read


def test_the_navigator_and_version_list_do_not_re_read_every_draft(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1", current_slot_index=2)
    service = _service(env)
    revision = type("Rev", (), {"id": "rev-1", "slot_count": 5})()
    assert _save(service, question="rev-1:slot:0").accepted
    assert _save(service, question="rev-1:slot:1", answer={"text": "b"}).accepted

    first = service.list_released_slots("run-1", "s1", revision)
    assert [slot["answered"] for slot in first["slots"]] == [True, True, False]

    assert _save(service, question="rev-1:slot:2", answer={"text": "c"}).accepted
    reads.reset()
    second = service.list_released_slots("run-1", "s1", revision)
    assert [slot["answered"] for slot in second["slots"]] == [True, True, True]
    assert reads.query_docs == 0 and reads.queries == 0  # served from the write-through summary
    assert reads.billed <= 2  # the attempt (+ the run once its TTL lapsed)

    versions = {question_id: summary.version for question_id, summary in service.drafts.summaries("run-1", "s1").items()}
    assert versions == {"rev-1:slot:0": 1, "rev-1:slot:1": 1, "rev-1:slot:2": 1}


def test_marking_for_review_updates_the_summary_without_a_reload(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    service = _service(env)
    revision = type("Rev", (), {"id": "rev-1", "slot_count": 3})()
    assert _save(service).accepted
    service.list_released_slots("run-1", "s1", revision)  # loads the summary
    assert service.save_draft(
        "run-1", "s1", QUESTION, expected_version=1, answer={"text": "x"}, marked_for_review=True, client_epoch=1
    ).accepted
    reads.reset()
    slots = service.list_released_slots("run-1", "s1", revision)["slots"]
    assert slots[0]["marked_for_review"] is True and slots[0]["version"] == 2
    assert reads.queries == 0


def test_a_replayed_save_changes_nothing_in_the_summary(monkeypatch) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    service = _service(env)
    first = service.save_draft("run-1", "s1", QUESTION, expected_version=0, answer={"text": "x"}, client_epoch=1, client_save_id="a")
    again = service.save_draft("run-1", "s1", QUESTION, expected_version=0, answer={"text": "x"}, client_epoch=1, client_save_id="a")
    assert first.accepted and again.replayed
    assert service.drafts.summaries("run-1", "s1")[QUESTION].version == 1


def test_a_paused_run_stops_saves_immediately_even_though_the_run_is_cached(monkeypatch) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    seed_attempt(env.db, "run-1", "s1")
    service = _service(env)
    assert _save(service).accepted  # the run is cached as RELEASED
    env.service.execute(run_id="run-1", command="pause", expected_version=1, idempotency_key="p1", actor_uid="f1")
    refused = _save(service, version=1, answer={"text": "later"})
    assert not refused.accepted and refused.conflict_reason == "run_paused"


# -- authorization lookups ------------------------------------------------------------------------------------------

def _membership(**overrides) -> Membership:
    base = dict(
        institution_id="inst-1", identity_key="ID1", role_scope="student", status="active", uid="u1",
        contact_email="a@b.co", authorization_version=1, created_at=T0, updated_at=T0,
    )
    base.update(overrides)
    return Membership.model_validate(base)


def test_memberships_are_read_once_and_every_write_clears_them(monkeypatch, reads) -> None:
    db = mem.FakeFirestore()
    repo = MembershipRepository(db)
    repo.upsert(_membership())
    reads.reset()
    assert [m.status.value for m in repo.find_by_uid("u1")] == ["active"]
    assert [m.status.value for m in repo.find_by_uid("u1")] == ["active"]
    assert reads.billed == 1
    repo.upsert(_membership(status="revoked", authorization_version=2))
    assert [m.status.value for m in repo.find_by_uid("u1")] == ["revoked"]


def test_an_unknown_uid_is_remembered_as_having_no_memberships(monkeypatch, reads) -> None:
    db = mem.FakeFirestore()
    repo = MembershipRepository(db)
    reads.reset()
    assert repo.find_by_uid("nobody") == [] and repo.find_by_uid("nobody") == []
    assert reads.billed == 1


def test_the_lockdown_profile_is_read_once_and_a_change_is_visible_at_once(monkeypatch, reads) -> None:
    db = mem.FakeFirestore()
    service = LockdownService(db)
    reads.reset()
    assert service.get_or_default_profile("run-1").enabled is False
    assert service.get_or_default_profile("run-1").enabled is False
    assert reads.billed == 1
    service.configure_profile(
        run_id="run-1", enabled=True, expected_config_key_hash="h", expected_browser_exam_key_hashes=[],
        piloted_platforms=[], updated_by="f1",
    )
    assert service.get_or_default_profile("run-1").enabled is True


def test_a_confirmed_staff_binding_is_not_re_read_but_a_conflict_always_is(monkeypatch, reads) -> None:
    db = mem.FakeFirestore()
    service = StaffBindingService(db)
    assert service.ensure_bound(email="Boss@College.edu", uid="u1", roles=["super_admin"]) == "bound"
    reads.reset()
    assert service.ensure_bound(email="boss@college.edu", uid="u1", roles=["super_admin"]) == "already_bound"
    assert reads.billed == 0
    assert service.ensure_bound(email="boss@college.edu", uid="intruder", roles=["super_admin"]) == "conflict"
    reads.reset()
    assert service.ensure_bound(email="boss@college.edu", uid="intruder", roles=["super_admin"]) == "conflict"
    assert reads.billed == 1  # a different uid is never served from the cache


# -- the faculty grid -----------------------------------------------------------------------------------------------

def _seed_grid(db, uids):
    seed_run(db, enrolled=tuple(uids))
    for uid in uids:
        seed_attempt(db, "run-1", uid)


def test_the_grid_is_read_once_and_then_only_the_changed_rows(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    uids = [f"s{index:02d}" for index in range(8)]
    _seed_grid(env.db, uids)
    reads.reset()
    page, cursor = attempts_view.page_for_run(env.db, "run-1", limit=100)
    assert [a.uid for a in page] == uids and cursor is None
    full_load = reads.billed
    assert full_load == len(uids)

    reads.reset()
    attempts_view.page_for_run(env.db, "run-1", limit=100)
    assert reads.billed == 0  # nothing changed

    ref = mem._DocRef(env.db, ("exam_runs", "run-1", "attempts", "s03"))
    ref.update({"status": AttemptStatus.SUBMITTED.value})
    reads.reset()
    attempts_view.page_for_run(env.db, "run-1", limit=100)
    assert reads.billed == 0  # the change was never published, so the grid has not noticed yet

    attempts_view.mark_dirty("run-1", "s03")
    reads.reset()
    page, _ = attempts_view.page_for_run(env.db, "run-1", limit=100)
    assert reads.billed == 1  # one row, not eight
    assert {a.uid: a.status for a in page}["s03"] == AttemptStatus.SUBMITTED


def test_the_grid_pages_exactly_like_the_paged_query(monkeypatch) -> None:
    env = make_env(monkeypatch)
    uids = [f"s{index:02d}" for index in range(7)]
    _seed_grid(env.db, uids)
    repo = AttemptRepository(env.db)
    for limit, cursor in [(3, None), (3, "s02"), (3, "s05"), (7, None), (10, None), (1, "s06"), (2, "zzz")]:
        mine = attempts_view.page_for_run(env.db, "run-1", limit=limit, cursor_uid=cursor)
        theirs = repo.list_page_for_run("run-1", limit=limit, cursor_uid=cursor)
        assert ([a.uid for a in mine[0]], mine[1]) == ([a.uid for a in theirs[0]], theirs[1]), (limit, cursor)


def test_a_new_attempt_appears_after_it_is_published(monkeypatch) -> None:
    env = make_env(monkeypatch)
    _seed_grid(env.db, ["s1", "s2"])
    assert [a.uid for a in attempts_view.page_for_run(env.db, "run-1", limit=10)[0]] == ["s1", "s2"]
    seed_attempt(env.db, "run-1", "s3")
    attempts_view.mark_dirty("run-1", "s3")
    assert [a.uid for a in attempts_view.page_for_run(env.db, "run-1", limit=10)[0]] == ["s1", "s2", "s3"]


def test_the_grid_re_reads_everything_after_the_resync_interval(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    _seed_grid(env.db, ["s1", "s2", "s3"])
    clock = {"now": 1000.0}
    monkeypatch.setattr(attempts_view.time, "monotonic", lambda: clock["now"])
    attempts_view.page_for_run(env.db, "run-1", limit=10)
    clock["now"] += settings.EXAM_ATTEMPT_VIEW_RESYNC_SECONDS + 1
    reads.reset()
    attempts_view.page_for_run(env.db, "run-1", limit=10)
    assert reads.billed == 3


def test_the_grid_is_a_plain_paged_read_when_the_caches_are_off(monkeypatch, reads) -> None:
    monkeypatch.setattr(settings, "EXAM_READ_CACHE_ENABLED", False)
    env = make_env(monkeypatch)
    _seed_grid(env.db, ["s1", "s2"])
    attempts_view.page_for_run(env.db, "run-1", limit=10)
    reads.reset()
    attempts_view.page_for_run(env.db, "run-1", limit=10)
    assert reads.billed == 2


# -- the finalization sweep -----------------------------------------------------------------------------------------

def test_a_run_with_no_live_attempt_is_not_re_read_until_someone_is_admitted(monkeypatch, reads) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)  # RELEASED, nobody admitted
    env.sweeper.sweep_active_runs_once(batch_size=50)
    reads.reset()
    env.sweeper.sweep_active_runs_once(batch_size=50)
    assert reads.queries == 2  # the two run queries only; no per-run attempt queries

    seed_attempt(env.db, "run-1", "s1", admitted_at=env.clock.now())
    sweep_hints.note_attempt_started("run-1", env.clock.now() + timedelta(minutes=60))
    reads.reset()
    env.sweeper.sweep_active_runs_once(batch_size=50)
    assert reads.billed >= 2  # the run is looked at again
    assert env.sweeper.seconds_until_next_deadline() is not None


def test_only_an_earlier_deadline_wakes_the_sweep(monkeypatch) -> None:
    woken: list[int] = []

    class _Poller:
        name = "finalization"

        def wake(self) -> None:
            woken.append(1)

    monkeypatch.setitem(background._pollers, "finalization", _Poller())
    base = T0 + timedelta(hours=1)
    sweep_hints.note_attempt_started("run-1", base)
    assert len(woken) == 1  # nothing planned yet
    sweep_hints.note_attempt_started("run-1", base + timedelta(seconds=30))
    sweep_hints.note_attempt_started("run-1", base + timedelta(minutes=5))
    assert len(woken) == 1  # later deadlines are already covered
    sweep_hints.note_attempt_started("run-1", base - timedelta(minutes=1))
    assert len(woken) == 2
    sweep_hints.set_planned_deadline(None)  # the sweep found nothing live
    sweep_hints.note_attempt_started("run-2", base)
    assert len(woken) == 3


def test_an_admission_reports_its_deadline_to_the_sweep(monkeypatch) -> None:
    env = make_env(monkeypatch)
    seed_run(env.db)
    service = _service(env)
    service.admit("run-1", "s1")
    assert sweep_hints.drain_woken_runs() == {"run-1"}
    service.admit("run-1", "s1")  # an idempotent replay is not a new live attempt
    assert sweep_hints.drain_woken_runs() == set()


# -- the outbox ------------------------------------------------------------------------------------------------------

def _outbox_entry(entry_id: str, kind: str = "attempt_finalized") -> OutboxEntry:
    return OutboxEntry(id=entry_id, kind=kind, run_id="run-1", payload={}, created_at=T0)


def _queue(db, entries) -> OutboxRepository:
    repo = OutboxRepository(db)
    for entry in entries:
        transaction = db.transaction()
        repo.add_in_transaction(transaction, entry)
        assert transaction.commit()
    return repo


def test_a_backlog_is_claimed_a_page_at_a_time(monkeypatch, reads) -> None:
    repo = _queue(mem.FakeFirestore(), [_outbox_entry(f"e{index:02d}") for index in range(9)])
    reads.reset()
    first = repo.claim_batch(limit=3, lease_seconds=60, worker_id="w", now=T0)
    assert [entry.id for entry in first] == ["e00", "e01", "e02"]
    assert reads.billed == 3  # the rest of the backlog is not read
    for entry in first:
        repo.mark_processed(entry.id, now=T0)
    reads.reset()
    assert [entry.id for entry in repo.claim_batch(limit=3, lease_seconds=60, worker_id="w", now=T0)] == ["e03", "e04", "e05"]
    assert reads.billed == 3


def test_entries_a_loop_must_not_touch_do_not_starve_the_others(monkeypatch) -> None:
    excluded = [_outbox_entry(f"a{index}", kind="kt_ingest") for index in range(6)]
    repo = _queue(mem.FakeFirestore(), excluded + [_outbox_entry("b0"), _outbox_entry("b1")])
    claimed = repo.claim_batch(limit=2, lease_seconds=60, worker_id="w", now=T0, exclude_kinds=("kt_ingest",))
    assert [entry.id for entry in claimed] == ["b0", "b1"]
    assert repo.claim_batch(limit=2, lease_seconds=60, worker_id="w", now=T0, exclude_kinds=("kt_ingest",)) == []  # both are leased now
