"""Search-quality evaluation harness.

Ranks a labelled golden query set against a fixed corpus using the *real*
lexical scorer from ``app.services.search_core`` (the same BM25 used in
production's hybrid retrieval), then reports recall@k / MRR / nDCG@k. It is
deterministic and dependency-light (no Firestore, FAISS, or embedding model),
so it runs in CI and fails the build if ranking quality regresses below the
configured thresholds.

Usage:
    python -m eval.run_eval            # human report, non-zero exit if below gates
    python -m eval.run_eval --json     # machine-readable summary

This evaluates the lexical branch only — semantic recall needs the embedding
model and a live index, which are out of scope for a CI gate. See
docs/adr/0006-search-evaluation.md for the methodology and how to extend it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.services.search_core import bm25_score_documents, build_document_terms, normalize_text, tokenize_terms

from eval.metrics import mean, ndcg_at_k, recall_at_k, reciprocal_rank

_EVAL_DIR = Path(__file__).resolve().parent
_K = 5

# CI quality gates. Set from an initial measured baseline with headroom; tighten
# as ranking improves. A regression below these fails the build.
_THRESHOLDS = {
    "recall@5": 0.85,
    "mrr": 0.80,
    "ndcg@5": 0.80,
}


def _load(name: str):
    with (_EVAL_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _rank(query: str, document_terms: dict[str, list[str]]) -> list[str]:
    query_terms = tokenize_terms(normalize_text(query), max_terms=12)
    scores = bm25_score_documents(query_terms, document_terms)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def evaluate() -> dict:
    corpus = _load("corpus.json")
    golden = _load("golden_queries.json")

    document_terms = {doc["id"]: build_document_terms(doc) for doc in corpus}

    recalls, rrs, ndcgs = [], [], []
    per_query = []
    for case in golden:
        ranked = _rank(case["query"], document_terms)
        relevant = case["relevant"]
        r = recall_at_k(ranked, relevant, _K)
        rr = reciprocal_rank(ranked, relevant)
        nd = ndcg_at_k(ranked, relevant, _K)
        recalls.append(r)
        rrs.append(rr)
        ndcgs.append(nd)
        per_query.append(
            {"query": case["query"], "recall@5": r, "rr": rr, "ndcg@5": nd, "top": ranked[:_K]}
        )

    return {
        "num_queries": len(golden),
        "corpus_size": len(corpus),
        "metrics": {
            "recall@5": mean(recalls),
            "mrr": mean(rrs),
            "ndcg@5": mean(ndcgs),
        },
        "thresholds": _THRESHOLDS,
        "per_query": per_query,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Search-quality evaluation")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    report = evaluate()
    metrics = report["metrics"]
    failures = [name for name, floor in _THRESHOLDS.items() if metrics[name] < floor]

    if args.json:
        report["passed"] = not failures
        print(json.dumps(report, indent=2))
    else:
        print(f"Search eval — {report['num_queries']} queries over {report['corpus_size']} docs\n")
        for name in ("recall@5", "mrr", "ndcg@5"):
            floor = _THRESHOLDS[name]
            mark = "PASS" if metrics[name] >= floor else "FAIL"
            print(f"  {name:10s} {metrics[name]:.3f}   (gate >= {floor:.2f})  [{mark}]")
        if failures:
            print("\nWeakest queries:")
            for row in sorted(report["per_query"], key=lambda r: r["ndcg@5"])[:3]:
                print(f"  ndcg={row['ndcg@5']:.2f}  {row['query']!r} -> {row['top']}")

    if failures:
        print(f"\nFAILED quality gates: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nAll quality gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
