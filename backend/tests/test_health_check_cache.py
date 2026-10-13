"""The Firestore health probe costs a read, so a recent "ok" is reused for two minutes; errors and deep checks always read."""

from __future__ import annotations

import time
import types

import pytest

from app.core import health_checks


class _Reads:
    def __init__(self) -> None:
        self.count = 0
        self.fail = False

    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self):
        self.count += 1
        if self.fail:
            raise RuntimeError("firestore unavailable")


@pytest.fixture
def firestore(monkeypatch):
    fake = _Reads()
    monkeypatch.setattr(health_checks, "db", fake)
    monkeypatch.setattr(health_checks, "_ok_cache", None)
    clock = {"now": 1000.0}
    monkeypatch.setattr(health_checks, "time", types.SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter))
    fake.clock = clock
    return fake


def test_a_recent_ok_is_reused(firestore) -> None:
    assert health_checks.firestore_health_check()["status"] == "ok"
    assert health_checks.firestore_health_check()["status"] == "ok"
    assert firestore.count == 1


def test_the_probe_reads_again_after_two_minutes(firestore) -> None:
    health_checks.firestore_health_check()
    firestore.clock["now"] += 121
    health_checks.firestore_health_check()
    assert firestore.count == 2


def test_a_deep_check_always_reads(firestore) -> None:
    health_checks.firestore_health_check()
    health_checks.firestore_health_check(fresh=True)
    assert firestore.count == 2


def test_an_error_is_never_reused_and_clears_a_stale_ok(firestore) -> None:
    health_checks.firestore_health_check()
    firestore.fail = True
    assert health_checks.firestore_health_check(fresh=True)["status"] == "error"
    assert health_checks.firestore_health_check()["status"] == "error"
    firestore.fail = False
    assert health_checks.firestore_health_check()["status"] == "ok"
    assert firestore.count == 4
