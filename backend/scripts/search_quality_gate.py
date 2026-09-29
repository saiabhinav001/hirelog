from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _dcg(grades: list[float], k: int) -> float:
    total = 0.0
    for idx, grade in enumerate(grades[:k], start=1):
        total += (2**grade - 1.0) / math.log2(idx + 1)
    return total


def _ndcg(predicted_ids: list[str], relevance_by_id: dict[str, float], k: int) -> float:
    predicted_grades = [float(relevance_by_id.get(doc_id, 0.0)) for doc_id in predicted_ids[:k]]
    ideal_grades = sorted((float(v) for v in relevance_by_id.values()), reverse=True)

    actual = _dcg(predicted_grades, k)
    ideal = _dcg(ideal_grades, k)
    if ideal <= 0:
        return 0.0
    return actual / ideal


def _build_relevance_map(entry: dict) -> dict[str, float]:
    explicit = entry.get("relevance")
    if isinstance(explicit, dict):
        result: dict[str, float] = {}
        for key, value in explicit.items():
            try:
                result[str(key)] = float(value)
            except Exception:
                continue
        return result

    # Fallback: treat relevant_ids list as descending graded relevance.
    relevant_ids = [str(item) for item in (entry.get("relevant_ids") or []) if str(item).strip()]
    score = float(len(relevant_ids))
    graded: dict[str, float] = {}
    for doc_id in relevant_ids:
        graded[doc_id] = max(score, 1.0)
        score -= 1.0
    return graded


def _run_search(base_url: str, query: str, filters: dict, k: int, mode: str, timeout_seconds: float) -> list[str]:
    params = {"q": query, "mode": mode, "limit": str(k)}
    for field in ("company", "role", "year", "topic", "difficulty"):
        value = filters.get(field)
        if value is None:
            continue
        params[field] = str(value)

    encoded = urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}/api/search?{encoded}"

    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=max(timeout_seconds, 1.0)) as response:
        payload = json.loads(response.read().decode("utf-8"))

    results = payload.get("results") or []
    return [str(item.get("id")) for item in results if isinstance(item, dict) and item.get("id")]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate NDCG quality gate for /api/search")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Backend base URL")
    parser.add_argument(
        "--benchmark-file",
        default="data/search_benchmark.json",
        help="JSON benchmark file with query and relevance labels",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "semantic", "keyword"],
        help="Search mode used while evaluating benchmark queries",
    )
    parser.add_argument("--k", type=int, default=10, help="NDCG@k cutoff")
    parser.add_argument("--min-ndcg", type=float, default=0.82, help="Minimum mean NDCG threshold")
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP timeout per query")
    parser.add_argument(
        "--report-file",
        default=None,
        help="Optional path to write the JSON report artifact",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    benchmark_path = Path(args.benchmark_file)
    if not benchmark_path.exists():
        print(
            json.dumps(
                {
                    "status": "error",
                    "detail": f"Benchmark file not found: {benchmark_path}",
                },
                indent=2,
            )
        )
        sys.exit(2)

    with benchmark_path.open("r", encoding="utf-8") as file:
        benchmark_entries = json.load(file)

    if not isinstance(benchmark_entries, list) or not benchmark_entries:
        print(json.dumps({"status": "error", "detail": "Benchmark file must be a non-empty JSON array."}, indent=2))
        sys.exit(2)

    rows = []
    ndcg_values: list[float] = []

    for entry in benchmark_entries:
        if not isinstance(entry, dict):
            continue
        query = str(entry.get("query") or "").strip()
        if not query:
            continue

        relevance_by_id = _build_relevance_map(entry)
        if not relevance_by_id:
            continue

        filters = entry.get("filters") if isinstance(entry.get("filters"), dict) else {}
        predicted_ids = _run_search(
            args.base_url,
            query,
            filters,
            args.k,
            args.mode,
            max(args.timeout_seconds, 1.0),
        )
        score = _ndcg(predicted_ids, relevance_by_id, args.k)
        ndcg_values.append(score)
        rows.append(
            {
                "query": query,
                "ndcg": round(score, 4),
                "returned": len(predicted_ids),
            }
        )

    if not ndcg_values:
        print(json.dumps({"status": "error", "detail": "No valid benchmark rows found."}, indent=2))
        sys.exit(2)

    mean_ndcg = sum(ndcg_values) / len(ndcg_values)
    passed = mean_ndcg >= args.min_ndcg

    report = {
        "status": "pass" if passed else "fail",
        "benchmark_count": len(ndcg_values),
        "k": args.k,
        "min_ndcg": args.min_ndcg,
        "mean_ndcg": round(mean_ndcg, 4),
        "rows": rows,
    }

    if args.report_file:
        report_path = Path(args.report_file)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
