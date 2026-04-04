from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.parse
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


class LoadTester:
    def __init__(
        self,
        *,
        base_url: str,
        workers: int,
        duration_seconds: int,
        target_rps: float,
        search_ratio: float,
        search_mode: str,
        search_limit: int,
        timeout_seconds: float,
        auth_token: str | None,
        queries: list[str],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workers = workers
        self.duration_seconds = duration_seconds
        self.target_rps = target_rps
        self.search_ratio = max(0.0, min(search_ratio, 1.0))
        self.search_mode = search_mode
        self.search_limit = search_limit
        self.timeout_seconds = timeout_seconds
        self.auth_token = auth_token
        self.queries = [q.strip() for q in queries if q.strip()]

        if not self.queries:
            self.queries = [
                "coding round arrays",
                "system design interview",
                "sql joins",
                "operating system scheduling",
                "data structures and algorithms",
            ]

        self._lock = threading.Lock()
        self.latencies_ms: list[float] = []
        self.status_counts: Counter[str] = Counter()
        self.endpoint_counts: Counter[str] = Counter()
        self.error_counts: Counter[str] = Counter()

    def _build_request(self) -> tuple[str, str]:
        if random.random() <= self.search_ratio:
            query = random.choice(self.queries)
            params = urllib.parse.urlencode(
                {
                    "q": query,
                    "mode": self.search_mode,
                    "limit": self.search_limit,
                }
            )
            return f"{self.base_url}/api/search?{params}", "search"
        return f"{self.base_url}/health", "health"

    def _single_request(self) -> None:
        url, endpoint = self._build_request()
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        req = urllib.request.Request(url=url, method="GET", headers=headers)

        start = time.perf_counter()
        status_label = "ERR"
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                _ = response.read()
                status_label = str(response.status)
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
                self.endpoint_counts[endpoint] += 1

    def _worker(self, end_time: float, interval_seconds: float) -> None:
        next_fire = time.perf_counter()
        while True:
            now = time.perf_counter()
            if now >= end_time:
                return

            if interval_seconds > 0 and now < next_fire:
                time.sleep(min(0.01, next_fire - now))
                continue

            self._single_request()
            if interval_seconds > 0:
                next_fire += interval_seconds

    def run(self) -> dict:
        start_time = time.perf_counter()
        end_time = start_time + self.duration_seconds

        interval_seconds = 0.0
        if self.target_rps > 0:
            per_worker_rps = max(self.target_rps / self.workers, 0.0001)
            interval_seconds = 1.0 / per_worker_rps

        threads: list[threading.Thread] = []
        for idx in range(self.workers):
            thread = threading.Thread(
                target=self._worker,
                args=(end_time, interval_seconds),
                name=f"load-worker-{idx}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

        elapsed = max(time.perf_counter() - start_time, 0.001)
        latencies_sorted = sorted(self.latencies_ms)
        total = len(latencies_sorted)
        success = sum(
            count for code, count in self.status_counts.items() if code.isdigit() and code.startswith("2")
        )

        summary = {
            "config": {
                "base_url": self.base_url,
                "workers": self.workers,
                "duration_seconds": self.duration_seconds,
                "target_rps": self.target_rps,
                "search_ratio": self.search_ratio,
                "search_mode": self.search_mode,
                "search_limit": self.search_limit,
                "timeout_seconds": self.timeout_seconds,
            },
            "totals": {
                "requests": total,
                "success_2xx": success,
                "non_2xx_or_errors": total - success,
                "success_rate_percent": round((success / total) * 100.0, 2) if total else 0.0,
                "achieved_rps": round(total / elapsed, 2),
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
            "endpoint_mix": dict(self.endpoint_counts),
            "error_types": dict(self.error_counts),
        }
        return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backend load test for search and health endpoints.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--workers", type=int, default=20, help="Number of concurrent workers")
    parser.add_argument("--duration-seconds", type=int, default=30, help="Test duration")
    parser.add_argument(
        "--target-rps",
        type=float,
        default=120.0,
        help="Approximate total request rate. Set 0 for unthrottled.",
    )
    parser.add_argument(
        "--search-ratio",
        type=float,
        default=0.9,
        help="Ratio of requests routed to /api/search. Remaining traffic hits /health.",
    )
    parser.add_argument(
        "--search-mode",
        default="auto",
        choices=["auto", "semantic", "keyword"],
        help="Search mode query parameter used for /api/search requests",
    )
    parser.add_argument("--search-limit", type=int, default=20, help="Search endpoint result limit")
    parser.add_argument("--timeout-seconds", type=float, default=8.0, help="Per-request timeout")
    parser.add_argument("--auth-token", default=None, help="Optional bearer token")
    parser.add_argument(
        "--queries",
        default="coding round arrays,system design interview,sql joins,operating system scheduling,data structures and algorithms",
        help="Comma-separated search queries used by workers.",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="Optional path to write the JSON report artifact",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tester = LoadTester(
        base_url=args.base_url,
        workers=max(args.workers, 1),
        duration_seconds=max(args.duration_seconds, 1),
        target_rps=max(args.target_rps, 0.0),
        search_ratio=args.search_ratio,
        search_mode=args.search_mode,
        search_limit=max(args.search_limit, 1),
        timeout_seconds=max(args.timeout_seconds, 0.5),
        auth_token=args.auth_token,
        queries=[q.strip() for q in args.queries.split(",")],
    )
    report = tester.run()
    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
