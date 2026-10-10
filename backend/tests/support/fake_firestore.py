"""In-memory stand-in for the slice of the Firestore client the exam code uses.

Faithful enough to drive real repositories/routes end to end (documents,
subcollections, `==`/`array_contains` filters, order/limit/start_after,
transactions) and to COUNT billable operations, which is what the Spark
(free-tier) capacity tests assert on. Not a Firestore emulator: transactions
are serialised by one lock rather than optimistically retried.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections import defaultdict
from typing import Any, Iterable, Optional

from google.cloud.firestore_v1.transforms import Increment, Sentinel


class AlreadyExists(Exception):
    pass


class NotFound(Exception):
    pass


class OpCounter:
    """Billable-operation tally: one read per returned document (minimum one
    per query), one write per set/update/create, one delete per delete."""

    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0
        self.deletes = 0
        self.by_collection: dict[str, dict[str, int]] = defaultdict(lambda: {"reads": 0, "writes": 0, "deletes": 0})

    def read(self, collection: str, count: int = 1) -> None:
        self.reads += count
        self.by_collection[collection]["reads"] += count

    def write(self, collection: str, count: int = 1) -> None:
        self.writes += count
        self.by_collection[collection]["writes"] += count

    def delete(self, collection: str, count: int = 1) -> None:
        self.deletes += count
        self.by_collection[collection]["deletes"] += count

    def reset(self) -> None:
        self.reads = self.writes = self.deletes = 0
        self.by_collection.clear()

    def snapshot(self) -> dict[str, int]:
        return {"reads": self.reads, "writes": self.writes, "deletes": self.deletes}


class FakeSnapshot:
    def __init__(self, ref: "FakeDocumentReference", data: Optional[dict]) -> None:
        self.reference = ref
        self.id = ref.id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field_path: str) -> Any:
        return (self._data or {}).get(field_path)


def _apply_write(existing: Optional[dict], data: dict, *, merge: bool) -> dict:
    base = copy.deepcopy(existing) if (merge and existing is not None) else {}
    for key, value in data.items():
        if isinstance(value, Increment):
            current = base.get(key, 0) or 0
            base[key] = current + value.value
        elif isinstance(value, Sentinel):
            base[key] = "SERVER_TIMESTAMP"
        else:
            base[key] = copy.deepcopy(value)
    return base


class FakeDocumentReference:
    def __init__(self, db: "FakeFirestore", path: tuple[str, ...]) -> None:
        self._db = db
        self._path = path

    @property
    def id(self) -> str:
        return self._path[-1]

    @property
    def path(self) -> str:
        return "/".join(self._path)

    @property
    def parent(self) -> "FakeCollectionReference":
        return FakeCollectionReference(self._db, self._path[:-1])

    def collection(self, name: str) -> "FakeCollectionReference":
        return FakeCollectionReference(self._db, self._path + (name,))

    def get(self, transaction: Any = None, **_: Any) -> FakeSnapshot:
        self._db.counter.read(self._path[0])
        return FakeSnapshot(self, self._db._docs.get(self._path))

    def set(self, data: dict, merge: bool = False) -> None:
        self._db._set(self._path, data, merge=merge)

    def update(self, data: dict) -> None:
        self._db._update(self._path, data)

    def create(self, data: dict) -> None:
        self._db._create(self._path, data)

    def delete(self) -> None:
        self._db._delete(self._path)


class FakeQuery:
    def __init__(self, db: "FakeFirestore", collection_path: tuple[str, ...]) -> None:
        self._db = db
        self._collection_path = collection_path
        self._filters: list[tuple[str, str, Any]] = []
        self._order: Optional[str] = None
        self._descending = False
        self._limit: Optional[int] = None
        self._start_after: Optional[Any] = None

    def _clone(self) -> "FakeQuery":
        clone = FakeQuery(self._db, self._collection_path)
        clone._filters = list(self._filters)
        clone._order, clone._descending = self._order, self._descending
        clone._limit, clone._start_after = self._limit, self._start_after
        return clone

    def where(self, *args: Any, filter: Any = None) -> "FakeQuery":  # noqa: A002 - mirrors the SDK keyword
        clone = self._clone()
        if filter is not None:
            clone._filters.append((filter.field_path, filter.op_string, filter.value))
        else:
            clone._filters.append((args[0], args[1], args[2]))
        return clone

    def order_by(self, field_path: str, direction: Any = None) -> "FakeQuery":
        clone = self._clone()
        clone._order = field_path
        clone._descending = bool(direction) and str(direction).upper().startswith("DESC")
        return clone

    def limit(self, count: int) -> "FakeQuery":
        clone = self._clone()
        clone._limit = count
        return clone

    def start_after(self, cursor: dict) -> "FakeQuery":
        clone = self._clone()
        clone._start_after = cursor
        return clone

    @staticmethod
    def _matches(doc: dict, field: str, op: str, value: Any) -> bool:
        actual = doc.get(field)
        if op == "==":
            return actual == value
        if op == "!=":
            return actual != value
        if op == "array_contains":
            return isinstance(actual, list) and value in actual
        if op == "in":
            return actual in value
        if op == "array_contains_any":
            return isinstance(actual, list) and any(v in actual for v in value)
        if actual is None:
            return False
        return {"<": actual < value, "<=": actual <= value, ">": actual > value, ">=": actual >= value}[op]

    def _run(self) -> list[FakeSnapshot]:
        depth = len(self._collection_path)
        rows = [
            (path, data) for path, data in self._db._docs.items()
            if len(path) == depth + 1 and path[:depth] == self._collection_path
        ]
        for field, op, value in self._filters:
            rows = [(p, d) for p, d in rows if self._matches(d, field, op, value)]
        if self._order:
            rows.sort(key=lambda item: item[1].get(self._order), reverse=self._descending)
            if self._start_after is not None:
                cutoff = self._start_after.get(self._order) if isinstance(self._start_after, dict) else self._start_after
                rows = [(p, d) for p, d in rows if (d.get(self._order) < cutoff if self._descending else d.get(self._order) > cutoff)]
        else:
            rows.sort(key=lambda item: item[0])
        if self._limit is not None:
            rows = rows[: self._limit]
        return [FakeSnapshot(FakeDocumentReference(self._db, p), copy.deepcopy(d)) for p, d in rows]

    def stream(self, transaction: Any = None, **_: Any) -> Iterable[FakeSnapshot]:
        results = self._run()
        self._db.counter.read(self._collection_path[0], max(1, len(results)))
        return iter(results)

    def get(self, transaction: Any = None, **_: Any) -> list[FakeSnapshot]:
        return list(self.stream(transaction))


class FakeCollectionReference(FakeQuery):
    def document(self, doc_id: Optional[str] = None) -> FakeDocumentReference:
        return FakeDocumentReference(self._db, self._collection_path + (doc_id or uuid.uuid4().hex,))

    def add(self, data: dict) -> tuple[Any, FakeDocumentReference]:
        ref = self.document()
        ref.set(data)
        return None, ref

    @property
    def id(self) -> str:
        return self._collection_path[-1]


class FakeTransaction:
    def __init__(self, db: "FakeFirestore") -> None:
        self._db = db
        self._ops: list[tuple[str, FakeDocumentReference, Optional[dict], bool]] = []

    def set(self, ref: FakeDocumentReference, data: dict, merge: bool = False) -> None:
        self._ops.append(("set", ref, data, merge))

    def update(self, ref: FakeDocumentReference, data: dict) -> None:
        self._ops.append(("update", ref, data, False))

    def create(self, ref: FakeDocumentReference, data: dict) -> None:
        self._ops.append(("create", ref, data, False))

    def delete(self, ref: FakeDocumentReference) -> None:
        self._ops.append(("delete", ref, None, False))

    def _commit(self) -> None:
        for kind, ref, data, merge in self._ops:
            if kind == "set":
                ref.set(data, merge=merge)
            elif kind == "update":
                ref.update(data)
            elif kind == "create":
                ref.create(data)
            else:
                ref.delete()
        self._ops.clear()


class FakeWriteBatch(FakeTransaction):
    def commit(self) -> None:
        self._commit()


class FakeFirestore:
    def __init__(self) -> None:
        self._docs: dict[tuple[str, ...], dict] = {}
        self.counter = OpCounter()
        self.lock = threading.RLock()

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, (name,))

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    def batch(self) -> FakeWriteBatch:
        return FakeWriteBatch(self)

    # ── internals shared by refs/transactions ──────────────────────────────
    def _set(self, path: tuple[str, ...], data: dict, *, merge: bool) -> None:
        with self.lock:
            self.counter.write(path[0])
            self._docs[path] = _apply_write(self._docs.get(path), data, merge=merge)

    def _update(self, path: tuple[str, ...], data: dict) -> None:
        with self.lock:
            if path not in self._docs:
                raise NotFound(f"no document to update: {'/'.join(path)}")
            self.counter.write(path[0])
            self._docs[path] = _apply_write(self._docs[path], data, merge=True)

    def _create(self, path: tuple[str, ...], data: dict) -> None:
        with self.lock:
            if path in self._docs:
                raise AlreadyExists(f"document exists: {'/'.join(path)}")
            self.counter.write(path[0])
            self._docs[path] = _apply_write(None, data, merge=False)

    def _delete(self, path: tuple[str, ...]) -> None:
        with self.lock:
            self.counter.delete(path[0])
            self._docs.pop(path, None)

    # ── test conveniences ──────────────────────────────────────────────────
    def seed(self, path: str, data: dict) -> None:
        """Insert a document without touching the op counter."""
        self._docs[tuple(path.split("/"))] = copy.deepcopy(data)

    def raw(self, path: str) -> Optional[dict]:
        return copy.deepcopy(self._docs.get(tuple(path.split("/"))))

    def top_level_collections(self) -> set[str]:
        return {path[0] for path in self._docs}


def fake_transactional(to_wrap):
    """Stand-in for `firestore.transactional`: runs the callback under the DB
    lock and commits the buffered writes if it returns normally."""

    def runner(transaction: FakeTransaction, *args: Any, **kwargs: Any):
        with transaction._db.lock:
            transaction._ops.clear()
            result = to_wrap(transaction, *args, **kwargs)
            transaction._commit()
            return result

    return runner
