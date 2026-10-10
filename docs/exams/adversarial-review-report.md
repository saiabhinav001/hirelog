# Exams — Final Adversarial Review (v1.1)

Replaces the 2026-10-02 review, which predates Prompts 1–11 of the v1.1 pack. Two of its conclusions were wrong
(§1 "revoked membership has no effect" was true then and is fixed now; §12 "PWA updates verified" was false, see
[production-qualification-report.md](production-qualification-report.md) §5), so none of its rows are carried over
unchecked.

Date: 2026-10-11. Branch `exams/phase-11-production-qualification`, reviewed at `cab75a9` plus
[backend/tests/test_adversarial_integrated.py](../../backend/tests/test_adversarial_integrated.py).
**Local, in-memory evidence only.** Nothing here was run against a real Firestore, identity provider, judge, bucket
or browser fleet; see §4.

## 1. Method

- Route inventory of the exam app: 127 routes; five need no token (`/`, two health routes, OTP `challenge` and
  `verify`); 124 are behind `require_exams_enabled`.
- **Integrated probes** (16 tests, new). Existing HTTP tests replace `get_current_exam_user` and
  `require_student_run_scope` with a stub, so they cannot show the real chain works. These override only token
  verification and run the real dependency chain against the in-memory Firestore, sweeping every registered route.
  A positive control shows a current member and an allowlisted super-admin are admitted, so the 403s mean something.
- Full suites at review time: backend 1585 passed, 0 failed; frontend 237 passed (35 files), `tsc` clean, `eslint`
  0 errors and 1 pre-existing warning.
- Targeted reading of the live-event, evidence-upload and feedback paths, and a check for committed secrets,
  dangerous calls and sensitive logging.

## 2. Confirmed failures

| # | Severity | Finding | Status |
|---|---|---|---|
| F1 | Medium | **Learning feedback read the live gradebook.** `FeedbackService` checked that a release existed, then rebuilt the student's row with `build_student_row`. A regrade or defective-question fix made after a release reached feedback before it was released, while `/results` still showed the earlier release. Location: `backend/app/domain/gradebook/feedback_service.py`. Reproduce: release, regrade one student, call `/feedback` before re-releasing | **Fixed** in `cab75a9`: feedback uses the same frozen row as `/results`. Test `test_learning_feedback_follows_the_released_row_not_the_live_regrade` fails on the old code and passes now |

No other failure was confirmed.

## 3. Verified controls (evidence in this run)

| Invariant | Evidence |
|---|---|
| Roles, institution and course boundaries cannot be self-granted or crossed | Probes: a signed-in user with no authority, a student member, an unverified allowlisted e-mail, a teacher of another institution (on every run-scoped staff route) and a teacher with a revoked membership each get 403 on **every** staff route. Positive control: a verified super-admin gets 200 on the ops route, a verified teacher 403 |
| A forged college ID or e-mail cannot impersonate a roster identity | Probe: a verified token e-mail equal to a roster contact gets 403 on every student route; membership is only set by OTP or link provisioning |
| A revoked or stale session cannot read questions or change answers | Probes: with a valid token, a revoked membership, a membership bumped past the token's version, and a member of another institution each get 403 on every student route, and the attempt document is unchanged. The own-run list returns no scoped run to anyone without a current membership. This confirms the 2026-10-02 failure is closed |
| Unauthenticated callers get nothing | Probe: every non-public route answers 401 or 403 with no credentials |
| Authenticated student responses are not cacheable | Probe: every student GET answered to a member carries `Cache-Control: no-store` |
| Locked content, answer key, private rubric, hidden tests absent from API responses | Existing tests in the full suite: `test_no_student_response_carries_private_content_and_all_are_no_store`, `test_hidden_tests_and_outputs_never_appear_in_what_a_student_can_receive`, `test_short_answer_never_leaks_rubric_or_expected_answer`, `test_provider_notes_and_hidden_tests_never_reach_the_student_content` |
| Live events carry no content | Read: events hold run status (broadcast to the run's connections) and one student's attempt status, version, warning count and slot index (delivered only to the faculty room and that student); no question, answer or evidence |
| Submission immutability, deadlines, clock | Existing tests in the full suite (p6, p7): concurrent submit, sweeper and terminate; lost response; stale epoch; late retry. The 300-student burst with a mid-burst restart is in the release pack |
| Exports and evidence expire and are scoped | Existing tests: export tickets, expiry sweep, view-ticket expiry |
| Generated items are source-grounded and teacher-approved | Existing tests plus Prompts A and B: anchors must exist in the supplied bundle, a provider cannot set status, a review can only pull a candidate back |
| Legacy workflows intact | No diff against `main` for the legacy entrypoint, search, experiences, practice and services |
| No secrets or dangerous calls | Only `backend/.env.example` is tracked; no `shell=True`, `eval`, `exec`, `os.system` or `pickle.loads` in `backend/app`; the only `subprocess` use is the bounded ingestion parser; a grep of logger calls for answer, key, rubric, token, secret and OTP terms found only two generic OTP messages that print no code |

## 4. Untested risks (not failures)

| # | Risk | Notes |
|---|---|---|
| U1 | **No real sandbox evidence.** Student code isolation, egress and metadata denial are unproven | Release-pack gate G4 |
| U2 | **No real identity provider, OTP delivery, bucket/IAM or Firestore rules emulator run** | G5, G8, G14 |
| U3 | **Single-process state.** The live-event registry, rate limiters and the OTP fallback secret are per process; with more than one instance, events and limits are not shared | G1, G6 |
| U4 | **Load, backup restore, outage rehearsal and alerting are local only** | G1, G6, G7, G12 |
| U5 | **Real browsers, screen readers, zoom and devices** were not exercised; the axe and keyboard tests are automated only | G11 |
| U6 | **Prompt text filters are best effort.** Model feedback is capped and filtered by a pattern list, and generated items are checked for forbidden topics by substring. Neither is proof a model said nothing improper; faculty review remains the control | Prompts A and C |

## 5. Low-severity observations (no fix made)

| # | Observation | Impact |
|---|---|---|
| L1 | `POST /student/proctoring/snapshots/upload/{ticket_id}` is authorized by the single-use, short-lived ticket and the caller's uid, not by the run scope. A student whose membership is revoked after a ticket was minted, and whose session has no institution claim, can still upload that one snapshot until it expires | Private evidence attached to their own attempt; one ticket. Minimal fix: also call the run scope on upload |
| L2 | An already-open live WebSocket is not closed when a membership is revoked; new tickets are refused | Only run and own-attempt status events are delivered. Minimal fix: a maximum socket age |
| L3 | `require_active_institution_membership` is wired into zero routes (its docstring says the token-claim check in `get_current_exam_user` replaced it) | Dead code, no gap |
| L4 | `npm audit --omit=dev`: 6 findings (4 high via `firebase` → `@firebase/firestore`, `@grpc/grpc-js`; 2 moderate). The only offered fix is a major downgrade | G10; a CI job that fails on high would fail |
| L5 | `ALLOWED_ORIGINS` defaults to three localhost origins | Must be set explicitly in any deployment; checklist item in the release pack |

## 6. Release recommendation

**Do not release.** Unchanged from the release pack: no real-environment evidence exists for the sandbox,
identity provider, storage boundaries, load target, restore, rules or devices.

Remaining gates, from the release pack §14: G1–G9, G12 and G14 need a staging environment and the institution's
decisions; G10 needs the dependency decision (L4); G11 needs people and devices; G13 needs billing data; G16 is a
small code change. **G15 is now partly met**: the authorization chain has been falsified at the HTTP level in
memory (§3), and one real defect (F1) was found and fixed. It still needs repeating at the real-staging standard
after G1–G9.
