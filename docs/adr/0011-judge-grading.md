# ADR 0011 — Isolated coding judge and reproducible grading

**Status:** Accepted

## Context

Prompt 5's `GenerationCandidate` can carry `item_type: CODING`, with public
tests students see before submitting and `hidden_test_groups` reserved for
sealed grading (ADR 0009). Nothing graded any of it yet — MCQ/short-answer
candidates had no scoring path either. This ADR adds: a Judge0-compatible
execution provider contract, server-resolved runtime/limits (students only
ever choose a `language_key`), classification of provider results into
scored code failures vs. retryable infra faults, weighted aggregation bound
to submission hash/variant/runtime image/test-suite version, explicit
regrade versioning, MCQ exact-match, a short-answer human-review workflow,
and the wiring that triggers all of this from attempt finalization
(Prompt 3/4's outbox) without modifying the outbox drain loop itself.

Committed on `exams/phase-6-judge-grading`, stacked on top of
`exams/phase-2-institution-identity` (the current tip of the branch stack;
`main` remains untouched throughout).

## Decisions

### 1. Poll-only — no callback endpoint

`JudgeProvider.get_result(token)` is polled by our own worker/sweep loop;
there is no `POST /judge/callback` endpoint accepting provider-pushed
results. The master prompt's "do not assume signed provider callbacks"
makes an untested callback-auth scheme (verifying a provider's signature we
have never actually received or validated) a real risk to design blind. Poll
is slower but requires no inbound trust decision at all — every result this
server acts on, it explicitly asked for. `verify_result_ownership` still
guards the poll path itself: a result is only trusted if its token matches
exactly what this server recorded when it submitted that specific test (see
`submission.py`), which also protects against a provider that mixes up
concurrent queries, not just a hypothetical push-based attacker.

### 2. Runtime allowlist: server-resolved, provider-unvalidated by construction

`CodeSubmission`/the student-facing `PublicRunRequest` DTO carry only
`language_key` (e.g. `"python3.11"`) and `source_code` — there is no field
anywhere for network settings, compiler flags, image IDs, or callback URLs.
`resolve_runtime()` maps `language_key` to a server-side `RuntimeAllowlistEntry`
(`judge0_language_id`, `runtime_image`, resource limits come from constants,
never the request). `default_allowlist()` ships three entries
(python3.11/java17/cpp17) with `validated_against_provider=False` on all of
them — nobody has queried a real Judge0 instance's `/languages` endpoint to
confirm these ids/images are actually what it offers. Treat the default
allowlist as a starting point requiring institutional verification before
go-live, not a validated table.

### 3. Status classification: code failure vs. infra fault, fail-safe to retry

`classify_status()` maps Judge0's status taxonomy to `TestCaseOutcome`:
accepted/wrong-answer/compile-error/TLE/output-limit/runtime-error are all
legitimate **scored** outcomes (including 0 credit for that test) — none of
them retry the submission. Internal-error, exec-format-error, and any
**unknown** status id all classify as `INFRA_ERROR` — provider-side, never
scored as the student's fault. `poll_one()` in `worker.py` will mark the
**whole submission** `INFRA_ERROR` (retryable, `attempts` incremented) if
**any** single test comes back infra-faulted, rather than persisting a
partial/mixed result — a submission is graded fully or not at all for this
pass.

### 4. Every result bound to hash + variant + runtime image + test-suite version

`CodeSubmission.submission_hash` (sha256 of the exact submitted source) and
`runtime_image` (resolved, not requested) travel onto the `GradingResult` it
produces, alongside `candidate_id` (variant) and `test_suite_version`. A
regrade against a different test-suite version or a result that doesn't
match the submitted code's hash is detectable after the fact — reproducing
"what was actually graded, against what, and on what image" doesn't depend
on trusting anything logged out-of-band.

### 5. Regrades are explicit new versions; nothing is ever mutated in place

`GradingResult` is immutable once created. `create_regrade()` always returns
a **new** document with `version = previous.version + 1`, `graded_by` set to
the reviewing faculty member, and a required `reason` string — the original
submission, its hash, and every prior graded version stay exactly as they
were. The same version-bump pattern is reused for short-answer review
(`ShortAnswerReview.version` increments if a faculty member updates an
already-`REVIEWED` item).

### 6. Idempotent by construction — safe to crash, restart, or double-invoke

`poll_final_grade()` checks `GradingResultRepository.latest_for_submission`
**before** creating anything — a second call (duplicate provider poll,
worker crash-and-restart, a concurrent sweep tick) after a result already
exists just returns the existing one rather than creating a second. `poll_one()`
treats a submission that's already left the `SUBMITTED`/`POLLING` states as
a safe no-op. `retry_infra_errors()` and the background sweep are both
bounded, single-pass, and re-runnable from scratch at any time — there is no
in-memory-only state a restart would lose (contract: durable/restart-safe
workers).

### 7. Sealed grading is decoupled from `RunStatus` — grading never stops at exam end

`JudgeService`/`worker.py` never look up `ExamRun`/`RunStatus` at all.
Grading a `FINAL_GRADE` submission proceeds purely from the submission's own
recorded `run_id`/`uid`/`question_id`/`candidate_id` — whether the run is
`ACTIVE`, `CLOSED`, or long archived makes no difference, and grading a
submission whose `run_id` matches no live run at all still succeeds (a
regression test asserts this explicitly). This is deliberate: "late grading
after exam end" must always be able to complete, including provider retries
that land well after the exam window closed.

### 8. Student-safe DTOs strip hidden data by construction, not by a redaction step

`TestCaseResult` has no `expected_output` field at all — it was never copied
in from the hidden test spec in the first place, so there's no redaction
step to forget. `public_test_result_dto()` is only ever invoked by the
public-run route, using only the candidate's `public_tests` group (hidden
groups are never even submitted for a `PUBLIC_RUN` purpose — see
`JudgeService.run_public_test`). `sealed_grade_summary_dto()` — the only
thing a student's final-grade view ever receives — returns aggregate counts
only (`tests_passed`/`tests_total`/`weighted_score`/`max_score`/`version`),
never per-test stdout/stderr, hidden or public. A test asserts a distinctive
marker planted in hidden test stdout/stderr can never appear anywhere in
that summary's output.

### 9. Background sweep completes grading nobody is polling over HTTP

`GradingTriggerOutboxHandler` (triggered once, at attempt finalization) only
*creates and submits* a `FINAL_GRADE` `CodeSubmission` — nobody is polling
it over HTTP the way a student polls their own public run. A new bounded
background thread (`_judge_sweep_loop` in `exam_main.py`, mirroring the
existing outbox-drain thread) calls `JudgeService.sweep_pending_once()`
every 2 seconds, which queries `list_pending_or_polling` and drives each
submission's public-run or final-grade poll to completion. Both this thread
and the outbox-drain thread only start when `EXAMS_ENABLED=true`.

### 10. Outbox dispatch: composite handler, no change to the drain loop

`drain_outbox_once` itself is untouched. A new `CompositeOutboxHandler`
(small, additive change to `app/domain/exams/outbox.py`) fans one entry out
to a list of handlers; each handler decides for itself whether `entry.kind`
applies. `exam_main.py` now wires `[LoggingNoopHandler(), GradingTriggerOutboxHandler(...)]`
— the exact seam ADR 0008 reserved for this phase (§8 "real consumers...
register their own handler later without changing the drain loop").

### 11. MCQ and short-answer are graded here too, not a separate module

Exact-match MCQ grading and the short-answer human-review workflow live in
this same judge domain rather than a fourth module, because all three
(coding/MCQ/short-answer) share the same trigger (attempt finalization via
the outbox), the same "immutable result + explicit regrade version" pattern,
and the same repository/DTO conventions. `GradingTriggerOutboxHandler`
dispatches by `candidate.item_type` to whichever of the three applies.

## Consequences

- **Positive:** 74 new tests (Judge0 status classification for every status
  family, weighted scoring, MCQ exact-match, regrade versioning, hidden-data
  non-leakage, runtime allowlist resolution, submission hashing and result-
  ownership verification, the deterministic fake provider's non-execution
  guarantee, worker submit/poll for correct/incorrect/compile/runtime/limit/
  output-flooding/infra-fault/duplicate-poll/crash-restart/mismatched-token
  scenarios, service-layer quota enforcement and idempotent final-grading,
  and outbox-dispatch routing by item type including every skip condition) —
  full suite 289/289 passing, ruff clean, `compileall` clean. A real repo
  bug was caught and fixed *during* this work by writing these tests first:
  `retry_infra_errors` queried the wrong repository method and would never
  have retried anything.
- **Negative / follow-up required:** No real Judge0-compatible instance was
  available to integrate or test against — `Judge0HttpAdapter` is a genuine
  `NotImplementedError` stub, exactly like the generation/malware-scanner
  providers in ADR 0009. **Mock/fake adapter tests here do not, and cannot,
  certify real sandbox security** — they prove the grading/classification/
  idempotency *logic* is correct given honest provider responses; they say
  nothing about whether a real deployment actually isolates untrusted code
  execution. Before pointing `EXAM_JUDGE_PROVIDER_BASE_URL` at a live
  instance, an institution must separately provision and evidence, against
  that real deployment (not this repo): network egress blocked from the
  execution sandbox (including cloud metadata endpoints), no secrets or host
  mounts reachable from the execution container, pinned/immutable runtime
  images, enforced CPU/memory/process/output-size/wall-clock limits per
  execution, and reliable post-execution cleanup — staging isolation
  evidence must be collected and reported separately from this repo's test
  results. Runtime allowlist entries are shipped with
  `validated_against_provider=False` and must be confirmed against the real
  provider's actual language table before relying on them. The judge
  domain's own Firestore-touching layer (`repository.py`, `worker.py`'s
  repo calls, `service.py`, `outbox_handler.py`) is — like every prior
  phase's transactional shell — exercised only against in-memory fakes
  constructed via the `Service.__new__(Service)` pattern already established
  in Prompt 2's tests, not a real Firestore/emulator run (ADR 0008 §1, ADR
  0010 "Known limitations"). No CSV/file upload path exists for bulk regrade
  or bulk short-answer review — both are single-item endpoints for now.

## Migration / configuration steps

- New settings, safe defaults (unset = fake/deterministic provider, never a
  real sandbox): `EXAM_JUDGE_PROVIDER_BASE_URL`, `EXAM_JUDGE_PROVIDER_API_KEY`.
- New Firestore collections (`judge_submissions`, `judge_grading_results`,
  `judge_mcq_results`, `judge_short_answer_reviews`) — no rules changes
  needed (default-deny catch-all; documentation comment added in
  `repository.py`).
- New background thread (`_judge_sweep_loop`) starts alongside the existing
  outbox-drain thread, only when `EXAMS_ENABLED=true`.
- New routes under `/api/exams/v1/student/judge/*` (public run, quota'd via
  a dedicated `SlidingWindowLimiter`, 10 requests / 5 minutes per
  run+uid+question) and `/api/exams/v1/faculty/judge/*` (grading-result
  lookup, regrade, pending short-answer review list, review submission) —
  all behind the existing `require_exams_enabled`/`require_exam_faculty`
  dependencies, no new auth mechanism.
- No data migration. Prompts 3/4/5's files changed only additively:
  `GenerationCandidate.hidden_test_groups` (new optional field, Prompt 5's
  model), `app/domain/exams/outbox.py` gained `CompositeOutboxHandler`
  (new class, `LoggingNoopHandler` untouched), `exam_main.py` gained one new
  provider-selection helper, one new background thread, and two new
  `include_router` calls.

## Next-phase prerequisites (for whichever prompt owns student/faculty UI)

- `GET /api/exams/v1/faculty/judge/short-answer-reviews/pending` and
  `POST .../short-answer-reviews/{review_id}` are ready for a faculty
  review screen.
- `POST /api/exams/v1/student/judge/public-run` +
  `GET .../public-run/{submission_id}` are ready for an in-browser code
  editor/run panel; the response never includes hidden-test data.
- `GET /api/exams/v1/faculty/judge/grading-results/{submission_id}` and
  `POST .../regrade` are ready for a gradebook/regrade UI.
