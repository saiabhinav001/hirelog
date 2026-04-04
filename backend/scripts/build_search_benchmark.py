from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_QUERIES = [
    "dsa interview questions",
    "sql joins round",
    "operating system scheduling",
    "hr fit interview",
    "system design backend",
    "computer networks tcp udp",
]


def _run_search(base_url: str, query: str, limit: int, mode: str, timeout_seconds: float) -> list[str]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "mode": mode,
            "limit": str(limit),
        }
    )
    url = f"{base_url.rstrip('/')}/api/search?{params}"
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=max(timeout_seconds, 1.0)) as response:
        payload = json.loads(response.read().decode("utf-8"))

    results = payload.get("results") or []
    return [str(item.get("id")) for item in results if isinstance(item, dict) and item.get("id")]


def _graded_relevance(ids: list[str], labels_per_query: int) -> dict[str, float]:
    selected = ids[:labels_per_query]
    if not selected:
        return {}

    score = float(len(selected))
    graded: dict[str, float] = {}
    for doc_id in selected:
        graded[doc_id] = max(score, 1.0)
        score -= 1.0
    return graded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a starter search benchmark file from current /api/search top results."
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="Backend base URL")
    parser.add_argument(
        "--output-file",
        default="data/search_benchmark.json",
        help="Path to write benchmark JSON",
    )
    parser.add_argument(
        "--queries",
        default=",".join(DEFAULT_QUERIES),
        help="Comma-separated benchmark queries",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "semantic", "keyword"],
        help="Search mode used while collecting labels",
    )
    parser.add_argument("--search-limit", type=int, default=20, help="Search result depth fetched per query")
    parser.add_argument("--labels-per-query", type=int, default=5, help="Number of graded labels stored per query")
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP timeout per query")
    parser.add_argument(
        "--min-results",
        type=int,
        default=3,
        help="Minimum result count required for query to be kept",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries = [item.strip() for item in args.queries.split(",") if item.strip()]

    benchmark_rows: list[dict] = []
    for query in queries:
        ids = _run_search(
            args.base_url,
            query,
            max(args.search_limit, 1),
            args.mode,
            max(args.timeout_seconds, 1.0),
        )
        if len(ids) < max(args.min_results, 1):
            continue

        relevance = _graded_relevance(ids, max(args.labels_per_query, 1))
        if not relevance:
            continue

        benchmark_rows.append(
            {
                "query": query,
                "filters": {},
                "relevance": relevance,
            }
        )

    if not benchmark_rows:
        raise SystemExit("No benchmark rows generated. Check backend URL/data and retry.")

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(benchmark_rows, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "ok",
                "queries_requested": len(queries),
                "rows_written": len(benchmark_rows),
                "output_file": str(output_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
