# ADR 0004 — FAISS `IndexFlatIP` (exact) over an ANN index

**Status:** Accepted

## Context

Semantic search needs a vector index. Options range from exact brute-force
(`IndexFlatIP`) to approximate nearest-neighbour indexes (IVF, HNSW) that trade
recall for sub-linear query time.

## Decision

Use `IndexFlatIP` (exact inner-product search) for now.

At the current corpus size (thousands to low tens of thousands of vectors, 384-d),
a flat exact scan is **sub-millisecond to low-millisecond per query** and returns
**100% recall** with zero tuning. An ANN index would add index-build complexity,
recall/latency tuning, and a correctness surface (silent recall loss) for no
practical latency win at this scale — i.e. premature optimization.

### When to revisit

Switch to **IVF** or **HNSW** when any holds:

- Vector count exceeds ~1M (flat scan RAM ≈ `N × 384 × 4` bytes; ~1.5 GB at 1M),
  or
- Measured p99 search latency approaches the SLO in
  [../load-testing.md](../load-testing.md) with CPU headroom to spare.

Two known scaling limits are documented rather than fixed now:

1. **Search serializes on a single index lock** (`FaissStore.search` holds
   `self._lock`). Fine until concurrent semantic search throughput matters; the
   fix is a read/write lock or snapshot-swap-on-write.
2. **Single unsharded index** in one process. Sharding/replication is an ANN-era
   concern.

## Consequences

- **Positive**: exact results, no tuning, trivial to reason about; the index is
  rebuilt from Firestore on boot (`faiss_reconcile`) if the local file is lost.
- **Negative**: linear query cost and a global search lock cap throughput at
  scale — accepted deliberately, with the revisit triggers above.
