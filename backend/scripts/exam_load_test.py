"""Exam-API load test — Prompt 11 (production qualification).

Mirrors `load_test.py`'s architecture (thread-pool workers, latency
percentiles, JSON report) but targets the exam admission path instead of
legacy search: each simulated student repeatedly calls
`POST /runs/{run_id}/admit` for the test duration — the first call performs
the real NOT_STARTED -> ADMITTED -> IN_PROGRESS transition, every call after
that exercises the idempotent already-admitted return path under concurrency
(the realistic "waiting room refresh" pattern).

Auth: the exam API requires a real Firebase ID token for every request
(contract: server-assigned identity, never a test bypass baked into
production code). Two ways to supply one:

  * --tokens-file PATH   JSON object {uid: id_token}, for real tokens
    obtained out-of-band (real sign-in against a real or Auth-emulator
    project) — use this against any real staging deployment.
  * --emulator-insecure-tokens   Mints unsigned, emulator-only JWTs locally
    for each uid. These are ONLY ever accepted by a server process that has
    FIREBASE_AUTH_EMULATOR_HOST set (firebase_admin then skips signature
    verification entirely) — they are rejected by every real Firebase
    project. Never use this flag against anything but a local emulator.

State hardware/configuration caveat (per the module's own release-honesty
contract): results from this tool describe ONLY the machine and Firestore
backend (real project vs. local emulator) it was run against. They are not a
substitute for a real staging-environment load test and must never be quoted
as production capacity evidence without re-running against real
infrastructure.
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_unsigned_emulator_token(uid: str, project_id: str) -> str:
    """Emulator-only unsigned JWT — see module docstring. NEVER valid
    against a real Firebase project (no real Google key ever signs this)."""
    header = {"alg": "none", "type": "JWT"}
    now = int(time.time())
    payload = {
        "iss": f"https://securetoken.google.com/{project_id}",
        "aud": project_id,
        "auth_time": now,
        "user_id": uid,
        "sub": uid,
        "iat": now,
        "exp": now + 3600,
        "email": f"{uid}@example.edu",
        "email_verified": True,
        "firebase": {"identities": {}, "sign_in_provider": "custom"},
    }
    segments = [_b64url(json.dumps(header).encode()), _b64url(json.dumps(payload).encode())]
    return ".".join(segments) + "."


class ExamLoadTester:
    def __init__(
        self,
        *,
        base_url: str,
        run_id: str,
        uid_tokens: dict[str, str],
        workers: int,
        duration_seconds: int,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.uid_tokens = uid_tokens
        self.workers = max(workers, 1)
        self.duration_seconds = duration_seconds
        self.timeout_seconds = timeout_seconds

        self._lock = threading.Lock()
        self.latencies_ms: list[float] = []
        self.status_counts: Counter[str] = Counter()
        self.error_counts: Counter[str] = Counter()
        self.distinct_admitted_uids: set[str] = set()

    def _admit_once(self, uid: str, token: str) -> None:
        url = f"{self.base_url}/api/exams/v1/student/runs/{self.run_id}/admit"
        req = urllib.request.Request(
            url=url, method="POST", headers={"Authorization": f"Bearer {token}"}, data=b""
        )
        start = time.perf_counter()
        status_label = "ERR"
        body: dict | None = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
                status_label = str(response.status)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
        except urllib.error.HTTPError as exc:
            _ = exc.read()
            status_label = str(exc.code)
        except Exception as exc:
            status_label = "ERR"
            with self._lock:
                self.error_counts[exc.__class__.__name__] += 1
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            with self._lock:
                self.latencies_ms.append(latency_ms)
                self.status_counts[status_label] += 1
                if status_label == "200" and body and body.get("uid") == uid:
                    self.distinct_admitted_uids.add(uid)

    def _worker(self, uids: list[str], end_time: float) -> None:
        if not uids:
            return
        i = 0
        while time.perf_counter() < end_time:
            uid = uids[i % len(uids)]
            self._admit_once(uid, self.uid_tokens[uid])
            i += 1

    def run(self) -> dict:
        uids = list(self.uid_tokens.keys())
        if not uids:
            raise ValueError("no uid/token pairs supplied")

        # Partition uids across workers so every worker repeatedly drives its
        # OWN slice (first call = real admission, rest = idempotent replay —
        # the realistic "waiting room refresh" pattern), never two workers
        # racing the exact same uid against each other.
        buckets: list[list[str]] = [[] for _ in range(self.workers)]
        for idx, uid in enumerate(uids):
            buckets[idx % self.workers].append(uid)

        start_time = time.perf_counter()
        end_time = start_time + self.duration_seconds
        threads = [
            threading.Thread(target=self._worker, args=(bucket, end_time), name=f"exam-load-{i}", daemon=True)
            for i, bucket in enumerate(buckets)
            if bucket
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        elapsed = max(time.perf_counter() - start_time, 0.001)
        latencies_sorted = sorted(self.latencies_ms)
        total = len(latencies_sorted)
        success = sum(
            count for code, count in self.status_counts.items() if code.isdigit() and code.startswith("2")
        )

        return {
            "config": {
                "base_url": self.base_url,
                "run_id": self.run_id,
                "distinct_students": len(uids),
                "workers": self.workers,
                "duration_seconds": self.duration_seconds,
                "timeout_seconds": self.timeout_seconds,
            },
            "totals": {
                "requests": total,
                "success_2xx": success,
                "non_2xx_or_errors": total - success,
                "success_rate_percent": round((success / total) * 100.0, 2) if total else 0.0,
                "achieved_rps": round(total / elapsed, 2),
                "distinct_students_admitted": len(self.distinct_admitted_uids),
            },
            "latency_ms": {
                "avg": round((sum(latencies_sorted) / total), 2) if total else 0.0,
                "min": round(latencies_sorted[0], 2) if total else 0.0,
                "p50": round(_percentile(latencies_sorted, 50), 2) if total else 0.0,
                "p95": round(_percentile(latencies_sorted, 95), 2) if total else 0.0,
                "p99": round(_percentile(latencies_sorted, 99), 2) if total else 0.0,
                "max": round(latencies_sorted[-1], 2) if total else 0.0,
            },
            "status_counts": dict(self.status_counts),
            "error_types": dict(self.error_counts),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exam API load test (admission path).")
    parser.add_argument("--base-url", default="http://localhost:8020", help="Exam API base URL")
    parser.add_argument("--run-id", required=True, help="RELEASED exam run id (see exam_staging_seed.py)")
    parser.add_argument(
        "--students-file",
        required=True,
        help="JSON file from exam_staging_seed.py (reads its 'student_uids' list)",
    )
    parser.add_argument(
        "--tokens-file", default=None, help="JSON object {uid: id_token} of real, out-of-band-obtained tokens"
    )
    parser.add_argument(
        "--emulator-insecure-tokens",
        action="store_true",
        help="Mint unsigned emulator-only tokens locally. ONLY works against a server with "
        "FIREBASE_AUTH_EMULATOR_HOST set — rejected by every real Firebase project.",
    )
    parser.add_argument("--project-id", default="demo-test", help="Project id used when minting emulator tokens")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--output-file", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.tokens_file and not args.emulator_insecure_tokens:
        raise SystemExit("one of --tokens-file or --emulator-insecure-tokens is required")

    students_payload = json.loads(Path(args.students_file).read_text(encoding="utf-8"))
    uids = students_payload["student_uids"]

    if args.emulator_insecure_tokens:
        uid_tokens = {uid: make_unsigned_emulator_token(uid, args.project_id) for uid in uids}
    else:
        real_tokens = json.loads(Path(args.tokens_file).read_text(encoding="utf-8"))
        uid_tokens = {uid: real_tokens[uid] for uid in uids if uid in real_tokens}

    tester = ExamLoadTester(
        base_url=args.base_url,
        run_id=args.run_id,
        uid_tokens=uid_tokens,
        workers=args.workers,
        duration_seconds=max(args.duration_seconds, 1),
        timeout_seconds=max(args.timeout_seconds, 0.5),
    )
    report = tester.run()
    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
