# Exams — Production Qualification Report (Prompt 11)

Scope: an evidence-based qualification pass across the whole Exams module
(Prompts 1-10) before any real institution is onboarded. Per the master
prompt's own release-honesty contract, this report states **exactly** what was
verified in this sandbox, how, and what still requires a real staging
environment this sandbox does not have. It does not claim local/emulator
results prove production capacity, and it does not claim unit tests prove real
infrastructure resilience.

Branch: `exams/phase-11-production-qualification`. Date: 2026-10-02.

## 1. Test suite baseline (full re-run, this phase)

| Suite | Command | Result |
|---|---|---|
| Backend lint | `ruff check .` | Clean, 0 violations |
| Backend compile | `python -m compileall app tests eval` | Clean |
| Backend unit tests | `pytest` | **464 passed**, 0 failed, 0 errors (460 pre-existing + 4 new, see §3) |
| Legacy search-quality CI gate | `python -m eval.run_eval --json` | **Passed** — recall@5=0.964 (≥0.85), MRR=1.0 (≥0.80), nDCG@5=0.964 (≥0.80), 14 queries / 12-doc fixed corpus |
| Frontend type-check | `npm run type-check` | Clean |
| Frontend lint | `npm run lint` | Clean |
| Frontend unit tests | `npx vitest run` | **49 passed**, 13 files |
| Frontend e2e (Playwright, real Chromium) | `npx playwright test` | **5 passed** (PWA offline/service-worker, accessibility axe scan, keyboard reachability, legacy smoke) |
| Frontend production build | `npm run build` | Succeeds, all 27 routes compile/prerender |

No regressions versus the Prompt 10 baseline (460/49/5). All four new backend
tests are the admissions-pause rollback lever added in this phase (§2).

## 2. Code change: new-admissions-pause rollback lever (contract #12)

**Gap found:** contract #12 requires rollback to "disable new admissions while
preserving safe completion," or to explicitly pause affected runs. Per-run
pausing already existed (`RunStatus.PAUSED` + faculty command, Prompt 3). The
"disable new admissions globally, without touching anyone already admitted"
half had no mechanism — the only global kill switch was `EXAMS_ENABLED`,
which is too blunt (it locks out students already mid-exam, violating
"preserving safe completion").

**Fix:** `EXAM_NEW_ADMISSIONS_PAUSED` (new config flag, default `False`).
Checked inside `ExamStudentService.admit()`
([backend/app/domain/exams/student_service.py](../../backend/app/domain/exams/student_service.py))
**after** the existing idempotent already-admitted-return check, so:

- A genuinely new NOT_STARTED -> ADMITTED transition is blocked
  (`PermissionError("new_admissions_paused")` -> HTTP 403) when the flag is set.
- A student already ADMITTED/IN_PROGRESS calling `admit()` again (page
  refresh/retry) is **completely unaffected** — the idempotent return happens
  before the new check is ever reached.
- The existing `run_not_released` check still takes priority (a draft/paused/
  closed run's own state is unaffected by this global flag).

Tests: [backend/tests/test_exams_student_service.py](../../backend/tests/test_exams_student_service.py)
— 4 cases: normal admission when not paused, blocked brand-new admission when
paused, idempotent already-admitted student unaffected when paused, and
run-released check still takes priority over the pause flag.

Operational use: see [rollback-runbook.md](rollback-runbook.md).

## 3. Load testing (local, honestly caveated)

**Hardware/environment:** single Windows developer machine, no dedicated load
generator, no real network hop (loopback only). These numbers describe this
machine's local ceiling for the specific code paths exercised below — **they
are not production capacity evidence** and must not be quoted as such without
re-running against real staging infrastructure with a real load generator.

**Firestore backend used:** the local **Firestore emulator**
(`gcloud emulators firestore start`, installed and run in this sandbox — no
real GCP project or credentials exist in this workspace; see §6). The
emulator is a real Firestore-compatible server (genuine transactions, genuine
`firestore.transactional` semantics), so the admission logic below was
exercised for real — but the emulator has different latency/consistency/quota
characteristics than production Firestore (typically slower for small ops
locally, no real network hop, no multi-region replication, no real quota
enforcement). Auth was satisfied with unsigned, emulator-only JWTs (accepted
only because the target server had `FIREBASE_AUTH_EMULATOR_HOST` set, which
makes `firebase_admin.auth.verify_id_token` skip signature verification
entirely — confirmed empirically; this never works against a real Firebase
project).

**What was seeded:** [backend/scripts/exam_staging_seed.py](../../backend/scripts/exam_staging_seed.py)
(new, reusable against real staging too) created one exam definition, one
published revision, and one RELEASED run with 60 synthetic enrolled student
uids via the real `ExamCommandService` (create -> publish -> release), not
hand-written Firestore documents.

**Exam admission load** ([backend/scripts/exam_load_test.py](../../backend/scripts/exam_load_test.py),
new): 20 workers, 20s, each of the 60 synthetic students repeatedly calling
`POST /runs/{run_id}/admit` (first call = real admission transaction, every
call after = idempotent "waiting room refresh" replay):

| Run | Requests | Success rate | Achieved RPS | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| Admission load alone | 1216 | 100% | 60.3/s | 299ms | 486ms | 945ms | 1022ms |
| Admission load **concurrent with** legacy search load (below) | 1540 | 100% | 76.6/s | 249ms | 352ms | 529ms | 579ms |

All 60 distinct students were admitted exactly once in both runs (no
double-admission, no lost admission) — the transactional idempotency
guarantee held under concurrency in both cases. No errors in either run, and
no measurable degradation while legacy search traffic ran concurrently
(reasonable isolation signal between the two independently-deployable FastAPI
processes).

**Concurrent legacy search load** (existing [backend/scripts/load_test.py](../../backend/scripts/load_test.py),
re-run against the same emulator-backed legacy API): 15 workers, 25s,
target 60 RPS, 90% `/api/search` / 10% `/health`:

| Requests | 2xx | 429 (rate-limited) | p50 (when not limited) | p95 | max |
|---|---|---|---|---|---|
| 1500 | 269 (17.9%) | 1231 | 19ms | 145ms | 1764ms |

The high 429 rate is `SEARCH_RATE_LIMIT_PER_MINUTE=120` (the configured
per-identity/per-key default) correctly rejecting a single-source burst far
above its own limit — **this is the rate limiter working as designed**
(ADR 0005), not a defect. It also means this specific run does not measure
sustained real-world multi-user search throughput; a multi-identity load
generator would be needed for that, which is out of scope here.

**What this load test does NOT cover** (explicit, not silently skipped):
- `get_current_question` / `save_draft` / `submit` / judge / proctoring
  endpoints — these need real published exam content (a blueprint with
  generated questions), which this seed script's minimal footprint does not
  create. Only the admission path was load-tested.
- Any real network latency, multi-instance/horizontal-scaling contention, or
  sustained multi-hour duration.
- Real concurrent faculty live-control (WebSocket) traffic alongside student
  traffic.
- Real Firestore (only the local emulator was available in this sandbox —
  no GCP project/credentials exist in this workspace at all; confirmed by
  attempting to start the legacy server with no credentials configured,
  which failed every Firestore-touching call with
  `ValueError: Firebase credentials not configured`, while `/health` and
  background tasks degraded gracefully rather than crashing).

**Recommendation:** re-run both scripts against a real staging Firestore
project (not the emulator) with a real external load generator (e.g. the
existing `loadtest/search_load.js` k6 script, extended with an equivalent
exam-admission scenario) before any real cohort is scheduled.

## 4. Security findings

**Backend (`pip-audit`, new one-off scan — not yet wired into CI):**

| Package | Current | Findings | Fix |
|---|---|---|---|
| `starlette` | 0.45.3 | Multiple published CVEs (PYSEC-2026-1941/1942/161/2281/2280/249/248) | Needs 0.47.2+ (check FastAPI's pinned compatible range before bumping) |
| `transformers` | 4.48.3 | ~20 published advisories, mostly unsafe-deserialization-of-model-file class issues | Needs 4.53.0+ (legacy search only; exam API does not import this — see `Dockerfile.exams`) |
| `sentence-transformers` | 3.4.1 | 1 advisory | Needs 5.6.0+ |
| `python-dotenv` | 1.0.1 | 1 advisory (low practical impact — local `.env` parsing only) | Needs 1.2.2+ |

None of these were introduced by the Exams module — all are pre-existing
legacy dependencies. `starlette` is the most relevant to flag given it
underlies every FastAPI route (including every exam endpoint); recommend
upgrading before real production release and wiring `pip-audit` into CI
(not done in this phase — a process change beyond this report's scope).

**Frontend (`npm audit --omit=dev`, re-confirmed this phase):** 10
vulnerabilities (2 moderate, 6 high, **2 critical**) — all fixed by bumping
`next` from the currently-pinned `16.2.10` to `16.3.8`+. The 2 critical
findings are **direct Next.js advisories**, not just transitive dependencies:
an unauthenticated RCE on Windows-hosted servers, and an unauthenticated RCE
in the Image Optimization API (AVIF). Given this module may be deployed on a
Windows-hosted target, **this is a release-blocking recommendation**, not a
footnote — upgrade `next` before any real production rollout, independent of
anything else in this report.

**Secrets scan:** grep across `backend/app/**` and `frontend/src/**` for
hardcoded API keys/secrets/private-key material — no matches. All credentials
are environment-variable-driven (`FIREBASE_SERVICE_ACCOUNT_JSON`,
`EXAM_OTP_HMAC_SECRET`, provider API keys, etc.), consistent with every prior
phase.

**Firestore rules:** re-confirmed the default-deny catch-all
(`match /{document=**} { allow read, write: if false; }`) in
[firestore.rules](../../firestore.rules) still covers every exam-related
collection (`exam_definitions`, `exam_runs` + `attempts`/`drafts`
subcollections, `exam_command_receipts`, `exam_outbox`, `kt_uploads`,
`kt_approved_sources`, `exam_blueprints`, `generation_requests`,
`generation_candidates`, `generation_malformed_candidates`,
`exam_variant_assignments`, `institutions`, `institution_memberships`,
`institution_roster_imports`, `institution_otp_challenges`) — none of them
has a client-facing rule; all access is Admin-SDK-only through the backend.

## 5. Chaos / failure-scenario mapping

| Scenario | Evidence | Status |
|---|---|---|
| Network disconnect (student) | Live socket + REST fallback (Prompt 7) | Covered by existing unit/integration tests |
| Lost response (double-submit) | Submit/finish idempotency tests (Prompt 4) | Covered |
| Expired/revoked identity mid-session | `authorization_version` staleness checks (Prompt 2/10) | Covered |
| Stale session / concurrent tabs | Epoch-based concurrency tests (Prompt 3/4) | Covered |
| API/worker process restart | Outbox drain idempotency, judge submission sweep, finalization sweeper tests (Prompt 3/5/6/8) | Covered |
| Redis unavailable | `SlidingWindowLimiter._get_redis()` catches all exceptions, logs, falls back to in-memory per-process windows (`backend/app/core/rate_limit.py`) — design-level guarantee, re-verified by code read this phase | Covered by construction; **not exercised against a real Redis outage** (no Redis configured in this sandbox at all) |
| Generation provider outage | `DeterministicFakeProvider` + insufficient-capacity/timeout tests (Prompt 5) | Covered against the fake; **never exercised against a real provider's real outage behavior** |
| Judge provider outage | `INFRA_ERROR` retry/pending tests (Prompt 6), re-verified in Prompt 9's "pending infra failures" gradebook test | Covered against the fake/deterministic judge; **never exercised against a real Judge0 outage** |
| Database (Firestore) outage | — | **Not testable in this sandbox** — no real Firestore project exists here, and the local emulator cannot simulate a real regional outage. Confirmed this phase: starting the legacy server with zero credentials makes every Firestore-touching call fail immediately, while non-Firestore paths (`/health` liveness, background-task scheduling) degrade gracefully rather than crashing the process — a reasonable baseline signal, not a real-outage rehearsal |
| Mass concurrent admission at run-open | Real emulator-backed load test, this phase (§3) — 60/60 distinct students admitted exactly once under concurrency, 100% success | Covered for real, at local/emulator scale only |
| Rollback mid-exam (new-admissions-pause) | New service-level tests, this phase (§2) | Covered |

## 6. Environment constraints discovered this phase

- **No real Firebase/GCP project exists anywhere in this workspace** — no
  `backend/.env`, no service account file. Confirmed by attempting to start
  the legacy API with zero credentials: every Firestore-touching call fails
  immediately with `ValueError: Firebase credentials not configured`.
- A local **Firestore emulator** (`gcloud emulators firestore start`) and
  **unsigned-JWT-based emulator auth** were used to get genuine (if
  non-production-scale) transactional coverage for §3's load test and to
  validate `exam_staging_seed.py` end-to-end. This is a meaningfully stronger
  evidence tier than pure in-memory fakes, but it is still not real
  Firestore/real Firebase Auth — treat §3's numbers accordingly.
  Credentials used were a locally-generated, throwaway RSA key wrapped in a
  fake service-account-shaped JSON, used only to satisfy the Admin SDK's
  credential-loading requirement for talking to the emulator — never
  registered with or usable against any real Google service, and deleted
  after use (not committed).
- The legacy search corpus could not be seeded for a content-bearing search
  load test: `sentence-transformers/all-MiniLM-L6-v2` failed to download
  from Hugging Face Hub in this sandbox (`401 Unauthorized`) — a pre-existing,
  unrelated environment gap (no HF token configured), not something this
  phase fixed. The legacy search-quality CI gate (§1) is unaffected since it
  uses a fixed, dependency-light BM25 fixture, not this embedding model.

## 7. Accessibility, PWA, managed-lockdown

No new automated coverage was added this phase (Prompt 10 already covers
this — see
[accessibility-and-lockdown-checklist.md](accessibility-and-lockdown-checklist.md)
for the full automated-vs-manual breakdown and support matrix). That
checklist's "NOT performed in this environment" list (real screen reader
testing, real SEB client pilot, real PWA install flow, cross-browser/device
matrix) remains fully open and is restated here as a release gate, not
re-verified.

## 8. Cost, capacity planning, and alerting

**Cost measurements:** not produced in this report — this requires real
billing data from a real deployment (Firestore read/write/storage costs,
evidence-storage egress, any paid generation/judge/malware-scan provider
usage). None of that exists in this sandbox. Do not estimate production cost
from the local/emulator load test numbers in §3.

**Proposed alert thresholds** (based on measurable signals already emitted by
existing code, not new instrumentation):
- Outbox lease staleness / queue age (`EXAM_OUTBOX_LEASE_SECONDS`,
  `EXAM_OUTBOX_MAX_ATTEMPTS`) — alert if entries exceed max attempts without
  resolving.
- Judge submission retry count approaching `EXAM_JUDGE_PROVIDER` timeout
  budget (Prompt 6).
- Search-quality CI gate (§1) regressing below its fixed thresholds on any
  future change — already a pass/fail gate, wire it into CI if not already.
- Firestore health check (`firestore_health_check()` in
  `backend/app/core/health_checks.py`) reporting `status: error`.

## 9. Remaining institutional decisions (compiled from every prior ADR)

None of the following are code gaps — they are real-world provider/process
decisions only an adopting institution can make, restated here in one place
for release planning:

| Decision | Source ADR | Current safe default |
|---|---|---|
| Real malware-scan provider | 0009 | `NullMalwareScanner` (never reports "clean") |
| Real generation-model provider/endpoint | 0009 | `DeterministicFakeProvider` (test/dev only) |
| OCR provider for scanned documents | 0009 | Not implemented |
| OTP delivery provider (SMS/email) + shared `EXAM_OTP_HMAC_SECRET` for multi-instance | 0010 | `DevSinkOtpDelivery` (dev-only sink) + random per-process secret |
| Real Judge0-compatible hardened isolated compute | 0011 | Deterministic fake judge (never executes submitted code) |
| Evidence storage bucket (`EXAM_EVIDENCE_STORAGE_BUCKET`) | 0013 | `InMemoryEvidenceStorage` (dev/test only — does not survive a restart) |
| Real JPlag CLI jar + Java runtime (`EXAM_JPLAG_JAR_PATH`) | 0014 | Deterministic fake similarity provider |
| SEB Config Key / Browser Exam Key values, piloted platforms list | 0015 | Lockdown disabled by default; fails closed if enabled-but-unconfigured |
| `next` dependency upgrade to 16.3.8+ (2 critical RCE advisories) | This report, §4 | N/A — pre-existing, not exam-specific |
| `starlette`/`transformers` dependency upgrades | This report, §4 | N/A — pre-existing, not exam-specific |

## 10. Release recommendation

**Not yet ready for a real institutional production release.** Every piece of
code-level logic available to test in this sandbox (unit tests, the search
quality gate, and — new this phase — a real emulator-backed load test of the
admission path) passes. But the following release gates are explicitly
**not** evidenced yet, consistent with "report incomplete gates as such"
rather than claiming sandbox results stand in for them:

1. A real staging Firestore project + real load generator re-run of §3.
2. Real infrastructure chaos testing (real Redis outage, real provider
   outages, real database outage) — only verified by construction/code
   review in this sandbox, per §5.
3. Real backup/restore RPO/RTO measurement for exam-specific collections and
   the evidence storage bucket (§9 of the updated
   [backup-dr.md](../runbooks/backup-dr.md)).
4. Real cost data (§8).
5. Manual accessibility, SEB client pilot, and PWA install verification
   (§7 / the Prompt 10 checklist — unchanged, still fully open).
6. The institutional provider decisions in §9, each of which currently falls
   back to a safe-but-non-functional default.
7. Upgrading `next`/`starlette`/`transformers` to clear the security findings
   in §4 (not exam-specific, but blocking for any real production release of
   this codebase).

Once a real staging environment exists, re-run §1 and §3 against it, execute
the Prompt 10 manual checklist, and revisit this report before scheduling any
real exam.
