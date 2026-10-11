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
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
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


class ExamSubmitBurst:
    """Synchronized end-of-exam burst: every student admits, saves several drafts, then ALL submit at the same
    instant (barrier), then each retries the same submit as if the first response was lost and the receipt must be
    identical. Records per-step latency percentiles, status codes and receipt/idempotency mismatches. Like the admission
    test it describes only the machine and backend it ran against."""

    def __init__(self, *, base_url: str, run_id: str, question_id: str, uid_tokens: dict[str, str], workers: int,
                 drafts_per_student: int, timeout_seconds: float, submit_retries: int = 0) -> None:
        self.submit_retries = submit_retries
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.question_id = question_id
        self.uid_tokens = uid_tokens
        self.workers = max(1, min(workers, len(uid_tokens)))
        self.drafts = max(1, drafts_per_student)
        self.timeout = timeout_seconds
        self._lock = threading.Lock()
        self.lat: dict[str, list[float]] = {}
        self.status: dict[str, Counter] = {}
        self.errors: Counter[str] = Counter()
        self.receipts: dict[str, str] = {}
        self.mismatches: list[str] = []

    def _call(self, step: str, method: str, path: str, token: str, body: dict | None = None) -> tuple[str, dict | None]:
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, method=method, headers=headers, data=data)
        start = time.perf_counter()
        label, parsed = "ERR", None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                label = str(response.status)
                raw = response.read()
                parsed = json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            label = str(exc.code)
            try:
                parsed = json.loads(exc.read())
            except ValueError:
                parsed = None
        except Exception as exc:
            with self._lock:
                self.errors[exc.__class__.__name__] += 1
        finally:
            ms = (time.perf_counter() - start) * 1000.0
            with self._lock:
                self.lat.setdefault(step, []).append(ms)
                self.status.setdefault(step, Counter())[label] += 1
        return label, parsed

    def _student(self, uid: str, barrier: threading.Barrier, phase_gate: threading.Barrier) -> None:
        token = self.uid_tokens[uid]
        base = f"/api/exams/v1/student/runs/{self.run_id}"
        label, admitted = self._call("admit", "POST", f"{base}/admit", token)
        epoch = (admitted or {}).get("epoch", 1)
        phase_gate.wait()  # everyone has been admitted before anyone starts answering
        version = 0
        for n in range(self.drafts):
            label, saved = self._call(
                "draft_save", "PATCH", f"{base}/drafts/{self.question_id}", token,
                {"expected_version": version, "answer": {"selected": "A", "n": n}, "client_save_id": f"{uid}-{n}", "client_epoch": epoch},
            )
            tries = 0
            while (label == "ERR" or label.startswith("5")) and tries < self.submit_retries:
                tries += 1  # the same save id, so a save that did commit is acknowledged again rather than duplicated
                time.sleep(1.0)
                label, saved = self._call(
                    "draft_save_retry", "PATCH", f"{base}/drafts/{self.question_id}", token,
                    {"expected_version": version, "answer": {"selected": "A", "n": n}, "client_save_id": f"{uid}-{n}", "client_epoch": epoch},
                )
            if label == "200" and saved:
                version = saved.get("version", version + 1)
        key = f"burst-{uid}"
        payload = {"idempotency_key": key, "client_epoch": epoch, "expected_draft_versions": {self.question_id: version}}
        barrier.wait()  # the synchronized submit burst
        label, first = self._call("submit", "POST", f"{base}/submit", token, payload)
        retries = 0
        while (label == "ERR" or label.startswith("5")) and retries < self.submit_retries:
            retries += 1  # the client's own retry of an unanswered / failed submit, with the SAME idempotency key
            time.sleep(1.0)
            label, first = self._call("submit_client_retry", "POST", f"{base}/submit", token, payload)
        receipt = ((first or {}).get("receipt") or {}).get("receipt_id")
        # The same request again, as if the first response had been lost.
        label2, again = self._call("submit_retry", "POST", f"{base}/submit", token, payload)
        receipt2 = ((again or {}).get("receipt") or {}).get("receipt_id")
        _, via_get = self._call("receipt_get", "GET", f"{base}/receipt", token)
        receipt3 = ((via_get or {}).get("receipt") or via_get or {}).get("receipt_id") if isinstance(via_get, dict) else None
        with self._lock:
            if receipt:
                self.receipts[uid] = receipt
            if not receipt or receipt2 != receipt or (receipt3 is not None and receipt3 != receipt):
                self.mismatches.append(f"{uid}: first={receipt} retry={receipt2} get={receipt3}")

    def run(self) -> dict:
        uids = list(self.uid_tokens)
        gate = threading.Barrier(len(uids))
        burst = threading.Barrier(len(uids))
        started = time.perf_counter()
        # One thread per student so the barriers really release everyone together.
        threads = [threading.Thread(target=self._student, args=(u, burst, gate), daemon=True) for u in uids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started

        def pct(step: str) -> dict:
            values = sorted(self.lat.get(step, []))
            if not values:
                return {}
            return {"n": len(values), "p50": round(_percentile(values, 50), 1), "p95": round(_percentile(values, 95), 1),
                    "p99": round(_percentile(values, 99), 1), "max": round(values[-1], 1)}

        return {
            "config": {"base_url": self.base_url, "run_id": self.run_id, "students": len(uids), "threads": len(uids),
                       "drafts_per_student": self.drafts, "timeout_seconds": self.timeout},
            "elapsed_seconds": round(elapsed, 2),
            "latency_ms": {step: pct(step) for step in self.lat},
            "status_counts": {step: dict(c) for step, c in self.status.items()},
            "transport_errors": dict(self.errors),
            "students_with_receipt": len(self.receipts),
            "distinct_receipt_ids": len(set(self.receipts.values())),
            "receipt_mismatches": self.mismatches[:20],
            "receipt_mismatch_count": len(self.mismatches),
        }


def verify_firestore(run_id: str) -> dict:
    """Reads the run's attempts and outbox straight from the Firestore the target used (emulator or staging): states, one
    outbox entry per finalized attempt, and how long each entry waited (created -> processed)."""
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from app.core.firebase import exam_db as db  # noqa: PLC0415

    attempts = [d.to_dict() for d in db.collection("exam_runs").document(run_id).collection("attempts").stream()]
    outbox = [d.to_dict() for d in db.collection("exam_outbox").where("run_id", "==", run_id).stream()]
    final_entries = [o for o in outbox if o.get("kind") == "attempt_finalized"]

    def parse(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value

    ages = sorted(
        (parse(o["processed_at"]) - parse(o["created_at"])).total_seconds() for o in final_entries if o.get("processed_at")
    )
    return {
        "attempt_status_counts": dict(Counter(a.get("status") for a in attempts)),
        "attempts": len(attempts),
        "finalize_outbox_entries": len(final_entries),
        "finalize_entries_per_uid_max": max(Counter(o["payload"]["uid"] for o in final_entries).values(), default=0),
        "outbox_processed": sum(1 for o in final_entries if o.get("processed")),
        "outbox_unprocessed": sum(1 for o in final_entries if not o.get("processed")),
        "outbox_attempts_max": max((o.get("attempts", 0) for o in final_entries), default=0),
        "queue_age_seconds": {
            "n": len(ages), "p50": round(_percentile(ages, 50), 2) if ages else None,
            "p95": round(_percentile(ages, 95), 2) if ages else None, "max": round(ages[-1], 2) if ages else None,
        },
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
    parser.add_argument("--scenario", choices=["admit", "burst"], default="admit",
                        help="admit: repeated admission; burst: admit, drafts, synchronized submit, lost-response retry")
    parser.add_argument("--drafts", type=int, default=3, help="burst: draft saves per student before submitting")
    parser.add_argument("--submit-retries", type=int, default=0,
                        help="burst: retries (same idempotency key, 1 s apart) after a transport error or 5xx on submit")
    parser.add_argument("--question-id", default=None, help="burst: question id (default: <revision_id>:slot:0 from the students file)")
    parser.add_argument("--verify-firestore", action="store_true",
                        help="burst: afterwards read attempts and outbox from the same Firestore the target used")
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

    if args.scenario == "burst":
        question_id = args.question_id or f"{students_payload['revision_id']}:slot:0"
        report = ExamSubmitBurst(
            base_url=args.base_url, run_id=args.run_id, question_id=question_id, uid_tokens=uid_tokens,
            workers=args.workers, drafts_per_student=args.drafts, timeout_seconds=max(args.timeout_seconds, 0.5),
            submit_retries=args.submit_retries,
        ).run()
        if args.verify_firestore:
            report["firestore"] = verify_firestore(args.run_id)
    else:
        report = ExamLoadTester(
            base_url=args.base_url,
            run_id=args.run_id,
            uid_tokens=uid_tokens,
            workers=args.workers,
            duration_seconds=max(args.duration_seconds, 1),
            timeout_seconds=max(args.timeout_seconds, 0.5),
        ).run()
    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
