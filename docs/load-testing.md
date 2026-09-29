# Load Testing

HireLog's search endpoint is the capacity-defining path (hybrid semantic +
lexical retrieval, ML reranking). We size and regression-check it with
[k6](https://k6.io).

## Running

```bash
# 1. Get a Firebase ID token (search requires auth), or enable E2E_AUTH_BYPASS
#    on a throwaway backend and use the bypass token.
# 2. Point at the target host and run:
BASE_URL=https://<host> TOKEN=<firebase_id_token> k6 run loadtest/search_load.js
```

The scenario ramps 1 → 50 → 100 virtual users over ~2.5 minutes with realistic
think time, drawing from a fixed query pool.

## Service-Level Objectives (enforced as k6 thresholds)

| Metric | Threshold | Rationale |
|---|---|---|
| `http_req_failed` | < 1% | Errors excluding expected 429 rate-limit responses |
| `search_latency_ms` p95 | < 1500 ms | Interactive search feels responsive |
| `search_latency_ms` p99 | < 3000 ms | Tail stays bounded under the 100-VU spike |

k6 exits non-zero if any threshold is breached, so this can gate a release.

## Results

Record each run so regressions are visible over time. Replace the placeholder
row after the first run against a representative environment.

| Date | Host / tier | Peak VUs | p50 | p95 | p99 | Error % | Notes |
|---|---|---|---|---|---|---|---|
| _TBD_ | _e.g. HF Spaces CPU_ | 100 | _–_ | _–_ | _–_ | _–_ | Baseline |

### Interpreting the tail

Under the 100-VU spike, semantic search is bounded by `SEARCH_SEMANTIC_MAX_CONCURRENCY`
(the `BoundedSemaphore`) and the shared lexical thread pool; requests beyond
those either queue briefly or fall back to keyword-only results, and sustained
excess is shed as `429`s by the search rate limiter. A rising p99 with flat
error rate usually means the semaphore/pool is the bottleneck — raise
`SEARCH_SEMANTIC_MAX_CONCURRENCY` / `SEARCH_LEXICAL_MAX_WORKERS` (CPU permitting)
before touching rate limits.

> Note: FAISS search currently serializes on a single index lock (see
> `docs/adr/0004-faiss-flatip-choice.md`). If p99 climbs under concurrency with
> CPU headroom to spare, that lock is the next thing to profile.
