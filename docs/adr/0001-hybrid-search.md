# ADR 0001 — Hybrid semantic + lexical search

**Status:** Accepted

## Context

Interview experiences are free text. Users search with both natural-language
intent ("how to design a scalable feed") and exact keywords ("Dijkstra",
company names, acronyms like "ACID"). Pure keyword search misses paraphrase and
intent; pure vector search misses rare exact tokens and can drift on short
queries.

## Decision

Run two retrieval branches and fuse them:

- **Semantic**: MiniLM (`all-MiniLM-L6-v2`, 384-d) embeddings in FAISS, cosine
  via inner product on L2-normalized vectors.
- **Lexical**: BM25 over tokenized document fields (`search_core.py`).

Results are combined with Reciprocal Rank Fusion (weights configurable via
`SEARCH_RRF_*`), then optionally re-ranked by a cross-encoder
(`cross-encoder/ms-marco-MiniLM-L-6-v2`) for the top-K.

The semantic branch is bounded by a `BoundedSemaphore` (model concurrency) and a
circuit breaker: repeated semantic failures trip a cooldown during which search
degrades gracefully to lexical-only rather than erroring.

## Consequences

- **Positive**: robust across query styles; graceful degradation keeps search up
  when the model/index is unavailable; each branch is independently tunable.
- **Negative**: two code paths and a fusion step to maintain; more moving parts
  (semaphore, breaker, reranker) than a single-engine design.
- **Measured**: ranking quality is gated in CI — see
  [0006](0006-search-evaluation.md).
