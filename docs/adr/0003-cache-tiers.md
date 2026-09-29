# ADR 0003 — Multi-tier caching for dashboard & search

**Status:** Accepted

## Context

The dashboard aggregates across the experiences collection; recomputing per
request is expensive. Search results for popular queries repeat. Both need to be
fast and cheap, across multiple stateless API instances.

## Decision

**Dashboard stats** use three tiers: in-process memory (hot path, guarded by a
single lock so a payload and its timestamp update atomically) → a Firestore
`metadata` document (survives restarts, shared across instances) → full
recompute. The expensive main + admin aggregation is **pre-computed off the
request path** by a background refresher that is **coalesced**: concurrent
write-triggered refreshes collapse into one in-flight run plus at most one
trailing run, so bursts of writes never stampede Firestore with parallel scans.
The headline experience count uses a Firestore `count()` aggregation (exact),
not the sampled scan (which caps at `DASHBOARD_SAMPLE_LIMIT`).

**Search results** use a two-tier cache (`search_cache.py`): Redis when
`SEARCH_REDIS_URL` is set (shared across instances), else per-process memory.
Cache invalidation uses `SCAN` (non-blocking), never `KEYS`.

## Consequences

- **Positive**: sub-100 ms cached dashboard loads; search cache shared across
  instances; no request-path scans; write bursts don't multiply scan cost.
- **Negative**: several cache layers to keep coherent; a short window of
  staleness after a write until the coalesced refresh lands (acceptable for
  analytics). Redis is optional — absence degrades to per-process caches, not
  failure.
