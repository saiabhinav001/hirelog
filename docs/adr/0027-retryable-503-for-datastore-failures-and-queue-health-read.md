# ADR 0027 — Datastore failures are a retryable 503, and operators get a queue-health read

**Status:** Accepted

## Context

Prompt 11 asks for failure injection (restarts, database outage), measured contention and finalization queue age, and alerts. Running a synchronized submit burst against a local Firestore emulator and killing the API during it found:

1. When Firestore transactions kept aborting ("Transaction lock timeout", then the client library's `ValueError: Failed to commit transaction in 5 attempts`), or the datastore was unavailable, the API answered a **bare 500**: a stack trace per request in the log, no `Retry-After`, and nothing to tell a client or a monitor that a retry of the same request is safe. In the first restart run that was 60 responses (drafts and submits); the clients that retried recovered, the ones that did not were left with stale draft versions.
2. There was no way for an operator to see the outbox backlog, dead entries, judge retries or attempts waiting to be sealed — the alert thresholds in the earlier report were "proposals" with nothing to read them from.
3. The test suite read `EXAMS_ENABLED` from the developer's `.env` (11 failures when it was `true`).

## Decisions

### 1. One mapping for "try again"
`Aborted`, `ServiceUnavailable`, `DeadlineExceeded`, `ResourceExhausted`, `RetryError` and the exhausted-transaction `ValueError` return `503 {"detail": {"code": "temporarily_unavailable", …}}` with `Retry-After: 2` and `no-store`. Everything else stays a 500 with a generic body; details go only to the server log.
This is safe because every write that matters already carries an idempotency key, a save id or an expected version (Prompts 3 and 6): a failed call committed nothing, and an identical retry returns the same receipt. Alternatives rejected: retrying inside the request (longer queues under the very load that caused the abort); mapping to 429 (it is not the client's rate).

### 2. A read-only queue-health endpoint
`GET /api/exams/v1/ops/queue-health`, super-admin only, counts and ages only. It reuses the repositories the workers use (outbox, judge submissions, runs / attempts) and adds no state. It is the source for the alert table in the release pack. It does not detect an outbox entry that was *delivered but skipped* (a finalized answer whose question has no variant assignment is logged and dropped; the gradebook shows it as pending) — recorded as a finding.

### 3. Tests do not depend on the environment
`conftest.py` pins `EXAMS_ENABLED=false`.

## Consequences

- Clients can distinguish "retry the same request" (503) from a defect (500); monitors can alert on the 503 rate.
- The student client should treat 503 like a transport error; no server change is needed for that.
- Rollback: revert; nothing is stored.

## Not done

A real outage rehearsal on Firestore; an alerting platform; counting skipped-grading entries; the backend framework security upgrade (a FastAPI 0.143 / Starlette 1.7 trial failed only the 14 route-introspection tripwire tests and needs its own change).
