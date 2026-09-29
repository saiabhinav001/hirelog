# ADR 0005 — Distributed sliding-window rate limiting

**Status:** Accepted

## Context

Expensive endpoints (search, mutations, dashboards) need rate limiting. A purely
in-memory limiter resets on every restart/deploy and isn't shared across
instances, so under rolling deploys or autoscaling the limit is effectively
per-process and easily bypassed.

## Decision

`SlidingWindowLimiter` supports a Redis backend (sorted-set sliding window: prune
old entries, add current, count, expire — in one pipeline) when
`SEARCH_REDIS_URL` is configured, keyed per **namespace** (`mutation`,
`dashboard`, `admin_dashboard`, `search`) so limiters don't share counters.

Without Redis, or on any Redis error, it **degrades to a thread-safe in-process
window** — availability is preferred over strict global enforcement (fail-open).

## Consequences

- **Positive**: limits survive restarts and are shared across instances when
  Redis is present; namespaced keys prevent cross-limiter interference; single
  code path for both backends.
- **Negative / known limits**: fail-open means a Redis outage silently weakens
  enforcement to per-process; there is **no circuit breaker**, so during an
  outage each check still attempts Redis before falling back. Both are acceptable
  at current scale and are the next hardening steps if Redis becomes load-bearing.
