"""Cohort rehearsal against the LOCAL emulator-backed stack (see start-stack.ps1).

Drives a whole cohort through the real HTTP API at the same time - roster import, an exam, a
released session, then N students who each admit, answer every question with several autosaves,
advance, submit and wait for their receipt - while a faculty client polls the live grid. It
reports per-endpoint latency, every error, and whether all attempts finalised.

Students sign in with unsigned, emulator-only tokens (the API accepts those only when
FIREBASE_AUTH_EMULATOR_HOST is set), so the OTP flow itself is not repeated 90 times here; the
browser journeys cover it for single users.

    python e2e-local/cohort_rehearsal.py [--students 90] [--questions 10] [--think 2.4]

What the numbers mean: they describe THIS machine and the Firestore EMULATOR. They say nothing
about real Firestore latency, quotas or Hugging Face hardware.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

API = "http://127.0.0.1:7862"
PROJECT = "demo-hirelog-exams"

_lock = threading.Lock()
_latencies: dict[str, list[float]] = defaultdict(list)
_errors: list[str] = []


def call(label: str, method: str, url: str, body=None, token=None, headers=None, record=True):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except OSError as exc:
        raw, status = str(exc).encode(), 0
    elapsed_ms = (time.perf_counter() - started) * 1000
    if record:
        with _lock:
            _latencies[label].append(elapsed_ms)
            if not 200 <= status < 300:
                _errors.append(f"{label} -> {status}: {raw[:160]!r}")
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, {}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def emulator_student_token(uid: str, institution_id: str, authz_version: int) -> str:
    """Unsigned token, honoured only by an API running with FIREBASE_AUTH_EMULATOR_HOST set."""
    now = int(time.time())
    payload = {
        "iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT, "auth_time": now, "iat": now,
        "exp": now + 3600, "user_id": uid, "sub": uid, "exam_institution_id": institution_id,
        "exam_authz_version": authz_version, "firebase": {"identities": {}, "sign_in_provider": "custom"},
    }
    return f"{b64(json.dumps({'alg': 'none', 'type': 'JWT'}).encode())}.{b64(json.dumps(payload).encode())}."


def operator_token() -> str:
    """The platform operator (a verified Google email on the API's allow-list) - unsigned, emulator-only."""
    now = int(time.time())
    payload = {
        "iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT, "auth_time": now, "iat": now,
        "exp": now + 3600, "user_id": "operator-rehearsal", "sub": "operator-rehearsal",
        "email": "operator@example.edu", "email_verified": True,
        "firebase": {"identities": {}, "sign_in_provider": "google.com"},
    }
    return f"{b64(json.dumps({'alg': 'none', 'type': 'JWT'}).encode())}.{b64(json.dumps(payload).encode())}."


def run_student(uid: str, run_id: str, revision_id: str, institution_id: str, questions: int, think: float) -> str:
    token = emulator_student_token(uid, institution_id, 1)
    base = f"{API}/api/exams/v1/student/runs/{run_id}"
    time.sleep(random.uniform(0, 5))  # students do not all click at the same instant
    status, attempt = call("admit", "POST", f"{base}/admit", token=token)
    if status != 200:
        return "admit failed"
    epoch = attempt.get("epoch", 0)
    versions: dict[str, int] = {}
    for slot in range(questions):
        question_id = f"{revision_id}:slot:{slot}"
        version = 0
        for edit in range(3):  # three autosaves per question, as a student editing an answer would produce
            time.sleep(random.uniform(think * 0.8, think * 1.2))
            status, saved = call(
                "save draft", "PATCH", f"{base}/drafts/{question_id}",
                {"expected_version": version, "answer": {"choice": "ABCD"[(slot + edit) % 4]}}, token=token,
            )
            if status == 200:
                version = saved["version"]
        versions[question_id] = version
        if slot < questions - 1:
            call("next question", "POST", f"{base}/next-question", token=token)
    status, _ = call(
        "submit", "POST", f"{base}/submit",
        {"idempotency_key": f"submit-{run_id}-{uid}", "client_epoch": epoch, "expected_draft_versions": versions},
        token=token,
    )
    if status != 200:
        return "submit failed"
    deadline = time.time() + 180
    while time.time() < deadline:
        status, _ = call("receipt", "GET", f"{base}/receipt", token=token, record=False)
        if status == 200:
            return "finalized"
        time.sleep(2)
    return "no receipt"


def monitor(run_id: str, token: str, stop: threading.Event, interval: float) -> None:
    """What a faculty member's control screen does: poll the run, the grid and the appeals."""
    while not stop.is_set():
        call("faculty: run", "GET", f"{API}/api/exams/v1/faculty/runs/{run_id}", token=token)
        call("faculty: grid", "GET", f"{API}/api/exams/v1/faculty/runs/{run_id}/attempts?limit=100", token=token)
        stop.wait(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--students", type=int, default=90)
    parser.add_argument("--questions", type=int, default=10)
    parser.add_argument("--think", type=float, default=2.4, help="seconds between autosaves (the UI debounces at 3s)")
    args = parser.parse_args()

    token = operator_token()
    stamp = int(time.time())
    _, institution = call("setup", "POST", f"{API}/api/exams/v1/faculty/institutions", {"name": f"Cohort {stamp}"}, token, record=False)
    institution_id = institution["id"]
    rows = [{"identity_key": f"R{i:03d}", "contact_email": f"r{i:03d}@cohort.example.edu", "role_scope": "student"}
            for i in range(1, args.students + 1)]
    roster = f"{API}/api/exams/v1/faculty/institutions/{institution_id}/roster"
    _, dry = call("setup", "POST", f"{roster}/dry-run", {"rows": rows}, token, record=False)
    _, applied = call("setup", "POST", f"{roster}/{dry['id']}/apply", {"rows": rows}, token, record=False)
    assert applied["status"] == "completed", applied
    _, definition = call("setup", "POST", f"{API}/api/exams/v1/faculty/definitions",
                         {"title": f"Cohort exam {stamp}", "institution_id": institution_id}, token, record=False)
    _, revision = call("setup", "POST", f"{API}/api/exams/v1/faculty/definitions/{definition['id']}/publish",
                       {"slot_count": args.questions}, token, record=False)
    _, run = call("setup", "POST", f"{API}/api/exams/v1/faculty/runs", {
        "definition_id": definition["id"], "revision_id": revision["id"],
        "policy": {"duration_minutes": 120, "warning_threshold": 3}, "enroll_all_institution_students": True}, token, record=False)
    for command, version in (("publish", run["version"]), ("release", run["version"] + 1)):
        status, _ = call("setup", "POST", f"{API}/api/exams/v1/faculty/runs/{run['id']}/commands/{command}",
                         {"expected_version": version, "idempotency_key": f"{run['id']}-{command}"}, token, record=False)
        assert status == 200, command
    uids = run["enrolled_uids"]
    print(f"cohort of {len(uids)} released; {args.questions} questions, ~{args.think}s between autosaves")

    stop = threading.Event()
    watcher = threading.Thread(target=monitor, args=(run["id"], token, stop, 5.0), daemon=True)
    watcher.start()
    started = time.time()
    with ThreadPoolExecutor(max_workers=len(uids)) as pool:
        outcomes = list(pool.map(
            lambda uid: run_student(uid, run["id"], revision["id"], institution_id, args.questions, args.think), uids))
    stop.set()
    watcher.join(timeout=10)
    elapsed = time.time() - started

    print(f"\nfinished in {elapsed:.0f}s")
    summary = defaultdict(int)
    for outcome in outcomes:
        summary[outcome] += 1
    print("outcomes:", dict(summary))
    print(f"{'endpoint':<16}{'calls':>7}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}")
    for label, values in sorted(_latencies.items()):
        values.sort()
        p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
        print(f"{label:<16}{len(values):>7}{statistics.median(values):>9.0f}{p95:>9.0f}{values[-1]:>9.0f}")
    print(f"errors: {len(_errors)}")
    for line in _errors[:15]:
        print("  ", line)
    _, grid = call("setup", "GET", f"{API}/api/exams/v1/faculty/runs/{run['id']}/attempts?limit=200&fresh=true", token=token, record=False)
    states = defaultdict(int)
    for attempt in grid.get("results", []):
        states[attempt["status"]] += 1
    print("final grid:", dict(states))
    ok = summary.get("finalized", 0) == len(uids) and not _errors
    print("RESULT:", "all students finalised with no errors" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
