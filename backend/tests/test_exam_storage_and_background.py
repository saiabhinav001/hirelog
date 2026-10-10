"""Exam storage boundary + background worker behaviour (free-tier safety)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.background import AdaptivePoller
from app.core.exam_storage import (
    EXAM_COLLECTIONS,
    PROTECTED_LEGACY_COLLECTIONS,
    CollectionBoundaryError,
    ExamStorageBoundary,
)
from tests.support.fake_firestore import FakeFirestore

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


# ── storage boundary ──────────────────────────────────────────────────────────

def test_boundary_allows_exam_collections_and_applies_prefix() -> None:
    client = FakeFirestore()
    boundary = ExamStorageBoundary(client, prefix="stg_")
    boundary.collection("exam_runs").document("r1").set({"ok": True})
    assert client.raw("stg_exam_runs/r1") == {"ok": True}
    assert client.raw("exam_runs/r1") is None


@pytest.mark.parametrize("name", sorted(PROTECTED_LEGACY_COLLECTIONS))
def test_boundary_blocks_every_legacy_collection(name: str) -> None:
    boundary = ExamStorageBoundary(FakeFirestore())
    with pytest.raises(CollectionBoundaryError):
        boundary.collection(name)


def test_boundary_blocks_listing_and_collection_group_apis() -> None:
    boundary = ExamStorageBoundary(FakeFirestore())
    for api in ("collection_group", "collections", "get_all", "document"):
        with pytest.raises(CollectionBoundaryError):
            getattr(boundary, api)


def test_boundary_does_not_break_introspection() -> None:
    boundary = ExamStorageBoundary(FakeFirestore())
    assert not hasattr(boundary, "__deepcopy__")


def test_no_legacy_collection_is_allow_listed() -> None:
    assert not (EXAM_COLLECTIONS & PROTECTED_LEGACY_COLLECTIONS)


def test_every_collection_the_exam_code_opens_is_allow_listed() -> None:
    """Static scan: a new repository that opens a new top-level collection must
    be added to the allow-list on purpose, or the deployed (bounded) process
    would fail at runtime."""
    constants = re.compile(r'^[A-Z_]*COLLECTION[A-Z_]*\s*=\s*"([a-z_]+)"', re.MULTILINE)
    literals = re.compile(r'\.collection\("([a-z_]+)"\)')
    files = [*APP_ROOT.joinpath("domain").rglob("*.py"), *APP_ROOT.joinpath("api").rglob("exams_*.py"),
             *APP_ROOT.joinpath("api", "routes").glob("exams_*.py"), APP_ROOT / "exam_main.py"]
    found: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        found |= set(constants.findall(text)) | set(literals.findall(text))
    subcollections = {"attempts", "drafts", "revisions", "extracted_versions"}
    assert found - subcollections - EXAM_COLLECTIONS == set()


def test_exam_code_never_references_the_legacy_users_helpers() -> None:
    """Identity comes from the verified token; the exam surface must not pull in
    the legacy cached user/role resolution."""
    for path in [APP_ROOT / "api" / "exams_dependencies.py", APP_ROOT / "api" / "exams_scope.py",
                 *APP_ROOT.joinpath("api", "routes").glob("exams_*.py")]:
        text = path.read_text(encoding="utf-8")
        assert "get_current_user_uncached" not in text, path.name
        assert "_get_or_create_user" not in text, path.name


def test_slim_exam_app_imports_without_the_legacy_search_stack() -> None:
    """Regression: `app.api.routes.__init__` used to import the numpy/FAISS
    search routes, so the exams-only image crashed on startup."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "for blocked in ('numpy', 'faiss', 'spacy', 'sentence_transformers', 'torch'):\n"
        "    sys.modules[blocked] = None\n"
        "import app.exam_main\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=APP_ROOT.parent, capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(APP_ROOT.parent)},
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip().endswith("ok")


# ── adaptive poller ───────────────────────────────────────────────────────────

def test_poller_backs_off_while_idle_up_to_the_cap() -> None:
    poller = AdaptivePoller("t", lambda: False, min_interval=2.0, max_interval=60.0, clock=lambda: 0.0)
    interval = 2.0
    seen = []
    for _ in range(8):
        interval = poller.run_once(interval)
        seen.append(interval)
    assert seen == [4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]


def test_poller_resets_to_fast_after_work() -> None:
    work = iter([False, False, True, False])
    poller = AdaptivePoller("t", lambda: next(work), min_interval=2.0, max_interval=60.0, clock=lambda: 0.0)
    interval = 2.0
    interval = poller.run_once(interval)
    interval = poller.run_once(interval)
    assert interval == 8.0
    assert poller.run_once(interval) == 2.0  # a tick that did work goes straight back to the fast cadence


def test_wake_keeps_the_fast_cadence_for_the_burst_window() -> None:
    now = [100.0]
    poller = AdaptivePoller("t", lambda: False, min_interval=2.0, max_interval=60.0, burst_seconds=30.0, clock=lambda: now[0])
    poller.wake()
    assert poller.next_interval(60.0, did_work=False) == 2.0
    now[0] += 31.0
    assert poller.next_interval(2.0, did_work=False) == 4.0


def test_a_failing_tick_never_kills_the_loop() -> None:
    def boom():
        raise RuntimeError("firestore unavailable")

    poller = AdaptivePoller("t", boom, min_interval=1.0, max_interval=8.0, clock=lambda: 0.0)
    assert poller.run_once(1.0) == 2.0
    assert poller.ticks == 1


def test_idle_polling_budget_is_a_small_fraction_of_the_spark_quota() -> None:
    """Worst-case idle reads/day for the four default loops (one read per empty
    query) must stay a few percent of the 50,000/day free quota — the old fixed
    2s/2s/15s loops cost ~92,000."""
    from app.core.config import Settings

    s = Settings()
    per_day = 86_400
    reads = (per_day / s.EXAM_OUTBOX_POLL_MAX_SECONDS + per_day / s.EXAM_JUDGE_SWEEP_MAX_SECONDS
             + per_day / s.EXAM_PROCTORING_SWEEP_MAX_SECONDS + per_day / 3600)
    assert reads < 0.05 * 50_000
