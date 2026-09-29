# ADR 0006 — Search-quality evaluation as a CI gate

**Status:** Accepted

## Context

Search ranking is easy to regress silently — a tokenizer tweak, a fusion-weight
change, or a field-weighting edit can quietly degrade relevance with all unit
tests still green. "It returns results" is not "it returns the right results."

## Decision

A small, deterministic evaluation harness (`backend/eval/`) measures ranking
quality and gates CI:

- A fixed labelled **corpus** (`corpus.json`) and **golden query set**
  (`golden_queries.json`) with relevance judgments.
- The harness ranks with the **real** BM25 scorer from `search_core` (the same
  lexical branch used in production) and computes **recall@5, MRR, nDCG@5**
  (`metrics.py`).
- `run_eval.py` fails (non-zero exit) if any metric drops below its gate. Gates
  are set below the measured baseline with headroom; tighten as ranking improves.

Metric functions are pure and unit-tested (`tests/test_eval_metrics.py`).

### Scope and extension

The CI gate evaluates the **lexical** branch only — it needs no Firestore, FAISS,
or embedding model, so it is fast and deterministic. Semantic/hybrid evaluation
(embedding recall against a live index) is the natural extension: run the same
golden set through the full pipeline in a scheduled job and track the same
metrics over time.

## Consequences

- **Positive**: relevance regressions fail the build; ranking changes come with
  a number, not a vibe; the methodology is documented and extensible.
- **Negative**: the golden set is small and hand-labelled — it catches gross
  regressions, not subtle ranking shifts; it must grow with real query patterns
  to stay representative.
