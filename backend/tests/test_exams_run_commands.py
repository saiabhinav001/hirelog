"""Prompt 3 (ADR 0019): run lifecycle commands, immutable revisions and
publication against the in-memory Firestore with optimistic transactions
(`exam_inmemory.py`). Covers illegal transitions, stale versions, duplicate
retries, real thread races, immutable revisions and pinned runs."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from google.api_core.exceptions import AlreadyExists

from app.core.config import settings
from app.domain.exams.errors import (
    CommandRejectedError,
    IllegalTransitionError,
    PublicationBlockedError,
    UnresolvedAttemptsError,
    VersionConflictError,
)
from app.domain.exams.models import ExamRevision, RunStatus
from app.domain.exams.policy import AccommodationWindow, ExamPolicy
from app.domain.generation.models import (
    BlueprintSlot,
    CandidateStatus,
    ExamBlueprint,
    GenerationCandidate,
    ItemType,
    SourceAnchorRef,
)
from exam_inmemory import T0, audit_kinds, make_env, outbox_kinds, seed_attempt, seed_run

DEF_ID = "def-1"


@pytest.fixture
def env(monkeypatch):
    return make_env(monkeypatch)


def _run_doc(env, run_id="run-1"):
    return env.db.get("exam_runs", run_id)


def _execute(env, command, *, run_id="run-1", version=1, key="k1", **kw):
    return env.service.execute(run_id=run_id, command=command, expected_version=version, idempotency_key=key, **kw)


# -- lifecycle: legality, versions, receipts, audit ------------------------------------

def test_release_applies_once_and_records_receipt_actor_reason_outbox_and_audit(env) -> None:
    seed_run(env.db, status=RunStatus.PUBLISHED)
    outcome = _execute(env, "release", actor_uid="teacher-1", reason="exam hall open")

    assert outcome.applied and outcome.receipt.resulting_version == 2
    doc = _run_doc(env)
    assert doc["status"] == "released" and doc["version"] == 2 and doc["released_at"]

    receipt = env.db.get("exam_command_receipts", "run-1__release__k1")
    assert receipt["actor_uid"] == "teacher-1" and receipt["reason"] == "exam hall open"
    assert outbox_kinds(env.db) == ["run_command"]
    assert audit_kinds(env.db) == ["exam_run_command"]
    event = env.db.collection_docs("exam_authz_audit")[0]
    assert event["actor_uid"] == "teacher-1"
    assert event["detail"]["from_status"] == "published" and event["detail"]["to_status"] == "released"
    assert event["detail"]["reason"] == "exam hall open"


def test_a_receipt_without_actor_or_reason_stays_legacy_shaped(env) -> None:
    seed_run(env.db, status=RunStatus.PUBLISHED)
    _execute(env, "release")
    receipt = env.db.get("exam_command_receipts", "run-1__release__k1")
    assert "actor_uid" not in receipt and "reason" not in receipt


def test_duplicate_retry_with_the_same_key_applies_exactly_once(env) -> None:
    seed_run(env.db, status=RunStatus.PUBLISHED)
    first = _execute(env, "release")
    again = _execute(env, "release")  # same expected_version, same key: a client retry

    assert first.applied and not again.applied and again.reused_existing_receipt
    assert again.receipt.resulting_version == first.receipt.resulting_version == 2
    assert _run_doc(env)["version"] == 2
    assert outbox_kinds(env.db) == ["run_command"] and audit_kinds(env.db) == ["exam_run_command"]


def test_a_stale_expected_version_is_a_conflict_and_changes_nothing(env) -> None:
    seed_run(env.db, status=RunStatus.PUBLISHED)
    _execute(env, "release", key="a")
    with pytest.raises(VersionConflictError) as caught:
        _execute(env, "pause", version=1, key="b")  # the caller still thinks version == 1
    assert (caught.value.expected, caught.value.actual) == (1, 2)
    assert _run_doc(env)["status"] == "released" and _run_doc(env)["version"] == 2
    assert env.db.get("exam_command_receipts", "run-1__pause__b") is None


def test_a_different_key_for_the_same_command_does_not_double_apply(env) -> None:
    seed_run(env.db, status=RunStatus.RELEASED)
    _execute(env, "pause", key="a")
    with pytest.raises(VersionConflictError):
        _execute(env, "pause", key="b")  # a second human pressing Pause at the same version
    assert _run_doc(env)["version"] == 2


ILLEGAL = [
    (RunStatus.DRAFT, "release"), (RunStatus.DRAFT, "pause"), (RunStatus.DRAFT, "close"), (RunStatus.DRAFT, "end"),
    (RunStatus.PUBLISHED, "pause"), (RunStatus.PUBLISHED, "close"), (RunStatus.PUBLISHED, "end"),
    (RunStatus.PUBLISHED, "publish"), (RunStatus.RELEASED, "publish"), (RunStatus.RELEASED, "end"),
    (RunStatus.PAUSED, "pause"), (RunStatus.PAUSED, "end"), (RunStatus.CLOSED, "release"),
    (RunStatus.CLOSED, "pause"), (RunStatus.CLOSED, "close"), (RunStatus.CLOSED, "resume"),
    (RunStatus.ENDED, "release"), (RunStatus.ENDED, "pause"), (RunStatus.ENDED, "close"),
    (RunStatus.ENDED, "end"), (RunStatus.ENDED, "resume"), (RunStatus.ENDED, "publish"),
]


@pytest.mark.parametrize("status,command", ILLEGAL)
def test_illegal_transitions_are_rejected_without_side_effects(env, status, command) -> None:
    seed_run(env.db, status=status)
    with pytest.raises(IllegalTransitionError):
        _execute(env, command)
    assert _run_doc(env)["status"] == status.value and _run_doc(env)["version"] == 1
    assert env.db.collection_docs("exam_command_receipts") == []
    assert outbox_kinds(env.db) == [] and audit_kinds(env.db) == []


def test_an_unknown_command_is_a_value_error_not_a_state_change(env) -> None:
    seed_run(env.db, status=RunStatus.RELEASED)
    with pytest.raises(ValueError):
        _execute(env, "explode")
    assert _run_doc(env)["version"] == 1


def test_a_run_that_does_not_exist_is_a_lookup_error(env) -> None:
    with pytest.raises(LookupError):
        _execute(env, "release", run_id="missing")


def test_ended_is_terminal_for_every_command(env) -> None:
    seed_run(env.db, status=RunStatus.RELEASED)
    for step, command in enumerate(["pause", "resume", "pause", "close"], start=1):
        _execute(env, command, version=step, key=f"s{step}", force=True)
    _execute(env, "end", version=5, key="s5")
    assert _run_doc(env)["status"] == "ended"
    for command in ("release", "pause", "resume", "close", "end", "publish"):
        with pytest.raises(IllegalTransitionError):
            _execute(env, command, version=6, key=f"after-{command}")


# -- real races ---------------------------------------------------------------------

def _race(workers) -> list:
    barrier = threading.Barrier(len(workers))
    results: list = [None] * len(workers)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - the test inspects the type
            results[i] = ("err", exc)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(workers)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert all(r is not None for r in results), "a worker never finished"
    return results


def test_concurrent_commands_at_one_version_apply_exactly_one(env) -> None:
    seed_run(env.db, status=RunStatus.RELEASED)
    commands = ["pause", "close"] * 4  # eight faculty clients, all holding version 1
    results = _race([
        (lambda c=c, i=i: _execute(env, c, key=f"key-{i}", force=True)) for i, c in enumerate(commands)
    ])
    applied = [r for r in results if r[0] == "ok" and r[1].applied]
    conflicts = [r for r in results if r[0] == "err" and isinstance(r[1], (VersionConflictError, IllegalTransitionError))]
    assert len(applied) == 1 and len(conflicts) == 7
    assert _run_doc(env)["version"] == 2
    assert len(env.db.collection_docs("exam_command_receipts")) == 1
    assert outbox_kinds(env.db) == ["run_command"] and audit_kinds(env.db) == ["exam_run_command"]
    assert env.db.commit_conflicts > 0  # the clients really did collide and re-run


def test_concurrent_duplicate_retries_of_one_key_apply_once_and_all_see_the_same_receipt(env) -> None:
    seed_run(env.db, status=RunStatus.RELEASED)
    results = _race([(lambda: _execute(env, "pause", key="same")) for _ in range(8)])
    assert all(r[0] == "ok" for r in results)
    assert sum(1 for r in results if r[1].applied) == 1
    assert {r[1].receipt.resulting_version for r in results} == {2}
    assert _run_doc(env)["version"] == 2 and len(outbox_kinds(env.db)) == 1


# -- closing a cohort never cuts off accommodated students -----------------------------

def test_close_needs_force_while_an_accommodated_student_is_still_inside_their_deadline(env) -> None:
    policy = ExamPolicy(duration_minutes=60, accommodations={"acc": AccommodationWindow(extra_minutes=30)})
    seed_run(env.db, status=RunStatus.RELEASED, policy=policy, enrolled=("acc", "std"))
    seed_attempt(env.db, "run-1", "std", minutes=60)
    seed_attempt(env.db, "run-1", "acc", minutes=90, accommodation_extra_minutes=30)
    env.clock.set(T0 + timedelta(minutes=70))  # std is past 60, acc still has 20 minutes

    with pytest.raises(UnresolvedAttemptsError) as caught:
        _execute(env, "close")
    assert caught.value.count == 1  # only the accommodated student; std is the sweeper's job

    _execute(env, "close", force=True, key="forced", reason="room needed")
    # close stops new admissions only: the accommodated attempt keeps its own deadline...
    assert env.db.get("exam_runs", "run-1", "attempts", "acc")["status"] == "in_progress"
    # ...and a sweep right after the close still leaves it alone while sealing the expired one.
    assert env.sweeper.sweep_once("run-1", batch_size=50) == 1
    assert env.db.get("exam_runs", "run-1", "attempts", "std")["status"] == "expired"
    assert env.db.get("exam_runs", "run-1", "attempts", "acc")["status"] == "in_progress"
    forced = [e for e in env.db.collection_docs("exam_authz_audit") if e["kind"] == "exam_run_command"][0]
    assert forced["detail"]["forced"] is True and forced["detail"]["reason"] == "room needed"


def test_close_without_force_is_fine_once_every_attempt_is_past_its_own_deadline(env) -> None:
    seed_run(env.db, status=RunStatus.RELEASED)
    seed_attempt(env.db, "run-1", "s1", minutes=60)
    env.clock.set(T0 + timedelta(minutes=61))
    assert _execute(env, "close").applied


# -- definitions, revisions, runs -------------------------------------------------------

def _definition(env, institution_id=None):
    return env.service.create_definition(title="Midterm", owner_uid="t1", institution_id=institution_id)


def test_legacy_publish_stores_the_same_revision_shape_and_audits_it(env) -> None:
    definition = _definition(env)
    revision = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1")
    stored = env.db.get("exam_definitions", definition.id, "revisions", revision.id)
    assert stored["slot_count"] == 3 and stored["revision_number"] == 1
    assert not {"blueprint_id", "total_marks", "slots", "publish_reason"} & set(stored)
    assert env.db.get("exam_definitions", definition.id)["latest_published_revision_id"] == revision.id
    assert audit_kinds(env.db) == ["exam_revision_published"]


def test_publish_requires_a_slot_source(env) -> None:
    definition = _definition(env)
    with pytest.raises(ValueError):
        env.service.publish_revision(definition_id=definition.id, published_by="t1")


def test_publish_of_an_unknown_definition_is_a_lookup_error(env) -> None:
    with pytest.raises(LookupError):
        env.service.publish_revision(definition_id="nope", slot_count=1, published_by="t1")


def test_a_published_edit_creates_a_new_revision_and_leaves_the_old_one_untouched(env) -> None:
    definition = _definition(env)
    first = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1")
    before = env.db.get("exam_definitions", definition.id, "revisions", first.id)
    env.clock.advance(timedelta(hours=1))
    second = env.service.publish_revision(definition_id=definition.id, slot_count=5, published_by="t1", reason="added Q4-5")

    assert second.revision_number == 2 and second.id != first.id and second.publish_reason == "added Q4-5"
    assert env.db.get("exam_definitions", definition.id, "revisions", first.id) == before  # byte-for-byte
    assert env.db.get("exam_definitions", definition.id)["latest_published_revision_id"] == second.id


def test_an_existing_revision_can_never_be_overwritten(env) -> None:
    definition = _definition(env)
    revision = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1")
    tampered = ExamRevision(**{**revision.model_dump(), "slot_count": 99})
    transaction = env.db.transaction()
    env.service.definitions.create_revision_in_transaction(transaction, tampered)
    with pytest.raises(AlreadyExists):
        transaction.commit()
    assert env.db.get("exam_definitions", definition.id, "revisions", revision.id)["slot_count"] == 3


def test_a_run_stays_pinned_to_its_revision_when_a_newer_one_is_published(env) -> None:
    definition = _definition(env)
    first = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1")
    run = env.service.create_run(
        definition_id=definition.id, revision=first, policy=ExamPolicy(duration_minutes=60),
        enrolled_uids=["s1", "s1", "s2"], actor_uid="t1",
    )
    second = env.service.publish_revision(definition_id=definition.id, slot_count=9, published_by="t1")

    assert run.enrolled_uids == ["s1", "s2"]  # de-duplicated, order preserved
    stored = env.service.runs.get(run.id)
    assert stored.revision_id == first.id != second.id
    pinned = env.service.definitions.get_revision(definition.id, stored.revision_id)
    assert pinned.slot_count == 3
    assert "exam_run_created" in audit_kinds(env.db)


def test_publish_is_idempotent_for_a_retried_request_key(env) -> None:
    definition = _definition(env)
    a = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1", idempotency_key="pub-1")
    env.clock.advance(timedelta(minutes=5))
    b = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1", idempotency_key="pub-1")
    c = env.service.publish_revision(definition_id=definition.id, slot_count=3, published_by="t1", idempotency_key="pub-2")

    assert a.id == b.id and a.revision_number == b.revision_number == 1
    assert c.revision_number == 2
    assert len(env.db.collection_docs("exam_authz_audit")) == 2  # the replay wrote nothing
    assert len(env.db.collection("exam_definitions").document(definition.id).collection("revisions").stream()) == 2


def test_concurrent_publishes_get_distinct_consecutive_revision_numbers(env) -> None:
    definition = _definition(env)
    results = _race([
        (lambda i=i: env.service.publish_revision(definition_id=definition.id, slot_count=2, published_by="t1"))
        for i in range(6)
    ])
    assert all(r[0] == "ok" for r in results)
    assert sorted(r[1].revision_number for r in results) == [1, 2, 3, 4, 5, 6]


# -- publication from a blueprint ---------------------------------------------------------

def _seed_blueprint(env, *, definition_id=DEF_ID, slots=None, bp_id="bp1"):
    slots = slots or [
        BlueprintSlot(slot_index=0, item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy",
                      marks=2, target_minutes=5, requested_variant_count=1),
        BlueprintSlot(slot_index=1, item_type=ItemType.CODING, objective_ids=["o2"], difficulty="hard", marks=8,
                      target_minutes=25, permitted_languages=["python3.11"], requested_variant_count=1),
    ]
    blueprint = ExamBlueprint(id=bp_id, exam_definition_id=definition_id, title="Midterm", slots=slots,
                              created_by="t1", created_at=T0, updated_at=T0)
    env.db.put("exam_blueprints", bp_id, blueprint.model_dump(mode="json"))
    return blueprint


def _seed_candidate(env, cid, slot_index, *, status=CandidateStatus.APPROVED, bp_id="bp1", source="src1"):
    candidate = GenerationCandidate(
        id=cid, request_id="r", blueprint_id=bp_id, slot_index=slot_index, family_id="f", local_candidate_id=cid,
        item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy", marks=2, estimated_minutes=5,
        uniqueness_signature=cid, public_prompt="p", status=status, created_at=T0,
        supporting_anchors=[SourceAnchorRef(source_id=source, source_version=1, anchor_id=f"anc-{cid}")],
    )
    env.db.put("generation_candidates", cid, candidate.model_dump(mode="json"))


def test_publishing_from_a_blueprint_freezes_counts_marks_and_approved_candidates(env) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id=definition.id)
    _seed_candidate(env, "c1", 0)
    _seed_candidate(env, "c2", 0)
    _seed_candidate(env, "c3", 1)
    _seed_candidate(env, "c9", 1, status=CandidateStatus.REJECTED)

    revision = env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", published_by="t1")

    assert revision.slot_count == 2 and revision.total_marks == 10 and revision.blueprint_id == "bp1"
    assert [s.approved_candidate_ids for s in revision.slots] == [["c1", "c2"], ["c3"]]
    assert revision.slots[0].source_refs == ["src1:1:anc-c1", "src1:1:anc-c2"]


def test_later_candidate_changes_never_alter_a_published_revision(env) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id=definition.id)
    for cid, idx in (("c1", 0), ("c3", 1)):
        _seed_candidate(env, cid, idx)
    revision = env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", published_by="t1")
    before = env.db.get("exam_definitions", definition.id, "revisions", revision.id)

    _seed_candidate(env, "c1", 0, status=CandidateStatus.REJECTED)  # faculty later rejects it
    _seed_candidate(env, "c4", 1)  # and approves a new one

    assert env.db.get("exam_definitions", definition.id, "revisions", revision.id) == before


def test_blockers_refuse_the_publish_and_write_nothing(env) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id=definition.id)
    _seed_candidate(env, "c1", 0)  # slot 1 has no approved candidate
    with pytest.raises(PublicationBlockedError) as caught:
        env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", published_by="t1")
    assert any("slot 1" in b for b in caught.value.blockers)
    assert env.db.collection_docs("exam_authz_audit") == []
    assert env.db.get("exam_definitions", definition.id)["latest_published_revision_id"] is None


def test_a_client_slot_count_that_disagrees_with_the_blueprint_is_rejected(env) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id=definition.id)
    for cid, idx in (("c1", 0), ("c3", 1)):
        _seed_candidate(env, cid, idx)
    with pytest.raises(CommandRejectedError) as caught:
        env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", slot_count=7, published_by="t1")
    assert caught.value.reason == "slot_count_mismatch"


def test_a_blueprint_of_another_definition_cannot_be_published_here(env) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id="someone-elses-definition")
    with pytest.raises(CommandRejectedError) as caught:
        env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", published_by="t1")
    assert caught.value.reason == "blueprint_definition_mismatch"
    with pytest.raises(LookupError):
        env.service.preview_publication(definition_id=definition.id, blueprint_id="missing")


def test_negative_marks_block_publication_unless_the_flag_is_on(env, monkeypatch) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id=definition.id, slots=[
        BlueprintSlot(slot_index=0, item_type=ItemType.SINGLE_CHOICE, objective_ids=["o1"], difficulty="easy",
                      marks=2, negative_marks=0.5, target_minutes=5, requested_variant_count=1),
    ])
    _seed_candidate(env, "c1", 0)
    monkeypatch.setattr(settings, "EXAM_NEGATIVE_MARKING_ENABLED", False)
    with pytest.raises(PublicationBlockedError):
        env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", published_by="t1")
    monkeypatch.setattr(settings, "EXAM_NEGATIVE_MARKING_ENABLED", True)
    revision = env.service.publish_revision(definition_id=definition.id, blueprint_id="bp1", published_by="t1")
    assert revision.slots[0].negative_marks == 0.5


def test_preview_reports_the_resolved_numbers_without_writing(env) -> None:
    definition = _definition(env)
    _seed_blueprint(env, definition_id=definition.id)
    _seed_candidate(env, "c1", 0)
    _, _, preview = env.service.preview_publication(definition_id=definition.id, blueprint_id="bp1", duration_minutes=20)
    assert preview.total_marks == 10 and preview.counts_by_type == {"coding": 1, "single_choice": 1}
    assert not preview.publishable  # slot 1 has no approved candidate
    assert any("exceeds the exam duration" in w for w in preview.warnings)
    assert env.db.collection_docs("exam_authz_audit") == []
