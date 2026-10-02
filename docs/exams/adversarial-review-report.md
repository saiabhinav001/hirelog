# Exams — Final Adversarial Review

Scope: an evidence-based attempt to falsify 13 named invariants against the
completed Hirelog Exams implementation (Prompts 1-11 plus Backend model
prompts A/B/C), per the implementation pack's "Final adversarial review
prompt." **No application code or tests were changed to produce this
report** — every finding below is backed by direct code inspection, existing
test evidence, and a small number of local, read-only runtime checks
(dependency-graph introspection, `git diff` against `main`), all performed
without modifying the codebase, per that prompt's own "Do not rewrite it or
add features" instruction.

Date: 2026-10-02. Branch: `exams/phase-11-production-qualification`
(HEAD at the time of review: `61bae40`).

## Summary

| # | Invariant | Verdict |
|---|---|---|
| 1 | Student cannot grant roles / cross institutions / claim another ID | ✅ Verified passing |
| 2 | Revoked/stale session cannot obtain questions or change answers | ❌ **Confirmed failure** (High) |
| 3 | No locked content in student response/cache | ✅ Verified passing |
| 4 | Final submission immutable under retries/races | ✅ Verified passing |
| 5 | Client time/refresh/disconnect cannot extend deadlines or reset warnings | ✅ Verified passing |
| 6 | Telemetry replay/correlation cannot manufacture warnings | ✅ Verified passing |
| 7 | Camera/network failures are not auto-misconduct | ✅ Verified passing |
| 8 | Uploaded KT cannot control tools or auto-approve | ✅ Verified passing |
| 9 | Code execution isolation / no hidden-input leak | ⚠️ Partially verified / untested risk (real sandbox) |
| 10 | Grading retries/regrades preserve exact lineage | ✅ Verified passing |
| 11 | Evidence/grade exports require scope and expire | ✅ Verified passing |
| 12 | PWA updates don't interrupt exams or cache sensitive content | ✅ Verified passing |
| 13 | Legacy auth/search/practice/anonymity compatibility | ✅ Verified passing |

**One confirmed failure, one partially-verified/untested-risk item, eleven
verified passing controls.** See §1 for the confirmed failure in full detail,
§2-§12 for every other invariant's evidence, and §13 for the release
recommendation.

## 1. CONFIRMED FAILURE — revoked/stale institutional membership has no effect on an active exam session

**Severity: High.**

**Invariant tested:** "A revoked/stale session cannot obtain questions or
change answers."

**Finding:** `require_active_institution_membership`
([backend/app/api/exams_dependencies.py](../../backend/app/api/exams_dependencies.py)
line 61) is the ONLY code path in this codebase that re-checks a student's
or faculty member's institutional membership status and `authorization_version`
against current Firestore state on a live request — its own docstring even
says "Used by the exam-auth dependency on every request." **It is not.** A
direct runtime inspection of every registered FastAPI route's dependency
graph (91 routes, including WebSocket routes) in `app.exam_main:app` found
**zero** routes that depend on it, directly or transitively:

```
Total exam routes inspected: 91
Routes depending on require_active_institution_membership: 0
```

Every student exam route
([backend/app/api/routes/exams_student.py](../../backend/app/api/routes/exams_student.py):
`/runs`, `/runs/{run_id}/state`, `/runs/{run_id}/admit`,
`/runs/{run_id}/question`, draft save, submit, finish) depends only on
`get_current_exam_user` or `require_seb_admission` (which wraps it) —
neither of which calls `InstitutionAuthService.check_active_membership` or
reads `authorization_version` at all
([backend/app/api/exams_dependencies.py](../../backend/app/api/exams_dependencies.py)
lines 25-33). `get_current_exam_user` only verifies the Firebase ID token's
cryptographic validity (`get_current_user_uncached`) and the `EXAMS_ENABLED`
feature flag — nothing about current institutional standing.

**Reproduction (code-path trace — no live Firestore needed to confirm):**
1. A student signs in via the institution OTP flow; `InstitutionAuthService`
   mints a custom token carrying `exam_authz_version` = the membership's
   `authorization_version` at that moment
   ([auth_service.py](../../backend/app/domain/institution/auth_service.py)
   line 137).
2. The student is mid-exam (admitted, answering questions).
3. An institution admin revokes the membership
   (`InstitutionAuthService.revoke()`,
   [auth_service.py](../../backend/app/domain/institution/auth_service.py)
   line 179) — this bumps `authorization_version` and sets
   `status=REVOKED` in Firestore.
4. The student's browser tab is untouched; their existing Firebase ID token
   is still cryptographically valid (tokens are valid up to their natural
   ~1 hour expiry, independent of this revocation).
5. The student continues calling `GET /runs/{run_id}/question`,
   `POST /runs/{run_id}/draft`, `POST /runs/{run_id}/submit` — every one of
   these succeeds exactly as before, because nothing in the request path
   ever re-reads the membership document or compares `authorization_version`.
   Revocation has **zero observable effect** until the token naturally
   expires and the student is forced to sign in again (at which point the
   OTP/SSO flow — not yet confirmed to also fail gracefully for a revoked
   identity key, see follow-up below — would be the first point of
   enforcement).

**Why this wasn't caught by existing tests:** `require_active_institution_membership`
itself is correctly unit-tested in isolation
([backend/tests/test_institution_reconciliation.py](../../backend/tests/test_institution_reconciliation.py),
3 tests: succeeds, rejects wrong institution, rejects stale authz version) —
those tests call the function directly, so they pass regardless of whether
any route actually depends on it. No test asserts the function is reachable
from an HTTP request.

**Root cause (architectural, not a simple oversight):** `ExamRun`
([backend/app/domain/exams/models.py](../../backend/app/domain/exams/models.py))
has no `institution_id` field at all — `enrolled_uids` is, per its own
code comment, "a direct UID list captured at publish time, standing in for
Prompt 2's institution/roster membership repository" (ADR 0008). Prompt 2
later built real institution membership with `authorization_version`
revocation, but no subsequent phase reconciled the two systems. There is
currently no way to even determine "which institution does this run belong
to" from an `ExamRun` alone, which is why `require_active_institution_membership`
— which needs an `institution_id` — was never wired into the student exam
routes: doing so isn't a one-line change with the current data model.

**Impact:** Any institution that revokes a student's or faculty member's
access mid-session (disciplinary action, enrollment correction, offboarding,
a compromised account) cannot actually cut off their exam access faster than
that user's current Firebase ID token's natural expiry. For the student
exam-taking path specifically, this is a real integrity/access-control gap,
not just a theoretical one.

**Minimal correction (described, not implemented — "do not rewrite" applies
to this review):**
1. Add an `institution_id: Optional[str]` field to `ExamRun` (or resolve it
   via the enrolled student's own membership lookup at admission time), so
   a run can be tied back to an institution.
2. Move the authz-version freshness check into `get_current_exam_user`
   itself (the dependency actually used everywhere) rather than leaving it
   as a separate opt-in dependency most routes never reach — reading
   `user.get("exam_authz_version")` and comparing it against the current
   membership's `authorization_version` for institution-backed users (a
   `EXAM_FACULTY_EMAILS`-only bootstrap user with no membership record is
   unaffected, same as today).
3. Add a regression test asserting every student/faculty exam route's
   dependency graph includes this freshness check (the exact inspection
   technique used to confirm this finding is a reasonable template for that
   test).
4. Separately confirm (not yet checked in this review) that the OTP
   challenge/verify flow itself also rejects a revoked identity key at
   sign-in time — `_verify_code` already checks
   `membership.status != MembershipStatus.ACTIVE` before issuing a new
   token, which is correct, but this does not help anyone with an
   already-issued token.

## 2. Role / institution / identity escalation — verified passing

Static evidence: `UserCreate` schema has no client-settable `role` field
(ADR 0007); `create_or_update_user` always takes the higher of
stored-vs-policy role, never a client-chosen privileged role; new
institution/exam authority never lives on the client-editable `users/{uid}`
document at all (Firestore rules confirm `role` updates are rejected unless
byte-identical to the stored value). 5 dedicated regression tests
([backend/tests/test_role_escalation.py](../../backend/tests/test_role_escalation.py)):
viewer cannot self-grant `placement_cell`, arbitrary authority fields are
ignored, a legitimate privileged user keeps their role on refresh, a
tampered stored doc doesn't escalate a viewer. Direct `git diff main HEAD`
confirms the ONLY legacy files touched across all 11+ phases are exactly
the ones this fix requires (`dependencies.py`, `users.py`, `schemas.py`,
`firestore.rules`) — nothing broader.

## 3. No locked content in student response/cache — verified passing

`build_public_content`
([backend/app/domain/generation/public_content.py](../../backend/app/domain/generation/public_content.py))
is the sole allowlist deciding what of a `GenerationCandidate` a student
ever sees; it is a pure, explicit field allowlist (never a denylist).
[backend/tests/test_generation_public_content.py](../../backend/tests/test_generation_public_content.py)
uses a unique marker string planted in every private field
(`private_expected_answer`, `private_explanation`, `marking_rubric`,
`reference_solutions` source, `hidden_test_groups`) and asserts the marker
is absent from the serialized output for every item type — a stronger test
than checking key names alone, since it would also catch an accidental
leak through a differently-named field. The exam PWA service worker (§10)
never caches `/api/` responses at all, and every student route sets
`Cache-Control: no-store` explicitly (`_set_no_store` helper, used on every
response in `exams_student.py`).

## 4. Final submission immutability under races — verified passing

9 dedicated decision tests in
[backend/tests/test_exams_decisions.py](../../backend/tests/test_exams_decisions.py)
directly target this: identical retry with the same idempotency key returns
the same receipt; a different key after finalization is an explicit
conflict; a stale epoch, a stale draft version, a past deadline, a blocked
attempt, a paused run, and an ended run are all independently rejected —
covering every race named in Prompt 4 (double-click, stale epoch, deadline,
concurrent close/block vs. submit). The transaction itself takes no external
service calls (confirmed in `student_service.py`'s submit/finish
implementation), consistent with "no external service calls inside
transaction callbacks."

## 5. Client time/refresh/disconnect cannot extend deadlines or reset warnings — verified passing

Every deadline is computed once, server-side, from a `Clock` abstraction
(`compute_deadline`, `backend/app/domain/exams/decisions.py`) — tests use a
`FixedClock`/`SystemClock` split specifically so client-supplied time is
structurally impossible to use (there is no code path that reads a
client-supplied timestamp into deadline math anywhere in the exam domain).
Warning counts live in the server-side incident ledger
(`backend/app/domain/proctoring/`), never a client-reported counter — a
page refresh re-reads the same ledger, it does not reset it.

## 6. Telemetry replay/correlation cannot manufacture warnings — verified passing

20 dedicated tests in
[backend/tests/test_proctoring_decisions.py](../../backend/tests/test_proctoring_decisions.py)
specifically target replay/correlation: duplicate event IDs are deduped, a
burst of identical events is processed once, flapping within the cooldown
window counts as one continuous departure (not several), an
already-counted incident does not double-count on reopen/reclose, and a
stray "end" event with no open incident is ignored outright.

## 7. Camera/network failures are not auto-misconduct — verified passing

`test_blur_alone_is_informational_never_counted` and
`test_permission_denied_and_camera_error_are_informational`
(same file as §6) directly assert these are classified as
technical/informational, never contributing to the warning count — matching
contract #5's "Connection/camera faults... are not automatic misconduct."

## 8. Uploaded KT cannot control tools or auto-approve generated questions — verified passing

`validation.py`'s `validate_candidate_dict` explicitly never reads a
provider-supplied `status` field — every candidate is forced to
`CandidateStatus.DRAFT` on construction regardless of what the (possibly
compromised, possibly prompt-injected) provider claims, with status
afterward recomputed purely from structural checks the backend itself
performs (source anchors must exist in the exact supplied bundle,
objective/difficulty/marks must match the request). This session's own
work on "Backend model prompt A/B" added a further enforced layer: the
top-level provider response envelope schema restricts `status` to
`Literal["draft", "insufficient_source"]` — a provider asserting
`"approved"` fails schema validation immediately, before any candidate-level
logic even runs. Independent review (prompt B) can only ever pull a
candidate backward to `NEEDS_REVISION`, never forward — enforced both by a
schema-level `Literal` restriction and by `apply_independent_review`'s
explicit "never upgrades" logic. No prompt-injection text in an uploaded
document can reach a tool call or an approval decision — approval is a
separate faculty-only code path (`review.py`) with no model input at all.

## 9. Code execution isolation / no hidden-input leak — partially verified, real isolation is an untested risk

**Verified by construction:** a workspace-wide search for
`subprocess|os\.system|eval\(|exec\(|Popen` across `backend/app/` returns
exactly one match — a docstring in `kt/parsers.py` explaining it does NOT
use a subprocess. **No student code has ever executed anywhere in this
codebase.** `Judge0HttpAdapter.submit()`
([backend/app/domain/judge/provider.py](../../backend/app/domain/judge/provider.py))
unconditionally raises `NotImplementedError` — there is no code path, real
or accidental, that could run submitted code on the application host.
Hidden-input non-leakage is directly tested:
`test_public_test_result_dto_includes_stdout_but_no_expected_output_field`
and `test_sealed_grade_summary_dto_never_leaks_hidden_stdout_stderr`
([backend/tests/test_judge_grading.py](../../backend/tests/test_judge_grading.py)).

**Untested risk (carried forward from ADR 0011 and the Prompt 11
qualification report, not new to this review):** the actual hardened
isolation properties a real Judge0-compatible deployment would need
(blocked egress/metadata routes, no secrets/host mounts, resource/process
limits) have never been verified against a real instance — none exists in
this sandbox. This is an infrastructure-provisioning gap, not a code gap;
it cannot be closed by any amount of local testing.

## 10. Grading retries/regrades preserve exact lineage — verified passing

`create_regrade`
([backend/app/domain/judge/grading.py](../../backend/app/domain/judge/grading.py))
increments `version` and constructs a NEW result object — it never mutates
the previous one. `test_create_regrade_increments_version_and_does_not_mutate_previous`
([backend/tests/test_judge_grading.py](../../backend/tests/test_judge_grading.py))
asserts exactly this. Every grading result is bound to its originating
submission (hash, variant, runtime image, test-suite version per contract
#8) at creation time and never recomputed from a different submission on
regrade.

## 11. Evidence/grade exports require scope and expire appropriately — verified passing

Both evidence upload/view tickets
([backend/app/domain/proctoring/evidence_service.py](../../backend/app/domain/proctoring/evidence_service.py))
and export download tickets
([backend/app/domain/gradebook/export_service.py](../../backend/app/domain/gradebook/export_service.py))
follow the identical pattern: a ticket is minted with an explicit
`expires_at`, is single-use (`consumed`), and is checked against the
specific caller it was issued to (`ticket.uid`/`ticket.issued_to`) — all
three conditions (expiry, consumption, caller identity) are checked
together before any access is granted, and a `*TicketInvalidError` fires if
any one fails.

## 12. PWA updates don't interrupt exams or cache sensitive content — verified passing

Direct reading of
[frontend/public/exams-sw.js](../../frontend/public/exams-sw.js) confirms:
the fetch handler only ever intercepts (a) the exact precached static-shell
URLs and (b) exam-page navigations (serving the real network response, only
falling back to the offline page on a genuine network failure, never on an
HTTP error status) — every `/api/` call and everything else is explicitly
never intercepted, so no dynamic/sensitive content can ever be cached by
this worker. Update suppression during an active attempt uses
`hadControllerAtMount` tracking in
[useExamServiceWorker.ts](../../frontend/src/lib/useExamServiceWorker.ts),
fixing a real bug (reload on the service worker's first-ever activation)
originally caught only by a real-browser Playwright test — both the unit
test and the e2e test (`exams-pwa.spec.ts`) remain in the suite.

## 13. Legacy auth/search/practice/anonymity compatibility — verified passing

`git diff main HEAD --stat` against every legacy route/service file
(`search.py`, `experiences.py`, `practice.py`, `dashboard.py`,
`services/**`, and the legacy frontend search/practice/submit pages)
returns **zero changes** across all 11+ phases of exam development. The
only legacy files touched anywhere are the ones Prompt 1's documented
security fix requires (`dependencies.py`, `users.py`, `schemas.py`,
`firestore.rules`) plus Prompt 10's path-scoped `Permissions-Policy` header
addition (`next.config.ts`) — nothing else. `backend/app/main.py` (the
legacy entrypoint's startup/lifespan tasks) has no diff at all, confirming
the exam API's separate entry point (`app.exam_main:app`) never executes
legacy seeding/search/NLP startup work, per contract #3's requirement.

## Release recommendation

Combined with the Prompt 11 production-qualification report's conclusions
(real staging infrastructure, real chaos testing, real cost data, and
manual accessibility/SEB pilots all remain outstanding), this review adds
one more concrete, code-level gate that must close before any real
institutional release:

**New blocking gate:** wire a live, per-request institutional
membership/authorization-version freshness check into the student (and,
where applicable, faculty) exam request path — §1 above. This is a code
change, not an infrastructure gap, and should be treated as a release
blocker on its own: it is the only one of the 13 reviewed invariants found
to be unenforced in the running system today.

**Confirmed-solid invariants:** the other 11 reviewed invariants are backed
by genuine code-level enforcement plus either targeted unit-test evidence
or direct structural/diff evidence gathered in this review — these do not
need further code changes to be considered sound at the code level (real
staging/chaos verification, per Prompt 11, is still separately required
before relying on them at production scale).

**Still an infrastructure-only gap, not a code gap:** real Judge0 sandbox
isolation (§9) and the real SEB client pilot (Prompt 10's own checklist)
remain unverifiable without real external infrastructure this sandbox does
not have.

**Overall:** the module is closer to release-ready at the code level than
the broad infrastructure gates in the Prompt 11 report suggest, but is
**not yet releasable** — both because of those infrastructure gates and
because of the new, concrete §1 finding, which is itself release-blocking
independent of any infrastructure availability.
