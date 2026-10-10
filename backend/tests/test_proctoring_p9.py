"""Prompt 9 (ADR 0025): proctoring ledger, review, appeals and evidence - HTTP and service level against the in-memory
Firestore with optimistic transactions. Browser signals are client-REPORTED observations; nothing here claims a browser
can enforce anything, and no test uses a real camera, bucket or identity. Real-storage IAM and real browsers are
Prompt 10/11 evidence."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_faculty as faculty_routes
from app.api.routes import exams_proctoring as proctoring_routes
from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.exams.models import AttemptStatus
from app.domain.exams.policy import ExamPolicy, ProctoringProfile
from app.domain.proctoring.decisions import detect_image_type, effective_proctoring
from app.domain.proctoring.evidence_service import EvidenceService
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage
from app.domain.proctoring.ledger_service import ProctoringLedgerService
from app.domain.proctoring.models import ProctoringState
from app.domain.proctoring.retention import RetentionSweeper
from exam_inmemory import T0, audit_kinds, seed_attempt
from test_exams_student_attempts_p6 import BASE, Q0, RUN, _fresh_attempt, _race, api  # noqa: F401 - fixtures reused

PROCTORING = f"{BASE}/proctoring"
FACULTY = "/api/exams/v1/faculty/proctoring"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32


class P9:
    def __init__(self, fixture, storage: InMemoryEvidenceStorage) -> None:
        self.api, self.env, self.client, self.storage = fixture, fixture.env, fixture.client, storage

    def observe(self, kind: str, event_id: str, at_seconds: float, uid: str | None = None):
        return self.client.post(
            f"{PROCTORING}/runs/{RUN}/observations",
            json={"kind": kind, "client_event_id": event_id, "client_timestamp": (T0 + timedelta(seconds=at_seconds)).isoformat()},
        )

    def depart(self, n: int, *, length: float = 3.0, base: float = 0.0):
        """One departure: tab hidden, then visible again `length` seconds later; departures are 100 s apart."""
        start = base + n * 100
        self.observe("page_hidden", f"hid-{n}", start)
        return self.observe("page_visible", f"vis-{n}", start + length)

    def attempt(self, uid: str = "s1") -> dict:
        return self.api.attempt_doc(uid)

    def state(self, uid: str = "s1") -> dict:
        return self.env.db.get("proctoring_states", f"{RUN}__{uid}") or {}

    def ledger(self, uid: str = "s1") -> list[dict]:
        return sorted(
            (d for d in self.env.db.collection_docs("proctoring_log") if d["uid"] == uid), key=lambda d: d["received_at"],
        )

    def resolve(self, allow: bool, notes: str = "reviewed by teacher", uid: str = "s1"):
        return self.client.post(f"{FACULTY}/runs/{RUN}/uids/{uid}/resolve", json={"allow_continue": allow, "notes": notes})

    def snapshot(self, content: bytes = JPEG, kind: str = "check_in", uid: str = "s1") -> dict:
        requested = self.client.post(f"{PROCTORING}/runs/{RUN}/snapshots", json={"kind": kind})
        assert requested.status_code == 200, requested.text
        body = requested.json()
        uploaded = self.client.post(f"{PROCTORING}/snapshots/upload/{body['upload_ticket']}", files={"file": ("shot.jpg", content, "image/jpeg")})
        return {"snapshot_id": body["snapshot_id"], "response": uploaded}


@pytest.fixture
def p9(api, monkeypatch):  # noqa: F811
    env = api.env
    storage = InMemoryEvidenceStorage()
    monkeypatch.setattr(proctoring_routes, "db", env.db)
    monkeypatch.setattr(proctoring_routes, "_shared_storage", storage)
    monkeypatch.setattr(proctoring_routes, "_observation_limiter", SlidingWindowLimiter(100_000, 60, namespace="p9-obs"))
    monkeypatch.setattr(proctoring_routes, "_snapshot_limiter", SlidingWindowLimiter(100_000, 60, namespace="p9-snap"))
    monkeypatch.setattr(proctoring_routes, "_appeal_limiter", SlidingWindowLimiter(100_000, 60, namespace="p9-appeal"))
    monkeypatch.setattr(settings, "ENV", "development")
    teacher = {"uid": "teacher-1"}
    app = __import__("app.exam_main", fromlist=["app"]).app
    app.dependency_overrides[deps.require_exam_faculty] = lambda: teacher
    app.dependency_overrides[scope.require_run_scope] = lambda run_id: teacher
    app.dependency_overrides[scope.require_snapshot_scope] = lambda snapshot_id: teacher
    app.dependency_overrides[scope.require_proctoring_appeal_scope] = lambda appeal_id: teacher
    return P9(api, storage)


# -- profile ----------------------------------------------------------------------------------------------------------------------

def test_a_run_without_a_profile_gets_exactly_the_deployment_defaults() -> None:
    eff = effective_proctoring(ExamPolicy(duration_minutes=60), default_grace=2.0, default_cooldown=5.0, default_retention_days=90)
    assert (eff.grace_seconds, eff.cooldown_seconds, eff.allowance_after_review, eff.retention_days) == (2.0, 5.0, 0, 90)


def test_a_profile_overrides_only_what_it_sets() -> None:
    policy = ExamPolicy(duration_minutes=60, proctoring_profile=ProctoringProfile(grace_seconds=10, allowance_after_review=2))
    eff = effective_proctoring(policy, default_grace=2.0, default_cooldown=5.0, default_retention_days=90)
    assert (eff.grace_seconds, eff.cooldown_seconds, eff.allowance_after_review, eff.retention_days) == (10, 5.0, 2, 90)


@pytest.mark.parametrize("bad", [{"grace_seconds": -1}, {"grace_seconds": 61}, {"cooldown_seconds": 61}, {"allowance_after_review": 11},
                                 {"evidence_retention_days": 6}, {"evidence_retention_days": 366}, {"unknown_field": 1}])
def test_invalid_profiles_are_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        ProctoringProfile(**bad)


def test_a_policy_stored_before_profiles_existed_reads_unchanged() -> None:
    assert ExamPolicy.model_validate({"duration_minutes": 30, "warning_threshold": 3}).proctoring_profile is None


def test_the_server_not_the_client_stamps_who_set_the_profile(monkeypatch) -> None:
    captured = {}

    class _Service:
        definitions = SimpleNamespace(get_revision=lambda definition_id, revision_id: object())

        def create_run(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(model_dump=lambda mode="json": {"id": "run-x"})

    monkeypatch.setattr(faculty_routes, "authorize_definition", lambda user, definition_id: SimpleNamespace(institution_id=None))
    monkeypatch.setattr(faculty_routes, "validate_enrollment_scope", lambda institution_id, uids: None)
    monkeypatch.setattr(faculty_routes, "_service", lambda: _Service())
    payload = faculty_routes.CreateRunRequest(
        definition_id="d", revision_id="r", enrolled_uids=["s1"],
        policy=ExamPolicy(duration_minutes=60, proctoring_profile=ProctoringProfile(grace_seconds=5, set_by="someone-else")),
    )
    faculty_routes.create_run(payload, user={"uid": "teacher-9"})
    assert captured["policy"].proctoring_profile.set_by == "teacher-9" and captured["policy"].proctoring_profile.grace_seconds == 5


def test_the_run_grace_period_decides_what_counts(p9) -> None:
    run = p9.env.db.get("exam_runs", RUN)
    run["policy"]["proctoring_profile"] = {"grace_seconds": 10.0, "allowance_after_review": 0}
    p9.env.db.put("exam_runs", RUN, run)
    p9.depart(0, length=5)  # 5 s: counted under the deployment default (2 s) but not under this run's 10 s
    assert p9.attempt()["warning_count"] == 0
    p9.depart(1, length=12)
    assert p9.attempt()["warning_count"] == 1


# -- observations: bursts, replay, informational -------------------------------------------------------------------------------------

def test_a_replay_storm_changes_nothing_after_the_first_delivery(p9) -> None:
    for n in range(3):
        assert p9.depart(n).status_code == 200
    assert p9.attempt()["status"] == "blocked" and p9.attempt()["warning_count"] == 3
    entries_before, state_before = len(p9.ledger()), p9.state()

    for _ in range(5):
        for n in range(3):
            assert p9.observe("page_hidden", f"hid-{n}", n * 100).json()["action"] == "deduped"
            assert p9.observe("page_visible", f"vis-{n}", n * 100 + 3).json()["action"] == "deduped"
    assert len(p9.ledger()) == entries_before and p9.state()["counted_incident_count"] == state_before["counted_incident_count"] == 3


def test_the_same_event_delivered_by_many_requests_at_once_counts_once(p9) -> None:
    p9.observe("page_hidden", "storm-open", 0)
    results = _race(*[lambda: p9.observe("page_visible", "storm-close", 3) for _ in range(12)])
    assert all(getattr(r, "status_code", 500) == 200 for r in results)
    assert p9.attempt()["warning_count"] == 1 and p9.state()["counted_incident_count"] == 1
    assert len([e for e in p9.ledger() if e["client_event_id"] == "storm-close"]) == 1


def test_a_flapping_departure_inside_the_cooldown_counts_once(p9) -> None:
    for i in range(10):  # leave, return, leave again 1 s later ... all inside the 5 s cooldown
        p9.observe("page_hidden", f"fl-h{i}", i * 2)
        p9.observe("page_visible", f"fl-v{i}", i * 2 + 1)
    assert p9.attempt()["warning_count"] == 1


@pytest.mark.parametrize("kind", ["permission_denied", "camera_error", "connection_degraded", "blur", "focus", "clipboard_blocked"])
def test_technical_and_informational_signals_never_count_and_never_block(p9, kind) -> None:
    for i in range(40):
        assert p9.observe(kind, f"{kind}-{i}", i).json()["action"] == "informational_logged"
    assert p9.attempt()["status"] == "in_progress" and p9.attempt()["warning_count"] == 0
    assert all(e["counted"] is False for e in p9.ledger())


def test_what_the_student_sees_explains_the_count_and_calls_nothing_proof(p9) -> None:
    p9.depart(0)
    mine = p9.client.get(f"{PROCTORING}/runs/{RUN}/incidents").json()["results"]
    assert any(e["counted"] and e["note"] == "counted departure" for e in mine)
    faculty = p9.client.get(f"{FACULTY}/runs/{RUN}/uids/s1/incidents").json()
    assert "not cryptographic proof" in faculty["client_reported_disclaimer"]


# -- threshold versus submit / expiry ----------------------------------------------------------------------------------------------------

def _ledger_service(p9) -> ProctoringLedgerService:
    return ProctoringLedgerService(p9.env.db, clock=p9.env.clock)


def _prime_one_short(p9, uid: str) -> None:
    """Two counted departures and one still open, so the next 'visible' event is the one that crosses the threshold."""
    state = ProctoringState(
        run_id=RUN, uid=uid, counted_incident_count=2, open_incident_started_at=T0 - timedelta(seconds=30),
        open_incident_last_activity_at=T0 - timedelta(seconds=30), updated_at=T0,
    )
    p9.env.db.put("proctoring_states", f"{RUN}__{uid}", state.model_dump(mode="json"))


def test_a_threshold_crossing_racing_a_submit_has_exactly_one_outcome(p9) -> None:
    service = p9.api.service()
    ledger = _ledger_service(p9)
    seen = set()
    for i in range(20):
        uid = f"race{i}"
        _fresh_attempt(p9.api, uid)
        _prime_one_short(p9, uid)
        results = _race(
            lambda: ledger.record_observation(run_id=RUN, uid=uid, kind=__import__("app.domain.proctoring.models", fromlist=["x"]).ObservationKind.PAGE_VISIBLE,
                                              client_event_id="close", client_timestamp=T0, grace_seconds=2.0, cooldown_seconds=5.0, threshold=3),
            lambda: service.submit(RUN, uid, idempotency_key=f"k{i}", client_epoch=1, expected_draft_versions={}),
        )
        assert not isinstance(results[0], Exception), results[0]  # a late observation is never an error
        doc = p9.attempt(uid)
        seen.add(doc["status"])
        if doc["status"] == "submitted":
            assert results[1].accepted and results[0].action == "ignored_inactive" and len(p9.api.outbox(uid)) == 1
        else:
            assert doc["status"] == "blocked" and not results[1].accepted and results[1].conflict_reason == "attempt_blocked"
            assert len(p9.api.outbox(uid)) == 0
    assert seen <= {"submitted", "blocked"}


def test_events_after_the_attempt_ended_are_recorded_but_never_counted_or_blocking(p9) -> None:
    assert p9.api.submit("k").status_code == 200
    for n in range(4):
        response = p9.depart(n)
        assert response.status_code == 200 and response.json()["action"] == "ignored_inactive"
    assert p9.attempt()["status"] == "submitted" and p9.attempt()["warning_count"] == 0
    late = p9.ledger()
    assert late and all(e["source"] == "server" and e["counted"] is False for e in late)
    # a replay rewrites the same entry instead of adding another
    count = len(late)
    p9.observe("page_hidden", "hid-0", 0)
    assert len(p9.ledger()) == count


def test_the_stale_sweep_survives_an_attempt_that_already_ended(p9) -> None:
    """A student left the tab open, submitted from another window, closed everything. The sweep used to raise on the
    illegal submitted -> blocked transition and never get past that state."""
    ledger = _ledger_service(p9)
    for uid in ("gone", "live"):
        _fresh_attempt(p9.api, uid)
        _prime_one_short(p9, uid)
    p9.api.service().submit(RUN, "gone", idempotency_key="k", client_epoch=1, expected_draft_versions={})
    p9.env.clock.advance(timedelta(seconds=300))

    closed = ledger.sweep_stale_incidents_once(grace_seconds=2.0, stale_after_seconds=120)
    assert closed == 2
    assert p9.attempt("gone")["status"] == "submitted" and p9.state("gone")["counted_incident_count"] == 2
    assert p9.attempt("live")["status"] == "blocked" and p9.state("live")["counted_incident_count"] == 3
    assert ledger.sweep_stale_incidents_once(grace_seconds=2.0, stale_after_seconds=120) == 0


# -- blocked: what stops, what is kept -----------------------------------------------------------------------------------------------------

def _block(p9) -> None:
    assert p9.api.save(Q0, 0, {"selected": "A"}).status_code == 200  # an answer exists before the block
    for n in range(3):
        p9.depart(n)
    assert p9.attempt()["status"] == "blocked"


def test_a_blocked_attempt_stops_writes_and_delivery_but_keeps_everything(p9) -> None:
    _block(p9)
    drafts_before = p9.env.db.get("exam_runs", RUN, "attempts", "s1", "drafts", Q0)
    for response in (p9.api.save(Q0, 1, {"selected": "B"}), p9.api.submit("kb"), p9.client.get(f"{BASE}/runs/{RUN}/question"),
                     p9.client.get(f"{BASE}/runs/{RUN}/slots"), p9.client.post(f"{BASE}/runs/{RUN}/next-question")):
        assert response.status_code in (403, 409), response.text
    assert p9.env.db.get("exam_runs", RUN, "attempts", "s1", "drafts", Q0) == drafts_before  # answers preserved
    assert p9.api.client.get(f"{BASE}/runs/{RUN}/state").json()["status"] == "blocked"
    assert len(p9.client.get(f"{PROCTORING}/runs/{RUN}/incidents").json()["results"]) >= 6  # the student can see why


def test_a_blocked_attempt_still_expires_and_is_sealed_with_what_was_saved(p9) -> None:
    _block(p9)
    ledger_before = p9.ledger()
    p9.env.clock.advance(timedelta(minutes=75))
    p9.env.sweeper.sweep_once(RUN, batch_size=10)
    doc = p9.attempt()
    assert doc["status"] == "expired" and doc["submit_receipt"]["receipt_id"]
    assert doc["submit_receipt"]["sealed_draft_versions"][Q0] == 1
    assert len(p9.api.outbox("s1")) == 1 and p9.state()["counted_incident_count"] == 3
    assert [e["id"] for e in p9.ledger()] == [e["id"] for e in ledger_before]  # sealing did not touch the ledger
    assert p9.resolve(True).status_code == 409  # an expired attempt is not reopened by a review


# -- review: uphold, continue, reversal -------------------------------------------------------------------------------------------------------

def test_a_review_needs_a_reason_and_only_applies_to_a_blocked_attempt(p9) -> None:
    assert p9.resolve(True).status_code == 409 and p9.resolve(True).json()["detail"]["code"] == "attempt_not_blocked"
    _block(p9)
    assert p9.client.post(f"{FACULTY}/runs/{RUN}/uids/s1/resolve", json={"allow_continue": True, "notes": ""}).status_code == 422
    assert p9.client.post(f"{FACULTY}/runs/{RUN}/uids/s1/resolve", json={"allow_continue": True}).status_code == 422


def test_upholding_leaves_the_block_and_records_who_decided(p9) -> None:
    _block(p9)
    assert p9.resolve(False, "telemetry looks genuine").status_code == 200
    assert p9.attempt()["status"] == "blocked"
    last = p9.ledger()[-1]
    assert last["source"] == "reviewer" and last["actor_uid"] == "teacher-1" and "upheld" in last["note"] and last["counted"] is False
    assert audit_kinds(p9.env.db) == ["exam_block_resolved"]


def test_allowing_to_continue_never_resets_the_ledger_and_trivial_blips_do_not_re_block(p9) -> None:
    _block(p9)
    ids_before = {e["id"] for e in p9.ledger()}
    assert p9.resolve(True).status_code == 200
    assert p9.attempt()["status"] == "in_progress" and p9.attempt()["warning_count"] == 3
    assert p9.state()["counted_incident_count"] == 3 and ids_before <= {e["id"] for e in p9.ledger()}  # append-only

    p9.depart(10, length=1.0, base=1000)  # shorter than the grace period: not counted
    assert p9.attempt()["status"] == "in_progress"
    p9.depart(11, base=1000)  # a counted departure: with no allowance this blocks again
    assert p9.attempt()["status"] == "blocked" and p9.state()["counted_incident_count"] == 4


def test_the_runs_allowance_decides_how_many_more_counted_departures_are_tolerated(p9) -> None:
    run = p9.env.db.get("exam_runs", RUN)
    run["policy"]["proctoring_profile"] = {"allowance_after_review": 2}
    p9.env.db.put("exam_runs", RUN, run)
    _block(p9)
    assert p9.resolve(True).status_code == 200
    for n in (10, 11):
        p9.depart(n, base=1000)
        assert p9.attempt()["status"] == "in_progress", n
    p9.depart(12, base=1000)
    assert p9.attempt()["status"] == "blocked" and p9.state()["counted_incident_count"] == 6


def test_a_review_can_be_reversed_and_every_step_is_kept(p9) -> None:
    _block(p9)
    p9.resolve(True, "first look: fine")
    p9.depart(10, base=1000)
    assert p9.attempt()["status"] == "blocked"
    p9.resolve(False, "second look: upheld")
    reviews = [e for e in p9.ledger() if e["source"] == "reviewer"]
    assert [("allowed" in e["note"]) for e in reviews] == [True, False]
    assert audit_kinds(p9.env.db) == ["exam_block_resolved", "exam_block_resolved"]


# -- appeals ---------------------------------------------------------------------------------------------------------------------------------------

def test_an_appeal_is_a_separate_channel_it_never_unblocks_by_itself(p9) -> None:
    _block(p9)
    first = p9.client.post(f"{PROCTORING}/runs/{RUN}/appeals", json={"student_statement": "my connection dropped"})
    again = p9.client.post(f"{PROCTORING}/runs/{RUN}/appeals", json={"student_statement": "please look again"})
    assert first.status_code == 200 and again.json()["id"] == first.json()["id"]
    appeal_id = first.json()["id"]

    overturned = p9.client.post(f"{FACULTY}/appeals/{appeal_id}/resolve", json={"status": "overturned", "resolution_notes": "evidence supports"})
    assert overturned.status_code == 200 and overturned.json()["status"] == "overturned"
    assert p9.attempt()["status"] == "blocked"  # the block is lifted only by the separate review decision
    assert p9.resolve(True, "appeal overturned").status_code == 200 and p9.attempt()["status"] == "in_progress"


def test_an_appeal_decision_cannot_be_set_back_to_pending_and_reversals_keep_history(p9) -> None:
    _block(p9)
    appeal_id = p9.client.post(f"{PROCTORING}/runs/{RUN}/appeals", json={"student_statement": "x"}).json()["id"]
    bad = p9.client.post(f"{FACULTY}/appeals/{appeal_id}/resolve", json={"status": "pending", "resolution_notes": "reset"})
    assert bad.status_code == 422
    p9.client.post(f"{FACULTY}/appeals/{appeal_id}/resolve", json={"status": "upheld", "resolution_notes": "first"})
    final = p9.client.post(f"{FACULTY}/appeals/{appeal_id}/resolve", json={"status": "overturned", "resolution_notes": "second"}).json()
    assert final["version"] == 2 and [d["notes"] for d in final["decisions"]] == ["first", "second"]


def test_appeals_are_rate_limited(p9, monkeypatch) -> None:
    monkeypatch.setattr(proctoring_routes, "_appeal_limiter", SlidingWindowLimiter(2, 60, namespace="p9-appeal-small"))
    codes = [p9.client.post(f"{PROCTORING}/runs/{RUN}/appeals", json={"student_statement": "x"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_an_appeal_can_be_filed_after_the_window_closed(p9) -> None:
    _block(p9)
    p9.env.clock.advance(timedelta(minutes=75))
    p9.env.sweeper.sweep_once(RUN, batch_size=10)
    assert p9.client.post(f"{PROCTORING}/runs/{RUN}/appeals", json={"student_statement": "it was a power cut"}).status_code == 200


# -- evidence ------------------------------------------------------------------------------------------------------------------------------------------

def test_the_bytes_decide_what_an_upload_is_not_the_clients_label() -> None:
    assert (detect_image_type(JPEG), detect_image_type(PNG), detect_image_type(WEBP)) == ("image/jpeg", "image/png", "image/webp")
    for junk in (b"", b"<html><script>alert(1)</script>", b"%PDF-1.7", b"GIF89a", b"\x00" * 64):
        assert detect_image_type(junk) is None


def test_an_upload_that_is_not_an_image_is_refused_and_recorded_as_missing(p9) -> None:
    result = p9.snapshot(b"<html><script>alert(1)</script>")
    assert result["response"].status_code == 409 and result["response"].json()["detail"]["message"] == "not_an_image"
    stored = p9.env.db.get("evidence_snapshots", result["snapshot_id"])
    assert stored["status"] == "missing" and stored["missing_reason"] == "not_an_image" and not p9.storage._objects


@pytest.mark.parametrize("content, media", [(JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp")])
def test_a_verified_image_is_served_with_its_real_type_and_never_sniffed_or_cached(p9, content, media) -> None:
    result = p9.snapshot(content)
    assert result["response"].status_code == 200 and result["response"].json()["content_type"] == media
    ticket = p9.client.post(f"{FACULTY}/snapshots/{result['snapshot_id']}/view-ticket").json()["view_ticket"]
    viewed = p9.client.get(f"{FACULTY}/view/{ticket}")
    assert viewed.status_code == 200 and viewed.content == content
    assert viewed.headers["content-type"] == media and viewed.headers["x-content-type-options"] == "nosniff"
    assert viewed.headers["cache-control"] == "no-store"


def test_unauthorized_evidence_access_is_refused_and_recorded(p9) -> None:
    result = p9.snapshot(JPEG)
    snapshot_id = result["snapshot_id"]
    ticket = p9.client.post(f"{FACULTY}/snapshots/{snapshot_id}/view-ticket").json()["view_ticket"]
    app = __import__("app.exam_main", fromlist=["app"]).app

    app.dependency_overrides[deps.require_exam_faculty] = lambda: {"uid": "someone-else"}
    assert p9.client.get(f"{FACULTY}/view/{ticket}").status_code == 401  # a ticket is bound to the reviewer it was issued to
    app.dependency_overrides[deps.require_exam_faculty] = lambda: {"uid": "teacher-1"}
    assert p9.client.get(f"{FACULTY}/view/{ticket}").status_code == 200
    assert p9.client.get(f"{FACULTY}/view/{ticket}").status_code == 401  # single use
    assert p9.client.get(f"{FACULTY}/view/not-a-ticket").status_code == 401

    log = sorted(p9.env.db.collection_docs("evidence_access_log"), key=lambda d: d["accessed_at"])
    assert sorted((e["accessed_by"], e["outcome"]) for e in log) == sorted([
        ("teacher-1", "issued"), ("someone-else", "denied"), ("teacher-1", "viewed"), ("teacher-1", "denied"),
    ])


def test_an_expired_view_ticket_is_refused(p9, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_EVIDENCE_VIEW_TICKET_TTL_SECONDS", -1)
    snapshot_id = p9.snapshot(JPEG)["snapshot_id"]
    ticket = p9.client.post(f"{FACULTY}/snapshots/{snapshot_id}/view-ticket").json()["view_ticket"]
    assert p9.client.get(f"{FACULTY}/view/{ticket}").status_code == 401


def test_a_ticket_cannot_be_issued_for_evidence_that_was_never_stored(p9) -> None:
    missing = p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots/missing", json={"kind": "check_in", "reason": "permission_denied"}).json()
    assert p9.client.post(f"{FACULTY}/snapshots/{missing['id']}/view-ticket").status_code == 404
    assert p9.client.post(f"{FACULTY}/snapshots/does-not-exist/view-ticket").status_code == 404


def test_another_students_upload_ticket_is_useless(p9) -> None:
    requested = p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots", json={"kind": "check_in"}).json()
    p9.api.as_("s2")
    stolen = p9.client.post(f"{PROCTORING}/snapshots/upload/{requested['upload_ticket']}", files={"file": ("a.jpg", JPEG, "image/jpeg")})
    assert stolen.status_code == 401 and not p9.storage._objects


def test_an_oversized_upload_is_refused_before_it_is_stored(p9, monkeypatch) -> None:
    monkeypatch.setattr(proctoring_routes, "MAX_SNAPSHOT_BYTES", 64)
    result = p9.snapshot(JPEG + b"\x00" * 100)
    assert result["response"].status_code == 413 and not p9.storage._objects


def test_snapshot_requests_and_missing_records_are_rate_limited(p9, monkeypatch) -> None:
    monkeypatch.setattr(proctoring_routes, "_snapshot_limiter", SlidingWindowLimiter(2, 60, namespace="p9-snap-small"))
    codes = [p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots", json={"kind": "periodic"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# -- evidence storage failure never touches the exam (PRC-08) ---------------------------------------------------------------------------------------------

def test_in_production_without_a_bucket_evidence_is_unavailable_not_silently_kept_in_memory(p9, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "EXAM_EVIDENCE_STORAGE_BUCKET", None)
    response = p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots", json={"kind": "check_in"})
    assert response.status_code == 503 and response.json()["detail"]["code"] == "evidence_storage_unavailable"
    assert p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots/missing", json={"kind": "check_in", "reason": "storage_unavailable"}).status_code == 200
    assert not p9.storage._objects


def test_submission_succeeds_when_evidence_cannot_be_stored(p9, monkeypatch) -> None:
    class _Broken(InMemoryEvidenceStorage):
        def put(self, ref, content, *, content_type):
            raise OSError("bucket offline")

    monkeypatch.setattr(proctoring_routes, "_shared_storage", _Broken())
    assert p9.api.save(Q0, 0, {"selected": "A"}).status_code == 200
    result = p9.snapshot(JPEG, kind="submission")
    assert result["response"].status_code == 503 and result["response"].json()["detail"]["code"] == "evidence_storage_unavailable"
    assert p9.env.db.get("evidence_snapshots", result["snapshot_id"])["missing_reason"] == "storage_unavailable"

    submitted = p9.api.submit("k", {Q0: 1})
    assert submitted.status_code == 200 and submitted.json()["receipt"]["receipt_id"]
    assert p9.attempt()["status"] == "submitted"


def test_submission_does_not_depend_on_the_evidence_module_at_all() -> None:
    import app.domain.exams.student_service as student_service

    source = open(student_service.__file__, encoding="utf-8").read()
    assert "proctoring.evidence" not in source and "EvidenceService" not in source


def test_a_missing_snapshot_for_a_denied_camera_is_an_honest_outcome_not_an_error(p9) -> None:
    body = p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots/missing", json={"kind": "check_in", "reason": "permission_denied"}).json()
    assert body["status"] == "missing" and body["missing_reason"] == "permission_denied"
    assert p9.attempt()["status"] == "in_progress"


def test_a_request_that_never_completes_is_marked_missing_not_requested_forever(p9) -> None:
    stale = p9.client.post(f"{PROCTORING}/runs/{RUN}/snapshots", json={"kind": "periodic"}).json()["snapshot_id"]
    done = p9.snapshot(JPEG)["snapshot_id"]
    service = EvidenceService(p9.env.db, p9.storage)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    assert service.expire_stale_requests(now=now, older_than_seconds=300) == 0  # not yet: its ticket may still be used
    assert service.expire_stale_requests(now=now + timedelta(seconds=400), older_than_seconds=300) == 1
    assert p9.env.db.get("evidence_snapshots", stale)["missing_reason"] == "upload_not_completed"
    assert p9.env.db.get("evidence_snapshots", done)["status"] == "verified"


# -- retention -------------------------------------------------------------------------------------------------------------------------------------------

def _aged_snapshots(p9, count: int, *, uid: str = "s1", age_days: int = 100):
    """Verified snapshots whose upload happened `age_days` ago."""
    from datetime import datetime, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    ids = []
    for i in range(count):
        snapshot_id = f"old-{uid}-{age_days}-{i}"
        ref = f"exam-evidence/{RUN}/{uid}/{snapshot_id}"
        p9.storage.put(ref, JPEG, content_type="image/jpeg")
        p9.env.db.put("evidence_snapshots", snapshot_id, {
            "id": snapshot_id, "run_id": RUN, "uid": uid, "kind": "periodic", "status": "verified", "requested_at": stamp,
            "uploaded_at": stamp, "verified_at": stamp, "storage_ref": ref, "sha256": hashlib.sha256(JPEG).hexdigest(),
            "size_bytes": len(JPEG), "content_type": "image/jpeg", "missing_reason": None,
        })
        ids.append(snapshot_id)
    return ids


def _sweeper(p9) -> RetentionSweeper:
    from app.domain.exams.repository import AttemptRepository, ExamRunRepository
    from app.domain.proctoring.repository import AppealRepository

    return RetentionSweeper(
        EvidenceService(p9.env.db, p9.storage), runs=ExamRunRepository(p9.env.db), attempts=AttemptRepository(p9.env.db),
        appeals=AppealRepository(p9.env.db),
    )


def test_retention_deletes_the_bytes_and_the_record_together(p9) -> None:
    ids = _aged_snapshots(p9, 3)
    fresh = p9.snapshot(JPEG)["snapshot_id"]
    assert _sweeper(p9).sweep_once(retention_days=90) == 3
    assert all(p9.env.db.get("evidence_snapshots", i) is None for i in ids)
    assert not any(i in ref for ref in p9.storage._objects for i in ids) and p9.env.db.get("evidence_snapshots", fresh) is not None


def test_retention_reaches_old_evidence_even_when_a_great_many_newer_snapshots_exist(p9) -> None:
    _aged_snapshots(p9, 130, age_days=5)
    old = _aged_snapshots(p9, 2, age_days=200)
    assert _sweeper(p9).sweep_once(retention_days=90, limit=50) == 2
    assert all(p9.env.db.get("evidence_snapshots", i) is None for i in old)


def test_the_runs_own_retention_window_applies(p9) -> None:
    run = p9.env.db.get("exam_runs", RUN)
    run["policy"]["proctoring_profile"] = {"evidence_retention_days": 7}
    p9.env.db.put("exam_runs", RUN, run)
    ids = _aged_snapshots(p9, 2, age_days=10)
    assert _sweeper(p9).sweep_once(retention_days=90) == 2
    assert all(p9.env.db.get("evidence_snapshots", i) is None for i in ids)


def test_evidence_is_held_while_the_attempt_is_blocked_or_an_appeal_is_pending(p9) -> None:
    blocked = _aged_snapshots(p9, 1, uid="s1")
    appealed = _aged_snapshots(p9, 1, uid="s2")
    free = _aged_snapshots(p9, 1, uid="s3")
    _fresh_attempt(p9.api, "s3")
    seed_attempt(p9.env.db, RUN, "s1", status=AttemptStatus.BLOCKED)
    p9.api.as_("s2")
    appeal_id = p9.client.post(f"{PROCTORING}/runs/{RUN}/appeals", json={"student_statement": "x"}).json()["id"]

    assert _sweeper(p9).sweep_once(retention_days=90) == 1
    assert p9.env.db.get("evidence_snapshots", blocked[0]) is not None and p9.env.db.get("evidence_snapshots", appealed[0]) is not None
    assert p9.env.db.get("evidence_snapshots", free[0]) is None

    # once the review is decided the normal window applies again
    seed_attempt(p9.env.db, RUN, "s1", status=AttemptStatus.IN_PROGRESS)
    p9.client.post(f"{FACULTY}/appeals/{appeal_id}/resolve", json={"status": "upheld", "resolution_notes": "decided"})
    assert _sweeper(p9).sweep_once(retention_days=90) == 2


def test_one_snapshot_that_cannot_be_deleted_does_not_stop_the_rest(p9) -> None:
    ids = _aged_snapshots(p9, 3)
    real_delete = p9.storage.delete

    def flaky(ref):
        if ref.endswith(ids[0]):
            raise OSError("bucket error")
        real_delete(ref)

    p9.storage.delete = flaky
    assert _sweeper(p9).sweep_once(retention_days=90) == 2
    assert p9.env.db.get("evidence_snapshots", ids[0]) is not None  # kept, so the next sweep retries it
    p9.storage.delete = real_delete
    assert _sweeper(p9).sweep_once(retention_days=90) == 1


def test_there_are_no_evidence_derivatives_beyond_the_one_stored_object(p9) -> None:
    result = p9.snapshot(JPEG)
    assert [ref for ref in p9.storage._objects] == [f"exam-evidence/{RUN}/s1/{result['snapshot_id']}"]


def test_the_ledger_has_no_delete_or_reset_path() -> None:
    from app.domain.proctoring.repository import IncidentLogRepository, ProctoringStateRepository

    assert not [m for m in dir(IncidentLogRepository) if m.startswith(("delete", "update", "reset", "clear"))]
    assert not [m for m in dir(ProctoringStateRepository) if m.startswith(("delete", "reset", "clear"))]


def test_no_biometric_or_automatic_misconduct_capability_exists() -> None:
    import pathlib

    root = pathlib.Path(proctoring_routes.__file__).resolve().parents[2] / "domain" / "proctoring"
    text = " ".join(p.read_text(encoding="utf-8").lower() for p in root.glob("*.py"))
    for forbidden in ("face_recognition", "facenet", "deepface", "embedding", "liveness_score", "ai_probability", "plagiarism_score"):
        assert forbidden not in text


def test_the_resolve_and_view_routes_stay_behind_the_server_scope_checks() -> None:
    from app.exam_main import app as exam_app

    wanted = {
        ("POST", "/api/exams/v1/faculty/proctoring/runs/{run_id}/uids/{uid}/resolve"): "require_run_scope",
        ("POST", "/api/exams/v1/faculty/proctoring/snapshots/{snapshot_id}/view-ticket"): "require_snapshot_scope",
        ("POST", "/api/exams/v1/faculty/proctoring/appeals/{appeal_id}/resolve"): "require_proctoring_appeal_scope",
    }
    for route in exam_app.routes:
        methods = getattr(route, "methods", None) or set()
        for (method, path), dep in wanted.items():
            if getattr(route, "path", None) == path and method in methods:
                names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
                assert dep in names, (path, names)
                wanted[(method, path)] = None
    assert all(v is None for v in wanted.values())


def test_a_student_sees_the_decision_but_not_the_reviewers_identity_or_private_notes(p9) -> None:
    _block(p9)
    p9.resolve(True, "INTERNAL-NOTE: suspicious but let it go; ask Dr. X")
    mine = p9.client.get(f"{PROCTORING}/runs/{RUN}/incidents").json()["results"]
    decision = [e for e in mine if e["source"] == "reviewer"]
    assert [e["note"] for e in decision] == ["reviewer decision: allowed to continue"]
    assert "INTERNAL-NOTE" not in str(mine) and "teacher-1" not in str(mine) and decision[0]["actor_uid"] is None
    faculty = p9.client.get(f"{FACULTY}/runs/{RUN}/uids/s1/incidents").json()["results"]
    assert any("INTERNAL-NOTE" in e["note"] and e["actor_uid"] == "teacher-1" for e in faculty)