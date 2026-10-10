# Exams — staging qualification and release pack (Prompt 11)

Date: 2026-10-11. Branch `exams/phase-11-production-qualification`, HEAD `19d5d3c` plus the uncommitted Prompt 11 work described here.
This document replaces the 2026-10-02 qualification report (it described a 464-test suite and an admission-only load test; the history is kept in §3.4).

## 0. Release recommendation

**Not ready for production or for a real cohort.** Nothing found in this pass is a known correctness defect left open in the exam flow, and the local evidence is good
(1529 backend tests, synchronized 300-student submit burst with zero errors and exactly-once receipts, restart-during-burst recovery, rollback lever rehearsed).
But the prompt's central gates — a staging environment, the institution's concurrency target, real Firestore / IAM / bucket / sandbox behaviour, a measured restore, real browsers and devices — **could not be executed**: this workspace has no staging project,
no credentials, no container runtime and no load generator beyond one Windows laptop. Every number below comes from a local Firestore emulator (or in-memory fakes) and says nothing about production capacity.

Recommended next step: a supervised **staging qualification** against a real Firebase project once the institution fixes the concurrency target (D-14) and the provider decisions in §13. The remaining gates are listed in §14.

## 1. What was run, where, and how honest the numbers are

| Item | Value |
|---|---|
| Environment | One Windows 11 Home laptop, Intel i5-1235U (10 cores / 12 threads), 7.7 GB RAM; client, API and emulator all on it (loopback) |
| Backend runtime | Python 3.12 locally (**production image and CI use 3.11** — see §14), `uvicorn` 0.27.1, **one worker**, FastAPI 0.115.8 / Starlette 0.45.3 |
| Datastore | Local Firestore emulator (`gcloud emulators firestore start`, Java 21) — real transactions and lock timeouts, no network, no quota, no multi-region |
| Identity | Unsigned emulator-only tokens (`FIREBASE_AUTH_EMULATOR_HOST`); real token verification, revocation and expiry were **not** exercised |
| Credentials | A throwaway key in the temp folder (never in the repository, deleted); no real project was contacted |
| Data | Synthetic students (`q100-…`, `q300-…`, `qr150-…`, `rb-…`); one slot, no generated question content, no code / SQL / short-answer items |

Reproduce: start the emulator, set `FIRESTORE_EMULATOR_HOST`, `FIREBASE_AUTH_EMULATOR_HOST`, a project id and a service-account path, run `scripts/exam_staging_seed.py --student-count N --output-file seed.json`, start `uvicorn app.exam_main:app`,
then `scripts/exam_load_test.py --scenario burst --run-id … --students-file seed.json --emulator-insecure-tokens --verify-firestore`. The same scripts take real tokens (`--tokens-file`) against a real staging deployment.

## 2. Test evidence

| Suite | Command | Result |
|---|---|---|
| Backend lint / compile | `ruff check .`, `python -m compileall app tests eval scripts` | Clean |
| Backend tests | `pytest` (run with `EXAMS_ENABLED` unset and `.env` saying `true`) | **1529 passed** (1423 at Prompt 9, 1516 at Prompt 10, +13 new) |
| Backend, exam API's own requirements | Fresh virtualenv from `requirements-exams.txt` (+ numpy) | 1529 passed — the exam image's dependency set is sufficient |
| Legacy search-quality gate | `python -m eval.run_eval --json` | Passed (all quality gates) |
| Frontend | `tsc --noEmit`, `eslint src`, `vitest run --maxWorkers=2 --testTimeout=20000`, `npm run build` | Clean (one pre-existing unused-directive warning), **237 passed in 35 files**, build succeeds on Next 16.4.0 |
| Real-browser e2e | Playwright (Chromium): `exams-pwa-cache`, `exams-pwa`, `exams-accessibility` | 6 passed (stable across repeated parallel runs after the wait fix, §4 P11-4) |
| Legacy end-to-end smoke | `e2e/smoke.spec.ts` | Not re-run this phase (needs the legacy backend) |

Pre-existing CI facts (OPS-01): the workflow runs on pull requests and pushes to `main` only, and this branch's history is **unrelated to `origin/main`** (no merge base; 27 commits ahead, 38 behind). The CI workflow has therefore never run on this work.
Decision D-10 (rebase or add a branch trigger) is still open. Tests now pin `EXAMS_ENABLED=false` in `conftest.py`: without it, 11 smoke tests failed on any machine whose `.env` enabled the module.

## 3. Load and burst report (local, emulator-backed)

### 3.1 Synchronized end-of-exam burst — `exam_load_test.py --scenario burst`

Every student (one thread each, 300 threads) admits, saves 3 versioned drafts, waits at a barrier, then **all submit at the same instant**; each then re-sends the identical submit (a "lost response") and reads the receipt.

| Config | Value |
|---|---|
| Students / threads | 300 / 300 (one client process, urllib), client timeout 60 s |
| Server | 1 uvicorn worker, default threadpool, `EXAMS_ENABLED=true`, all background loops running |
| Run | Fresh run, 1 slot, 120-minute exam, no question content |

| Step | n | p50 | p95 | p99 | max (ms) |
|---|---|---|---|---|---|
| admit | 300 | 5933 | 6078 | 6095 | 6098 |
| draft save | 900 | 3886 | 5527 | 5624 | 5675 |
| submit (the burst) | 300 | 5731 | 6326 | 6359 | 6480 |
| submit retried with the same key | 300 | 3641 | 4539 | 4588 | 4806 |
| receipt read | 300 | 2798 | 4217 | 4232 | 4236 |

Results: **every request 200** (0 non-2xx, 0 transport errors, 0 draft-save errors); 300 distinct receipts, 0 mismatches between first submit / retry / receipt read; all 300 attempts `submitted`;
exactly **one** finalization outbox entry per student (300), all processed with **0 retries** (`attempts` max 0). Finalization queue age (outbox entry created → processed): **p50 19.4 s, p95 31.3 s, max 34.0 s**; the backlog drained about 16 s after the burst ended.
A 100-student shakedown (p95 3.9 s for admit, 3.5 s for submit) and a 150-student run gave the same correctness results.

**Reading these numbers.** Multi-second latencies are queueing on a single sync worker with 40 request threads, a Java emulator and 300 client threads sharing 12 logical cores — they are saturation of this laptop, not a property of the design, and **must not be quoted as capacity**.
What they do show: no lost or duplicated submits, no double grading trigger and no stuck queue at an offered load of 300 simultaneous students on one worker. What they cannot show: p95/p99 on real Firestore, the effect of several instances, or the institution's real target (never supplied — D-14).
Design consequence to verify in staging: the API does several Firestore round trips per request, so a real deployment needs more than one worker/instance, and the finalization drain (one outbox loop per instance) is the likely first bottleneck for large cohorts.

### 3.2 Transaction contention
Server-side contention exhaustion (`Transaction lock timeout`, Firestore "Failed to commit transaction in 5 attempts") is the one failure the burst provoked, and only while 150 clients retried at once right after an API restart: 15 draft saves in that run. See P11-1: before this phase they were bare **500**s; now they are retryable **503**s and every one succeeded on retry.
The 300-student burst without a restart produced none.

### 3.3 Not measured (no way to do it here)
p95/p99 on real Firestore; realtime (WebSocket) lag; parser, generation and judge throughput with real providers; storage and egress; cost; multi-instance behaviour; sustained duration (runs were 20–90 s). These are gates (§14).

### 3.4 Earlier admission-only run (2026-10-02, kept for history)
60 synthetic students, 20 workers, 20 s: 1216 requests, 100% success, p95 486 ms, every student admitted exactly once. Same caveats.

### 3.5 Legacy search load
Not re-run (single-source rate limit makes it uninformative, as before).

## 4. Failure injection

| Scenario | How it was exercised | Evidence | Status |
|---|---|---|---|
| **API process killed and restarted during the burst** | Live: API killed 18 s into a 150-student burst, restarted 8 s later, clients retried (drafts and submits, same keys) | 150/150 submitted exactly once, 150 distinct receipts, 0 mismatches, 150 outbox entries (1 per student) all processed, 0 server tracebacks. First attempt, before P11-1: 6 students left `in_progress` with stale draft versions and 60 bare 500s (6 draft saves, 27 submits, 27 submit retries); that client did not retry draft saves; no data was lost or duplicated | **Measured (local)** |
| Lost response (submit) | Live: the identical submit re-sent after every submit | Same receipt id for all 300; HTTP 200 both times | **Measured (local)** |
| Database outage | Automated: Firestore switched off under the real student routes | `test_a_database_outage_is_a_retryable_503_*`: state / admit / draft / submit all `503` + `Retry-After`, no internals in the body, nothing committed, the same requests succeed afterwards with one receipt; deadline keeps running through an outage | Automated; **real outage not rehearsed** |
| Transaction contention | Live (above) + automated | 5 retryable error classes → 503; other failures stay a generic 500 that leaks nothing | Measured + automated |
| Rollback: new admissions paused | Live: API restarted with `EXAM_NEW_ADMISSIONS_PAUSED=true` | Already-admitted student: admit 200, state 200, draft 200, **submit 200 with receipt**; a new student: `403 new_admissions_paused`; flag cleared → admitted; the receipt survives | **Measured (local)** |
| Expired / revoked identity, stale session | Authorization tests (Prompts 1, 2, 6) | `authorization_version` checks, stale-epoch refusal | Automated; **not with a real identity provider** |
| Queue outage / worker restart | Outbox lease + retry tests; the live restart above | Entries delivered after restart, once | Measured + automated |
| Generation outage | Prompt 5 tests (timeouts, malformed output, shortfall) | Fake / mock transport only | Automated; **no real provider** |
| Judge outage | Prompt 8 tests (`INFRA_ERROR`, exhausted, stall → operator, regrade lineage) | Pending, never zero; fake provider only | Automated; **no real judge** |
| Storage failure | Prompt 9 tests (submit succeeds without evidence; refusal in production without a bucket) | Fake storage only | Automated; **no real bucket** |
| Deadline enforcement, immutable receipts, idempotency, no duplicate warnings, no hidden-content leakage, eventual grading | Prompts 3, 6, 7, 9 suites; the burst's per-student receipt checks | See those ADRs | Automated + measured |
| Sandbox egress / resource limits | — | Needs a real judge (D-5 unapproved) | **Not tested** |
| Storage / IAM boundaries | — | Needs a real bucket and service account | **Not tested** |
| Redis outage | Code reading (`SlidingWindowLimiter` falls back to per-process) | — | Not exercised |

### Defects found and fixed in this phase

| # | Severity | Defect | Fix |
|---|---|---|---|
| P11-1 | High | When transactions kept aborting (contention) or the datastore was unavailable, the API answered a bare **500** — indistinguishable from a bug, no `Retry-After`, a logged stack trace per request | One handler maps retryable datastore errors (`Aborted`, `ServiceUnavailable`, `DeadlineExceeded`, `ResourceExhausted`, `RetryError`) and the exhausted-transaction `ValueError` to `503 temporarily_unavailable` + `Retry-After: 2` + `no-store`; every other failure stays a generic 500 with no detail. 13 tests (fail on the old code) |
| P11-2 | Medium | The suite depended on a developer's `.env`: with `EXAMS_ENABLED=true` there, 11 tests failed | `conftest.py` pins `EXAMS_ENABLED=false` |
| P11-3 | **Release-blocking** | `next` 16.2.10 carried **2 critical** advisories (unauthenticated RCE on Windows-hosted servers; RCE in the image optimizer) | `next` 16.4.0 (non-major); `websocket-driver`, `nanoid`, `source-map-js` updated in the lockfile. Verified: tsc, eslint, 237 tests, production build, 6 e2e. `eslint-config-next` was deliberately **not** bumped (16.4.0 adds 35 new lint errors) |
| P11-4 | Low | The offline-shell e2e failed when run in parallel (it did not wait for the worker to be active) | Waits for `serviceWorker.ready` and the precache |
| P11-5 | Medium | No operator-visible queue figures existed (OPS-05) | `GET /api/exams/v1/ops/queue-health` (super-admin, counts only) — §10 |

## 5. Security findings

**Dependencies (this phase, re-run):**

| Scope | Before | After | Remaining |
|---|---|---|---|
| Frontend `npm audit --omit=dev` | 11 (2 critical, 7 high, 2 moderate) | **6 (0 critical, 4 high, 2 moderate)** | `firebase` → `@firebase/firestore`, `@firebase/firestore-compat`, `@grpc/grpc-js` (high; the only offered fix is a major **downgrade** to firebase 9.14 — needs an upstream release or an accepted-risk decision); `postcss` (moderate, pinned by an override) and one more moderate |
| Backend `pip-audit -r requirements-exams.txt` | 16 findings in 2 packages | unchanged | `starlette` 0.45.3 (needs ≥ 1.3.1 for all advisories) and `python-dotenv` 1.0.1 (≥ 1.2.2) |

Trial of the backend upgrade (scratch virtualenv, not applied): FastAPI 0.143.0 + Starlette 1.7.0 + python-dotenv 1.2.4 passed **1515 of 1529** tests; the 14 failures are all route-guard **introspection** tests that read FastAPI's dependency structure
(`test_exam_authz_route_guards.py` and the per-phase "bound to scope" tripwires) — no behavioural test failed. The upgrade is feasible but is its own change (rework the tripwires, re-run everything) and is **not** done here.

**Code and configuration:**
- No new secret, address or credential was added; the emulator credential was a throwaway outside the repository.
- No hard-coded secrets (grep of `backend/app` and `frontend/src`).
- Firestore rules: the default-deny catch-all covers every exam collection including `gradebook_release_rows`; **not verified with the rules emulator** (needs `firebase-tools`, not installed — gate G8).
- Student routes showed **no rate-limit refusals** at 300 simultaneous students from one IP (good for campus NAT; also means no per-student abuse limiter on draft saves — draft size is capped, `EXAM_DRAFT_MAX_BYTES`).
- **Finding (not fixed):** if a finalized attempt's question has no variant assignment, the outbox handler logs a warning, skips grading and marks the entry *delivered* — no retry, no alert. The gradebook shows that answer as *pending* (never zero) and a release needs the P10 acknowledgement, so no student is silently graded wrong,
  but an operator is not told. Low likelihood (assignments are made before the attempt) — recommended: count such skips in the queue-health read. Seen only because the synthetic seed has no assigned variants.
- **Correction to the earlier adversarial review** ([adversarial-review-report.md](adversarial-review-report.md) §12 said "PWA updates don't interrupt exams — verified passing"): Prompt 10 found that claim false (the layout's service-worker hook released updates mid-attempt) and fixed it. Treat that review's other "verified passing" rows as
  *needing re-verification under the HTTP-level, concurrent standard* used since Prompt 6 (which has overturned several earlier "verified" rows). A fresh adversarial pass after staging is a gate (G15).
- The 2026-10-02 report's sandbox, SSRF, malware-scan and IAM items remain **untested against real infrastructure**.

## 6. Accessibility results

Automated and keyboard evidence from Prompt 10 stands (axe in jsdom on the exam, results, blocked and gradebook pages; keyboard-only multiple select to submit; named controls; polite live warnings; real-Chromium login-page axe + keyboard; theme contrast tokens).
**Not done:** real screen readers (NVDA / JAWS / VoiceOver / TalkBack), 200–400% zoom and reflow, axe colour-contrast computation, real devices, Safe Exam Browser. See [accessibility-and-lockdown-checklist.md](accessibility-and-lockdown-checklist.md). Gate G11.

## 7. Browser / runtime support matrix

| Item | Tested | Not tested |
|---|---|---|
| Browser | Chromium (Playwright bundled) on Windows 11 | Firefox, Safari / iOS, Chrome on Android, Edge, older versions, Safari service-worker limits |
| Frontend runtime | Node 22.19, Next 16.4.0 (production build + dev server) | Node LTS matrix, the real hosting platform |
| Backend runtime | Python 3.12 (local), uvicorn 0.27.1 | **Python 3.11.9 (`runtime.txt`, `Dockerfile.exams`, CI) — the actual production version**; container build (no container runtime here) |
| Datastore | Firestore emulator | Real Firestore (regional latency, quotas, index builds) |
| Lockdown | — | Managed client / SEB (PWA alone is not lockdown) |

## 8. Cost measurements
**None.** Cost needs real billing data (Firestore reads / writes, storage and egress, generation / judge / scanner providers). Gate G13.

## 9. Migration dry-runs
No data migration is required by Prompts 10–11 (additive fields, one new private collection, no backfill). The only index need is a single-field query on `gradebook_export_jobs.expires_at` (automatic). No migration was run against real data. `exam_staging_seed.py --dry-run`
prints the seed plan without writing. A staging rehearsal of the first release (writes one document per enrolled student) is part of G1.

## 10. Operations: configuration, alerts, runbooks

### 10.1 Secrets and configuration checklist
Set through the host's secret manager, never in the repository (names only; see `backend/.env.example`):

| Must be set in staging / production | Purpose |
|---|---|
| `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_JSON` (or `_PATH`) | The project and its least-privilege service account |
| `EXAMS_ENABLED` (default off), `ENV=production` | Module switch; production guards (evidence bucket, OTP provider) |
| `EXAM_SUPER_ADMIN_EMAILS`, `EXAM_FACULTY_EMAILS`, `EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL=false` | Staff allowlists (verified e-mail only) |
| `EXAM_OTP_HMAC_SECRET`, `EXAM_OTP_DELIVERY_PROVIDER` | **Required for more than one instance** (the per-process fallback secret breaks OTP across restarts / instances) |
| `ALLOWED_ORIGINS`, `NEXT_PUBLIC_EXAMS_API_BASE_URL` | CORS and the frontend's pinned API origin |
| `EXAM_EVIDENCE_STORAGE_BUCKET` | Without it evidence is refused in production and exports sit in process memory (single instance only) |
| `EXAM_JUDGE_PROVIDER_BASE_URL` / `_API_KEY`, `EXAM_JUDGE_VALIDATED_LANGUAGES` | Only after D-5 |
| `EXAM_GENERATION_PROVIDER_*`, `EXAM_REVIEW_PROVIDER_*`, `EXAM_FEEDBACK_PROVIDER_*`, `EXAM_MALWARE_SCANNER_ENDPOINT` | Only after D-13 / D-4 |
| Tuning to set explicitly | `EXAM_OUTBOX_LEASE_SECONDS` (60), `EXAM_OUTBOX_MAX_ATTEMPTS` (5), `EXAM_FINALIZATION_SWEEP_*`, `EXAM_EXPORT_RETENTION_HOURS` (24), `EXAM_EVIDENCE_RETENTION_DAYS` (90), `EXAM_DRAFT_MAX_BYTES` |
| Rollback levers | `EXAM_NEW_ADMISSIONS_PAUSED`, per-run pause, `EXAMS_ENABLED` — [rollback-runbook.md](rollback-runbook.md) |

### 10.2 Alerts (proposals — **no alerting is wired**; gate G12)
Source: `GET /api/exams/v1/ops/queue-health` (super-admin, polled by the monitor) and `/health/ready`.

| Signal (field) | Warn | Page | First action |
|---|---|---|---|
| Outbox backlog age (`outbox.oldest_age_seconds`) | > 60 s | > 300 s | Check worker loops are running; instance count; Firestore latency |
| Dead outbox entries (`outbox.dead`) | ≥ 1 | ≥ 1 for 10 min | Read `last_error`, fix the cause, requeue (entries at the attempt limit are not retried) |
| Outbox entries with errors (`outbox.with_errors`) | > 0 for 5 min | — | Same |
| Judge waiting age (`judge.oldest_waiting_age_seconds`) | > `EXAM_JUDGE_STALL_SECONDS` | 2× that | Judge provider health; `judge.infra_error_retrying` |
| Judge exhausted (`judge.infra_exhausted_needs_operator`) | ≥ 1 | ≥ 1 for 15 min | Requeue after the provider recovers (the question stays *pending*, never zero) |
| Finalization lag (`finalization.overdue_unsealed`, `max_overdue_seconds`) | > 0 for 60 s | `max_overdue_seconds` > 120 | Sweeper running? `EXAM_FINALIZATION_SWEEP_ENABLED`; instance health |
| API 503 `temporarily_unavailable` rate | > 1% of requests / 1 min | > 5% | Firestore status; contention (reduce load, add instances) |
| `/health/ready` not `ready` | 1 failure | 3 consecutive | Firestore reachability |
| Admission pause left on | `EXAM_NEW_ADMISSIONS_PAUSED=true` for > 1 h | — | Confirm it is intentional |

### 10.3 Runbooks
| Situation | Where |
|---|---|
| Pause a run / pause admissions / disable the module (never deletes data) | [rollback-runbook.md](rollback-runbook.md) |
| Deploy, smoke test, troubleshooting | [deployment-guide.md](deployment-guide.md) |
| Backup, restore, exams collections and evidence | [../runbooks/backup-dr.md](../runbooks/backup-dr.md) |
| Institution readiness | [institution-onboarding-checklist.md](institution-onboarding-checklist.md) |
| **Datastore outage or contention (503 `temporarily_unavailable`)** | Students' clients retry the same request (same idempotency key / save id): nothing was committed by a failed call, deadlines keep running, receipts stay valid. Check `/health/ready` and the Firestore console; if it is load, pause new admissions rather than disabling the module |
| **Dead outbox entries / judge exhausted / finalization lag** | The alert table above; all three are visible in the queue-health read and none loses an answer (drafts are sealed at finalization; ungraded answers show as *pending* in the gradebook) |

## 11. Rollback plan
1. Narrowest: pause the affected run (teacher command, audited) — students keep their drafts, the deadline keeps running.
2. Global: `EXAM_NEW_ADMISSIONS_PAUSED=true` — **rehearsed locally** (§4): in-progress students continue and submit; new students get `403 new_admissions_paused`.
3. Broadest: `EXAMS_ENABLED=false` — locks everyone out; use only when the engine itself is unsafe.
4. Code rollback is safe for data: Prompt 10–11 fields are additive and old code ignores them (releases would be served live again). **Never delete exam data to roll back.**
Not rehearsed: the same levers on a real multi-instance deployment (config propagation delay, instances restarting at different times).

## 12. Backup and restore (RPO / RTO)
**Not measured.** The procedure exists ([backup-dr.md](../runbooks/backup-dr.md)); the emulator holds state in memory and offers no export, and no real project exists here, so no restore was performed and no RPO / RTO figure exists. Gate G7.

## 13. Institution decisions still open
D-1 super-admin list; D-4 malware scanner, private bucket and upload retention; D-5 judge architecture, language set and SQL runner; D-6 SSO or verified-e-mail OTP and its provider; D-7 proctoring profile and retention; **D-8** partial credit, negative marking and official count definitions (current rules: [scoring-and-gradebook-semantics.md](scoring-and-gradebook-semantics.md));
D-9 short-link policy; **D-10 CI history (rebase or branch trigger)**; D-11 legacy placement-cell promotion; D-12 per-student pause semantics; D-13 generation / review provider.
**New: D-14 — the institution-confirmed concurrency target** (students per run, simultaneous submit window, runs in parallel). Without it no capacity claim can be made; this pass used 100 / 150 / 300 as arbitrary shake-down sizes.

## 14. Incomplete gates (every one blocks a production recommendation)

| Gate | Needed | Blocked by |
|---|---|---|
| G1 | Real-staging load at the D-14 target: p95/p99, draft errors, queue age, contention, multi-instance | Staging project, D-14, load generator |
| G2 | Realtime (WebSocket) lag under load | Staging |
| G3 | Parser, generation and judge throughput with real providers | D-4, D-5, D-13, providers |
| G4 | Sandbox egress, metadata-endpoint and resource-limit evidence | D-5, a real judge |
| G5 | Real bucket / IAM boundaries (evidence, exports, uploads, scanner) | D-4, a project |
| G6 | Real outage rehearsal: Firestore, queue, Redis, expired / revoked identity with a real IdP | Staging, D-6 |
| G7 | Backup restore in isolation with measured RPO / RTO | Staging, a backup |
| G8 | Firestore rules emulator suite (AUTH-08) | `firebase-tools` |
| G9 | CI actually running this branch, on Python 3.11 (production version) and the container build | D-10, a runner, a container runtime |
| G10 | Backend security upgrade (Starlette ≥ 1.3.1, python-dotenv ≥ 1.2.2, tripwire rework); accepted-risk or upstream fix for the firebase / grpc-js highs; `pip-audit` and `npm audit` in CI | A separate change |
| G11 | Real browsers / devices / screen readers / zoom / contrast / managed client | People and devices |
| G12 | Alerting wired to queue-health and `/health/ready`; on-call | Monitoring platform |
| G13 | Cost measurements | Real billing data |
| G14 | Real identity-provider and OTP delivery provider | D-6 |
| G15 | Fresh final adversarial review at the HTTP / concurrent standard (the 2026-10 review had at least one wrong "verified" row) | After G1–G9 |
| G16 | Surface skipped-grading outbox entries (§5 finding) | Small code change |

**Recommendation:** do not release. Proceed to a supervised staging qualification (G1–G9, G12, G14) with the institution's D-14 figure; re-assess after G15.
