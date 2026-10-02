# 0016 — Production Qualification and Release Package

Status: Accepted
Date: 2026-10-02

## Context

Prompts 1-10 built the Exams module feature-by-feature, each with its own
unit/e2e test coverage and ADR. None of that work included a cross-cutting
qualification pass: a real (if locally-scoped) load test of the module's
transactional core, a dependency security audit, an explicit chaos/failure-
scenario mapping, or a single rollback mechanism that satisfies contract #12
("disable new admissions while preserving safe completion") at the *global*
level — only the per-run pause half of that contract existed.

Prompt 11 asked for exactly this: production qualification and a release
package, built honestly against this sandbox's real constraints (no real
Firebase/GCP project, no real staging environment) rather than claiming local
results prove production readiness.

## Decision

### 1. New rollback lever: `EXAM_NEW_ADMISSIONS_PAUSED`

Added a global config flag, checked inside `ExamStudentService.admit()`
*after* the existing idempotent already-admitted-return check. This means it
blocks only genuinely new NOT_STARTED -> ADMITTED transitions, across every
run, while every already-admitted/in-progress student is completely
unaffected by it. Combined with the pre-existing per-run `RunStatus.PAUSED`
command, this closes contract #12 in full: narrow (per-run) and broad
(global, admissions-only) rollback levers both exist, neither of which ever
deletes data or disrupts a student who is safely mid-exam. See
[rollback-runbook.md](../exams/rollback-runbook.md) for the full decision
guide across all three available levers (per-run pause, admissions-pause,
module-wide `EXAMS_ENABLED=false`).

### 2. Real (emulator-backed) load test, not just in-memory fakes

Rather than only asserting "mock tests prove this works" — which the master
prompt explicitly warns against — this phase installed and ran a local
**Firestore emulator** (`gcloud emulators firestore start`) and validated
that `firebase_admin.auth.verify_id_token` accepts unsigned, emulator-only
JWTs when the target process has `FIREBASE_AUTH_EMULATOR_HOST` set (an
empirically-confirmed, zero-network-call code path — never valid against a
real Firebase project). This unlocked a genuine, if local-scale, end-to-end
load test of the real `ExamCommandService`/`ExamStudentService` transactional
code against a real Firestore-compatible backend: `exam_staging_seed.py`
(new, also reusable against real staging) creates a real released run, and
`exam_load_test.py` (new) drives real concurrent admission traffic against
it. Full results, and their explicit limits (emulator ≠ production Firestore,
admission-path only, no real network/multi-instance contention), are in
[production-qualification-report.md §3](../exams/production-qualification-report.md).

### 3. Security pass

A one-off `pip-audit` scan (backend) and a re-confirmed `npm audit`
(frontend) surfaced real, actionable findings — most notably two **critical**
direct Next.js advisories (not just transitive dependencies), including an
unauthenticated RCE on Windows-hosted servers. None of these are
exam-specific, but they are release-blocking for the codebase as a whole and
are recorded in
[production-qualification-report.md §4](../exams/production-qualification-report.md)
rather than silently fixed mid-phase or silently ignored.

### 4. Chaos/failure-scenario mapping

Every named scenario (network disconnect, lost response, expired identity,
stale session, process restart, Redis/generation/judge/database outage, mass
concurrent admission) was mapped to either existing test evidence or an
explicit "not testable in this sandbox" statement — see
[production-qualification-report.md §5](../exams/production-qualification-report.md).

## Consequences

**Positive:**
- A real, if narrow, production rollback gap (contract #12) is closed with
  tests.
- The module now has one real (not purely mocked) transactional load-test
  data point, reusable against real staging infrastructure later with no
  code changes (`exam_staging_seed.py`/`exam_load_test.py` have no
  emulator-specific logic baked in — only the test *runner* optionally mints
  emulator-only tokens, clearly gated behind an explicit flag).
- Every institutional provider gap across all 10 prior ADRs is compiled into
  one onboarding checklist instead of being scattered across documents.

**Negative / known limitations (explicit, not silently omitted):**
- No real Firestore, no real Firebase Auth, no real network, no real
  multi-instance deployment was exercised — the emulator is a meaningfully
  stronger evidence tier than in-memory fakes, but it is not production
  infrastructure.
- Only the admission endpoint was load-tested; question/draft/submit/judge/
  proctoring traffic was not, because exercising it needs real published
  exam content (blueprint + generated questions) that this phase's minimal
  seed script does not create.
- Real chaos testing (actually killing a Redis/judge/generation/database
  instance under load) was not performed — only verified by construction
  (code review of exception-safe fallback paths) or against deterministic
  fakes.
- Cost, real backup/restore RPO/RTO, and manual accessibility/SEB-client
  verification remain entirely open, as stated in
  [production-qualification-report.md §10](../exams/production-qualification-report.md),
  which concludes the module is **not yet ready for a real institutional
  production release**.

## Migration / configuration steps

- New setting, default `false`, no migration required:
  `EXAM_NEW_ADMISSIONS_PAUSED`.
- No new Firestore collections, no `firestore.rules` changes.
- New scripts: `backend/scripts/exam_staging_seed.py`,
  `backend/scripts/exam_load_test.py` — both safe to run against a real
  staging project (seed script writes real documents via the real command
  service; load test requires real auth tokens unless
  `--emulator-insecure-tokens` is explicitly passed, which only ever works
  against an emulator-backed target).
