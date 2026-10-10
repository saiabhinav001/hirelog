# ADR 0024 — Judge hardening: no silent verdicts, durable grading, a server-owned registry

**Status:** Accepted (the real judge adapter and SQL remain **not** approved — decision D-5)

## Context

Prompt 8 asks for coding (and, once qualified, SQL) grading through a separate hardened judge and a durable worker: a server-owned
language registry, pinned versions, results bound to the submitted code / variant / suite version, server-owned resource limits with no
network, hidden tests kept out of the student's reach, infrastructure failures pending and retryable (never a silent zero), polled
and de-duplicated results — tested with correct and incorrect code, compile/runtime errors, output floods, limits, network/metadata
denial, hidden-data leakage, queue saturation, duplicate results, worker crash, infrastructure retry and regrade lineage.

The prompt says to implement execution **only after the judge architecture is approved**. It is not: D-5 (provider, language set, SQL
runner qualification) is open, `Judge0HttpAdapter` still raises `NotImplementedError`, and nothing in this repository can show that a
sandbox isolates code. This phase therefore changes only what the application itself does around the judge, and leaves the adapter and
SQL out. Reading the existing worker against the list found defects that matter even with the fake provider:

1. **The dev fake was the production judge when nothing was configured.** `DeterministicFakeJudgeProvider` answers "Accepted" to every
   test. Without `EXAM_JUDGE_PROVIDER_*` a production deployment would have shown students "passed" and, at finalization, awarded full
   marks to code that never ran.
2. **Infrastructure errors were never retried.** `JudgeWorker.retry_infra_errors` had no caller, it took one test list for all
   submissions, there was no attempt cap and no terminal state, and a submission created but never sent (crash, provider down) was not
   picked up by anything. The attempt counter only recorded.
3. **A regrade that was not instant was lost.** `regrade` submitted and polled once; if the judge was still working, the background
   sweep later found an existing result and returned it, so the new version was never written.
4. **A crash lost results.** The submission was marked done before its grading result was written; a crash in between left a finished
   submission with no grade, and nothing looked at it again.
5. **Retries duplicated work.** Each outbox retry created a new final-grade submission (re-running the student's code and producing
   a second result); MCQ results and short-answer reviews were duplicated too, and re-creating a review could reset one already scored.
6. **One bad submission stopped the sweep.** An exception in one poll aborted the tick, so the same first-listed submission could
   starve every other one behind it.
7. Smaller: the compiler's message was dropped (a student saw an empty box for a compile error); stdout/stderr were stored unbounded
   (a flooding program could bloat or break the grading write); only CPU time and memory were sent as limits; the suite version was a
   constant `1`; the public-run poll polled before checking whose submission it was; an unlisted language key raised in the middle of an
   attempt's grading loop and skipped the remaining questions.

## Decisions

### 1. Nothing is graded by a stand-in
`UnconfiguredJudgeProvider` is used when no provider is configured **and** `ENV=production`: every call raises `JudgeUnavailableError`.
The fake stays the default outside production. A configured `Judge0HttpAdapter` still has no implementation behind it, so today
configuring `EXAM_JUDGE_PROVIDER_*` produces infrastructure errors, never verdicts - by design until D-5 is decided.

### 2. Provider failure is pending work, then an operator's work - never a score
`JudgeWorker.submit` no longer raises on a provider or transport failure: it records `infra_error`, counts the attempt and stores only
the exception class name (no URL, key or message). The sweep (`JudgeService.sweep_pending_once`, same loop as before) then, for sealed
grading only:
- resends `pending` and `infra_error` submissions after an exponential backoff (15 s doubling to 10 min);
- after `EXAM_JUDGE_MAX_INFRA_ATTEMPTS` failures, or `EXAM_JUDGE_STALL_SECONDS` without a result, sets `infra_exhausted` and logs an error;
- sets `infra_exhausted` for an unusable language key instead of failing the attempt.
`infra_exhausted` is terminal for the machine and waits for a person: `GET /faculty/judge/runs/{id}/stalled-submissions` lists them (no
source code) and `POST …/requeue-stalled` returns them to `pending` with a fresh budget, audited with the actor. A question with no
grading result stays **PENDING** in the gradebook - it is never scored zero. A student's own test run is not retried on their behalf.

### 3. Results are written before work is marked done, under deterministic ids
`poll_one(commit=…)` lets the caller persist results first; the submission is marked `succeeded` in the same write that clears any
regrade marker. Final submissions have the id `final-{hash(run, student, question, variant)}` and are created with create-if-absent, so a
repeated trigger resumes the same one; grading results are `{submission}-v{n}`; first MCQ results and short-answer reviews are created
if absent and never reset. A regrade is recorded on the submission (`regrade_reason`, `regrade_by`, `regrade_base_version`) before it is
sent, so it lands exactly once as the next version whenever the result arrives, and earlier versions are never touched.
Each submission in a sweep is handled on its own: a failure is logged and the others continue.

### 4. Limits and the network are server constants
`JudgeSubmitRequest` carries `JudgeLimits` (CPU 5 s, wall 10 s, memory 256 MB, stack 64 MB, 32 processes/threads, 1 MB file size) and the
Judge0 field mapping always sets `enable_network` to `False`; the switch is a module constant, not a field. The request has no
callback, image, flag or URL field, and the language id comes only from the registry. What is **stored** is bounded too: 16 384 characters
per stream per test. The compiler's output becomes the stderr of a compile error. A student-initiated run is refused with `503 judge_busy`
while `EXAM_JUDGE_MAX_IN_FLIGHT` submissions are waiting; sealed grading is never refused (it is durable work). When a run cannot be
sent the student gets `503 judge_unavailable` ("your code was not run").
**None of this proves isolation.** Enforcement (cgroups, seccomp, egress and metadata blocking, read-only mounts) is the judge's, and
stays UNVERIFIED until staging evidence exists.

### 5. A server-owned registry that does not claim validation
The registry lists the candidate languages: `c17`, `cpp17`, `java17`, `python3.11`, `node18` (JavaScript), `typescript5`, with Judge0 CE
1.13 default ids as a starting point. With the fake judge all are usable. With a real provider configured only keys listed in
`EXAM_JUDGE_VALIDATED_LANGUAGES` are usable, by students **and** in blueprints; an operator adds a key only after checking its id against
the provider's `/languages`. None is validated by default. A result records the runtime image, the submission hash, the variant and the
variant's version as the suite version (it was a constant).

### 6. SQL stays out
There is no SQL item type and no SQL registry entry, and a test pins that. It can be enabled only with a qualified, disposable,
isolated database runner and no production credentials or data (D-5).

## Consequences

- No data migration. New optional fields on `judge_submissions`; deterministic ids apply to new work only. Work in flight at the
  upgrade could produce one extra submission on a retried trigger; reads take the latest version, so scores are unaffected.
- `infra_exhausted` is unknown to the previous code: **requeue or review stalled submissions before rolling back below this change**.
- No new Firestore index is needed by the new queries (`status in`, and two equality filters on `run_id` and `status`); this is not
  verified against a real Firestore.
- Residual risks: no real judge or sandbox exists to test; Judge0 ids and images are unvalidated defaults; the in-flight cap counts a
  bounded sample per request and the limiters are per instance; hidden tests are still sent to the provider with each submission (the
  provider must keep them out of the sandbox); a grading result is not yet visible to teachers in a UI beyond the existing summary route;
  decision D-5 remains the gate for execution, languages beyond what a provider validates, and SQL.
