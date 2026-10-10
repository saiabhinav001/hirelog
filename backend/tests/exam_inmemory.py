"""In-memory Firestore stand-in with optimistic-concurrency transactions.

Test support for Prompt 3 (ADR 0019) - not a production module and not named
`test_*`, so pytest does not collect it.

It models the one Firestore property the exam commands depend on: a
`@firestore.transactional` callback reads documents, decides, and buffers
writes; at commit the transaction is validated (every document it read must be
unchanged) and, if another writer got there first, the callback is RE-RUN
against the new state. That makes "two commands race for the same
expected_version" and "extension vs. expiry" real interleavings instead of
sequential calls on a passthrough fake.

What it does NOT model (so tests using it say nothing about these): real
network latency, Firestore's lock-based pessimistic reads, index requirements,
security rules, or multi-region contention. Query support is limited to the
operators the exam repositories use.
"""

from __future__ import annotations

import copy
import random
import threading
import time
from typing import Any, Optional

from google.api_core.exceptions import AlreadyExists, NotFound

MAX_TXN_ATTEMPTS = 25


class _Snapshot:
    def __init__(self, ref: "_DocRef", data: Optional[dict]) -> None:
        self.reference = ref
        self.id = ref.id
        self.exists = data is not None
        self._data = copy.deepcopy(data) if data is not None else None

    def to_dict(self) -> Optional[dict]:
        return copy.deepcopy(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, db: "FakeFirestore", path: tuple[str, ...]) -> None:
        self._db = db
        self.path = path
        self.id = path[-1]

    def collection(self, name: str) -> "_CollRef":
        return _CollRef(self._db, self.path + (name,))

    def get(self, transaction: Optional["FakeTransaction"] = None) -> _Snapshot:
        with self._db.lock:
            record = self._db.docs.get(self.path)
            if transaction is not None:
                transaction.reads[self.path] = record["rev"] if record else 0
            snapshot = _Snapshot(self, record["data"] if record else None)
        if transaction is not None and self._db.jitter:
            # Yield between the read and the (later) commit so other threads can
            # commit in the gap - without this the GIL serialises tiny critical
            # sections and the "race" tests would never actually race.
            time.sleep(random.uniform(0, self._db.jitter))
        return snapshot

    def set(self, data: dict, merge: bool = False) -> None:
        self._db.apply([("merge" if merge else "set", self, data)])

    def update(self, data: dict) -> None:
        self._db.apply([("update", self, data)])

    def create(self, data: dict) -> None:
        self._db.apply([("create", self, data)])

    def delete(self) -> None:
        self._db.apply([("delete", self, None)])


def _matches(data: dict, field: str, op: str, value: Any) -> bool:
    if field not in data or data[field] is None:
        return False
    actual = data[field]
    if op == "==":
        return actual == value
    if op == "in":
        return actual in value
    if op == "array_contains":
        return isinstance(actual, list) and value in actual
    if op == ">=":
        return actual >= value
    if op == ">":
        return actual > value
    if op == "<=":
        return actual <= value
    if op == "<":
        return actual < value
    raise NotImplementedError(f"operator {op!r} not supported by the in-memory fake")


class _Query:
    def __init__(self, db: "FakeFirestore", path: tuple[str, ...], filters=None, order=None, limit=None, after=None):
        self._db = db
        self._path = path
        self._filters = list(filters or [])
        self._order = order
        self._limit = limit
        self._after = after

    def where(self, *, filter) -> "_Query":  # noqa: A002 - mirrors the Firestore signature
        return _Query(
            self._db, self._path, self._filters + [(filter.field_path, filter.op_string, filter.value)],
            self._order, self._limit, self._after,
        )

    def order_by(self, field: str) -> "_Query":
        return _Query(self._db, self._path, self._filters, field, self._limit, self._after)

    def limit(self, count: int) -> "_Query":
        return _Query(self._db, self._path, self._filters, self._order, count, self._after)

    def start_after(self, values: dict) -> "_Query":
        return _Query(self._db, self._path, self._filters, self._order, self._limit, values)

    def stream(self):
        with self._db.lock:
            rows = []
            for path, record in self._db.docs.items():
                if len(path) != len(self._path) + 1 or path[:-1] != self._path:
                    continue
                data = record["data"]
                if all(_matches(data, f, op, v) for f, op, v in self._filters):
                    rows.append((path, data))
            if self._order:
                rows.sort(key=lambda item: item[1].get(self._order))
                if self._after is not None:
                    cutoff = self._after[self._order]
                    rows = [item for item in rows if item[1].get(self._order) > cutoff]
            if self._limit is not None:
                rows = rows[: self._limit]
            return [_Snapshot(_DocRef(self._db, path), data) for path, data in rows]


class _CollRef(_Query):
    def __init__(self, db: "FakeFirestore", path: tuple[str, ...]) -> None:
        super().__init__(db, path)

    def document(self, doc_id: Optional[str] = None) -> _DocRef:
        if doc_id is None:
            raise NotImplementedError("auto-ids are not used by the exam repositories")
        return _DocRef(self._db, self._path + (doc_id,))

    def add(self, data: dict) -> tuple[None, _DocRef]:
        """Auto-id insert (used by `record_malformed_candidate`)."""
        import uuid

        ref = _DocRef(self._db, self._path + (uuid.uuid4().hex,))
        ref.set(data)
        return None, ref


class FakeTransaction:
    def __init__(self, db: "FakeFirestore") -> None:
        self._db = db
        self.reads: dict[tuple[str, ...], int] = {}
        self.writes: list[tuple[str, _DocRef, Optional[dict]]] = []

    def reset(self) -> None:
        self.reads = {}
        self.writes = []

    def set(self, ref: _DocRef, data: dict) -> None:
        self.writes.append(("set", ref, copy.deepcopy(data)))

    def update(self, ref: _DocRef, data: dict) -> None:
        self.writes.append(("update", ref, copy.deepcopy(data)))

    def create(self, ref: _DocRef, data: dict) -> None:
        self.writes.append(("create", ref, copy.deepcopy(data)))

    def delete(self, ref: _DocRef) -> None:
        self.writes.append(("delete", ref, None))

    def commit(self) -> bool:
        """True when applied; False when a document this transaction read changed
        (the caller re-runs the callback). Raises AlreadyExists/NotFound without
        applying anything, like Firestore."""
        with self._db.lock:
            for path, rev in self.reads.items():
                record = self._db.docs.get(path)
                if (record["rev"] if record else 0) != rev:
                    return False
            self._db.apply_locked(self.writes)
            return True


class FakeFirestore:
    def __init__(self, jitter: float = 0.002) -> None:
        self.lock = threading.RLock()
        self.jitter = jitter
        self.docs: dict[tuple[str, ...], dict] = {}
        self._rev = 0
        self.commit_conflicts = 0  # how many times a transaction had to be re-run

    # -- Firestore surface -----------------------------------------------------

    def collection(self, name: str) -> _CollRef:
        return _CollRef(self, (name,))

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    # -- internals ---------------------------------------------------------------

    def apply(self, writes) -> None:
        with self.lock:
            self.apply_locked(writes)

    def apply_locked(self, writes) -> None:
        # Validate first so a failing write applies nothing (atomic commit).
        shadow = {path: (record["data"] is not None) for path, record in self.docs.items()}
        for kind, ref, _ in writes:
            exists = shadow.get(ref.path, False)
            if kind == "create":
                if exists:
                    raise AlreadyExists(f"document {'/'.join(ref.path)} already exists")
                shadow[ref.path] = True
            elif kind == "update":
                if not exists:
                    raise NotFound(f"document {'/'.join(ref.path)} not found")
            elif kind in ("set", "merge"):
                shadow[ref.path] = True
            elif kind == "delete":
                shadow[ref.path] = False
        for kind, ref, data in writes:
            self._rev += 1
            if kind == "delete":
                self.docs.pop(ref.path, None)
                continue
            record = self.docs.get(ref.path)
            if kind == "update" or (kind == "merge" and record is not None):
                merged = dict(record["data"])
                merged.update(copy.deepcopy(data))
                self.docs[ref.path] = {"data": merged, "rev": self._rev}
            else:
                self.docs[ref.path] = {"data": copy.deepcopy(data), "rev": self._rev}

    # -- test helpers ----------------------------------------------------------------

    def get(self, *path: str) -> Optional[dict]:
        record = self.docs.get(tuple(path))
        return copy.deepcopy(record["data"]) if record else None

    def put(self, *path_and_data) -> None:
        *path, data = path_and_data
        self.apply([("set", _DocRef(self, tuple(path)), data)])

    def collection_docs(self, *collection_path: str) -> list[dict]:
        return [
            copy.deepcopy(record["data"])
            for path, record in self.docs.items()
            if len(path) == len(collection_path) + 1 and path[:-1] == tuple(collection_path)
        ]


def install_optimistic_transactions(monkeypatch, db: FakeFirestore) -> None:
    """Replace `firestore.transactional` with a retrying runner bound to `db`."""
    from firebase_admin import firestore

    def transactional(fn):
        def wrapper(transaction: FakeTransaction):
            for _ in range(MAX_TXN_ATTEMPTS):
                transaction.reset()
                result = fn(transaction)  # exceptions propagate; nothing was written
                if transaction.commit():
                    return result
                with db.lock:
                    db.commit_conflicts += 1
            raise RuntimeError("in-memory transaction kept aborting")

        return wrapper

    monkeypatch.setattr(firestore, "transactional", transactional)


# -- seeding helpers (kept here so every Prompt 3 test builds state the same way) --

from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def seed_run(db: FakeFirestore, *, run_id: str = "run-1", status=None, version: int = 1, policy=None,
             enrolled=("s1",), institution_id=None, revision_id: str = "rev-1", now: datetime = T0):
    from app.domain.exams.models import ExamRun, RunStatus
    from app.domain.exams.policy import ExamPolicy

    run = ExamRun(
        id=run_id, definition_id="def-1", revision_id=revision_id, status=status or RunStatus.RELEASED,
        version=version, policy=policy or ExamPolicy(duration_minutes=60), enrolled_uids=list(enrolled),
        institution_id=institution_id, created_at=now, updated_at=now,
    )
    db.put("exam_runs", run_id, run.model_dump(mode="json"))
    return run


def seed_attempt(db: FakeFirestore, run_id: str, uid: str, *, status=None, admitted_at: datetime = T0,
                 minutes: int = 60, deadline: Optional[datetime] = None, **extra):
    from app.domain.exams.models import Attempt, AttemptStatus

    attempt = Attempt(
        run_id=run_id, uid=uid, status=status or AttemptStatus.IN_PROGRESS, version=1, epoch=1,
        admitted_at=admitted_at, deadline=deadline or admitted_at + timedelta(minutes=minutes),
        updated_at=admitted_at, **extra,
    )
    db.put("exam_runs", run_id, "attempts", uid, attempt.model_dump(mode="json"))
    return attempt


def audit_kinds(db: FakeFirestore) -> list[str]:
    return sorted(doc["kind"] for doc in db.collection_docs("exam_authz_audit"))


def outbox_kinds(db: FakeFirestore) -> list[str]:
    return sorted(doc["kind"] for doc in db.collection_docs("exam_outbox"))


class Env:
    """db + controllable clock + the real command service and sweeper bound to them."""

    def __init__(self, db, clock, service, sweeper) -> None:
        self.db, self.clock, self.service, self.sweeper = db, clock, service, sweeper


def make_env(monkeypatch, now: datetime = T0) -> Env:
    from app.domain.exams.clock import FixedClock
    from app.domain.exams.commands import ExamCommandService
    from app.domain.exams.finalization import ExamFinalizationSweeper

    db = FakeFirestore()
    install_optimistic_transactions(monkeypatch, db)
    clock = FixedClock(now)
    return Env(db, clock, ExamCommandService(db, clock=clock), ExamFinalizationSweeper(db, clock=clock))
