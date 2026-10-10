# Hirelog Exams — requirements traceability (Prompt 0, v1.1)

Audited 2026-10-09 at `5b0ad3b` (pre-Prompt-1 baseline). Companion documents: [baseline.md](baseline.md),
[integration-map.md](integration-map.md), [compatibility-and-migration.md](compatibility-and-migration.md), [delta-plan.md](delta-plan.md).

**Requirement source.** `01-RESEARCH-REPORT.md` / `02-MASTER-TECHNICAL-DESIGN.md` were not available, so "every user requirement" is derived
from the v1.1 prompt pack: the master prompt (roles, 15 hard invariants), Prompts 1-11, Backend prompts A/B/C and the Prompt 0 inspection list.
IDs are stable handles used by the delta plan and by tests.

**Status vocabulary** (master prompt): `EXISTS_AND_VERIFIED` (implemented **and** exercised by a named test or verified by direct code reading where
stated) · `EXISTS_BUT_DEFECTIVE` (implemented, concrete defect) · `PARTIALLY_EXISTS` · `MISSING` · `BLOCKED_BY_DECISION` (needs a product /
institution decision, see §14). `UNVERIFIED` marks anything that could not be confirmed from code or tests. A UI control alone is never counted as
evidence. Test names are `backend/tests/*` unless prefixed `fe:` (Vitest) or `e2e:` (Playwright).

**Evidence honesty.** Backend tests are pure-unit/fake-based; none runs a real or emulated Firestore transaction. No real generation, judge,
malware-scan, OTP-delivery or storage provider is configured. Nothing here claims production load, sandbox security or strict lockdown.

---

## 1. Authorization and roles — Prompt 1

> The **Status**, **Current behavior** and **Evidence** columns below record the audit baseline (before Prompt 1). The outcome after the
> Prompt 1 implementation is in [§1.1](#11-status-after-the-prompt-1-implementation).
| ID | Requirement | Current file / route / schema | Current behavior | Evidence / tests | Status | Smallest safe delta | Regression tests | Phase |
|---|---|---|---|---|---|---|---|---|
| AUTH-01 | Server-owned role policy for `student`, `teacher`, `super_admin` | `RoleScope` (student/faculty/proctor/admin) `domain/institution/models.py:21`; `EXAM_FACULTY_EMAILS` `core/config.py:91`; `api/exams_dependencies.py::require_exam_faculty` | Teacher = bootstrap email **or** ACTIVE FACULTY/ADMIN membership; bootstrap email is also ADMIN on institution routes; student = enrollment only; no `super_admin`, no central policy | `test_institution_reconciliation.py` (3 faculty-gate tests) | PARTIALLY_EXISTS | Pure policy module `domain/authz/policy.py` mapping onto existing `RoleScope` + new `EXAM_SUPER_ADMIN_EMAILS`; **no second role store**; `/me` adds server-computed `exam_roles` (additive) | policy matrix; `/me` shape | P1 |
| AUTH-02 | Bootstrap super-admins only via verified authentication + server allowlist; bind to stable UID; log | `EXAM_FACULTY_EMAILS` string match on the token `email` claim in `exams_dependencies.py:49-52`, `exams_me.py:20`, `exams_live.py:51`, `exams_institution_admin.py:35` | Recomputed from the raw email each request; no `email_verified`; no UID binding; no audit | none | MISSING (final address list `BLOCKED_BY_DECISION` D-1) | Env allowlist `EXAM_SUPER_ADMIN_EMAILS` (no addresses in source); require `email_verified`; first verified sign-in writes an immutable email↔uid binding + audit event; other uid for a bound email = denied + audited | binding service tests | P1 |
| AUTH-03 | Public profile create/update cannot grant privileged roles, institution scope or arbitrary authority fields | `UserCreate` (no `role`) `models/schemas.py`; `api/routes/users.py::create_or_update_user`; `firestore.rules` `users/{uid}` (create `role=='viewer'` + key allowlist; update `affectedKeys ⊆ {name,display_name,name_last_updated_at}`, `role` unchanged); exam authority never stored on `users/{uid}` (ADR 0007, 0008 §3) | Verified by reading; client cannot set role/authority | `test_role_escalation.py` (5) | EXISTS_AND_VERIFIED (rules not emulator-tested) | None to behavior. Add forged-email/uid body tests + static `firestore.rules` regression test; emulator suite deferred to P11 | `test_exam_authz_profile_and_rules.py` | P1 (+P11) |
| AUTH-04 | Verify identity from the token, bind to UID; never trust client-supplied email or stale claims alone | `api/dependencies.py::get_current_user_uncached` (email from verified token ✔; `email_verified` ignored; `verify_id_token(token)` without `check_revoked`, line 83) | An unverified email/password account can present an allowlisted address | `test_auth_dependency.py` (7; none cover verification) | EXISTS_BUT_DEFECTIVE (High) | Surface `email_verified`/`sign_in_provider`; allowlist authority requires it (dev-only opt-out hard-disabled when `ENV=production`); `check_revoked` stays a documented residual risk (extra network hop per request) | dependency tests | P1 |
| AUTH-05 | Every exam endpoint validates identity, active membership, institution/course scope, resource ownership, current role version and exam state | Identity ✔ on all 88 routes (`get_current_exam_user`); exam state ✔ (services/decisions); membership, scope, ownership ✘ on faculty routes (`require_exam_faculty` only) and student routes (enrollment only) | See baseline §5 #1-#2 | adversarial review §1 (route-graph: 0 of 91 use membership); `test_exams_routes_smoke.py` (flag only) | PARTIALLY_EXISTS | New `api/exams_scope.py` dependencies per resource family + student run scope; route-graph guard test | `test_exam_authz_scope.py`, `test_exam_authz_route_guards.py` | P1 |
| AUTH-06 | Revoked role / membership with a stale token cannot read questions or change answers | `InstitutionAuthService.check_active_membership`, `revoke()` (bumps `authorization_version`), `require_active_institution_membership` `exams_dependencies.py:61` | **Wired into zero routes**; revocation only takes effect at Firebase token expiry (open since adversarial review §1) | function-level only: `test_institution_reconciliation.py`, `test_institution_auth_service.py::test_check_active_membership_*` | EXISTS_BUT_DEFECTIVE (High, open) | Enforce in `get_current_exam_user` whenever the token carries `exam_institution_id`; student routes also check the run's `institution_id` membership | `test_exam_authz_dependencies.py` | P1 |
| AUTH-07 | Forged college ID / email cannot impersonate a roster identity | `auth_service.py` OTP flow: generic ack, `contact_email` must equal roster email, HMAC-SHA256 + salt, TTL, attempt cap, single-use, sliding-window limiters; membership `uid` only set by OTP provisioning or verified `link` | A token whose email equals a roster email gets no membership (resolved by uid) — true by construction, not asserted | `test_institution_auth_service.py` (16), `test_institution_otp.py` (9) | EXISTS_AND_VERIFIED | Add explicit same-email-no-membership and wrong-institution regression tests | in `test_exam_authz_dependencies.py` | P1 (tests) |
| AUTH-08 | Direct profile / database mutation blocked | Default-deny catch-all in `firestore.rules`; every exam collection Admin-SDK-only | Reviewed statically; no `firebase.json`, no emulator, no rules tests | qualification report §4 (static) | EXISTS_AND_VERIFIED (static) / UNVERIFIED (runtime) | Static rules regression test in P1; `@firebase/rules-unit-testing` emulator suite in P11 | static test | P1 / P11 |
| AUTH-09 | Cross-course / cross-institution / cross-owner access denied | No course entity; `ExamDefinition`/`ExamRun` have no `institution_id`; any faculty can act on any definition, run, blueprint, candidate (answer keys), gradebook, appeal, export, incident, **camera-evidence view ticket**, live ticket | Confirmed by reading all 12 route modules | none | MISSING (course part `BLOCKED_BY_DECISION` D-2) | Additive optional `institution_id`; rule = owner, or super-admin, or ADMIN of the resource's institution; unscoped pilot resources = owner/super-admin only | scope matrix | P1 (+P2) |
| AUTH-10 | Preserve legacy login / profile / workflows | Legacy routers untouched by exam work (documented ADR 0007 fix only); legacy `get_current_user` cache unchanged | `/me` for a legacy user returns empty memberships | `test_auth_dependency.py` (7), `test_role_escalation.py` (5), adversarial review §13 | EXISTS_AND_VERIFIED | Keep; add `/me` legacy-user regression | in `test_exam_authz_dependencies.py` | P1 (tests) |
| AUTH-11 | Feature flag default off; router-level `require_exams_enabled` on every exam router | `EXAMS_ENABLED=false`; 12/12 routers declare it | 503 even for unauthenticated callers | `test_exams_routes_smoke.py` (11; env-sensitive, OPS-01) | EXISTS_AND_VERIFIED | Route-graph test also asserts the flag dependency | route-guard test | P1 |
| AUTH-12 | UI guards are UX only | `lib/examAuth.tsx`, `components/exams/ExamProtectedRoute.tsx` read `/api/exams/v1/me` | Server must enforce; see AUTH-05/09 for places it does not yet | none isolated | EXISTS_AND_VERIFIED (client side) | No frontend change in P1 (`/me` change is additive) | — | P1 |
| AUTH-13 | Recent re-auth for sensitive staff actions (not labelled MFA) | `require_recent_staff_reauth` (auth_time recency) on `revoke_membership` only | Works; not applied elsewhere | `test_institution_reconciliation.py` (3) | PARTIALLY_EXISTS | Out of P1 (extending it is a behavior change for operators) — note for P2 | — | P2 |
| AUTH-14 | Legacy `PLACEMENT_CELL_EMAILS` promotion should also require verified identity | `api/dependencies.py::_resolve_role` promotes **and persists** `placement_cell` from the raw token email; legacy signup (`AuthContext.tsx`) uses `createUserWithEmailAndPassword` and never sends a verification email | Same defect class as AUTH-04 but in a **legacy production workflow** | `test_auth_dependency.py::test_resolve_role_promotes_placement_email` | EXISTS_BUT_DEFECTIVE / `BLOCKED_BY_DECISION` D-11 | **Not changed** (preserve legacy workflows; enforcing would lock out unverified password users). Documented in ADR 0017 with a recommendation | — | — |

### 1.1 Status after the Prompt 1 implementation

Implemented as [ADR 0017](../adr/0017-exam-role-policy-and-resource-scope.md); migration notes in
[compatibility-and-migration.md §6](compatibility-and-migration.md). Evidence is unit-level with in-memory doubles (no Firestore emulator, no browser
session). Backend suite: 627 tests passing with `EXAMS_ENABLED=false` (490 before + 137 new); `ruff check .` clean.

| ID | Status after P1 | What changed | Evidence |
|---|---|---|---|
| AUTH-01 | EXISTS_AND_VERIFIED | Pure policy `domain/authz/policy.py` maps `RoleScope` + verified allowlists onto `student` / `teacher` / `super_admin`; `/me` adds `exam_roles`, `is_super_admin`, `email_verified` (additive) | `test_exam_authz_policy.py` (34), `test_exam_authz_dependencies.py::test_me_*` |
| AUTH-02 | EXISTS_AND_VERIFIED (logic) / UNVERIFIED (concurrent writers) / final list `BLOCKED_BY_DECISION` D-1 | `EXAM_SUPER_ADMIN_EMAILS` (no address in source); verified e-mail only; first UID bound atomically via `create()`; conflicts denied and audited; store failure fails closed (503) | `test_exam_staff_binding.py` (8), `test_exam_authz_dependencies.py` |
| AUTH-03 | EXISTS_AND_VERIFIED (static rules) | No behavior change; profile payload pinned to `{name}`; forged role / e-mail / college id in a profile write creates no exam authority; rules checked statically | `test_exam_authz_profile_and_rules.py` (5), `test_role_escalation.py` |
| AUTH-04 | EXISTS_AND_VERIFIED; `check_revoked` stays a documented residual | Authority reads the verified **token** e-mail (`token_email`, `email_verified`, `sign_in_provider`); the client-writable stored `users/{uid}.email` is never used (new finding, closed) | `test_exam_authz_dependencies.py::test_forged_stored_profile_email_*`, `::test_uncached_user_*` |
| AUTH-05 | EXISTS_AND_VERIFIED for HTTP routes; WebSocket stream and question↔slot (P6) remain | Router-level flag, authenticated user, role gate and a resource-scope dependency on every scoped path id; body-id routes call the same helpers inline; route-table tripwires | `test_exam_authz_route_guards.py` (12), `test_exam_authz_scope.py` (42) |
| AUTH-06 | EXISTS_AND_VERIFIED | `get_current_exam_user` enforces membership status + `exam_authz_version` on every exam route; student routes also require an ACTIVE student membership in the run's institution | `test_exam_authz_dependencies.py::test_revoked_*`, `::test_token_older_*`, `::test_scoped_run_rejects_*` |
| AUTH-07 | EXISTS_AND_VERIFIED | Added explicit tests that a token e-mail equal to a roster contact e-mail yields no membership or run scope | `test_exam_authz_dependencies.py::test_matching_roster_email_*` |
| AUTH-08 | EXISTS_AND_VERIFIED (static) / UNVERIFIED (runtime) | Static checks: default-deny catch-all is last; no rule block for any exam/institution/binding/audit collection; `users` create/update pins | `test_exam_authz_profile_and_rules.py`; emulator suite remains P11 |
| AUTH-09 | EXISTS_AND_VERIFIED (owner / institution); course part `BLOCKED_BY_DECISION` D-2 | Owner, ADMIN of the resource's institution, or super-admin; additive optional `institution_id` / `created_by`; creation-time scope and enrollment validation | `test_exam_authz_scope.py` |
| AUTH-10 | EXISTS_AND_VERIFIED | Legacy sessions (no exam claims) trigger no membership lookup; legacy `/me` is empty and error-free; legacy `users.role` can neither grant nor block exam authority | `test_exam_authz_dependencies.py::test_legacy_*`, `::test_me_for_a_legacy_user_*` |
| AUTH-11 | EXISTS_AND_VERIFIED | Guard now asserts the flag is declared at **router level** on every exam route | `test_exam_authz_route_guards.py` |
| AUTH-12 / AUTH-13 / AUTH-14 | unchanged | No frontend change; re-auth not extended; legacy placement-cell promotion untouched (D-11) | — |
| ATT-02 | PARTIALLY_EXISTS | Membership/scope part closed (student scope per request); `question_id` ↔ released-slot validation remains P6 | scope + dependency tests |
| JDG-12 | EXISTS_AND_VERIFIED | Faculty judge routes scoped through submission / review / run; student public-run now checks enrollment + membership | route-guard + scope tests |
| LIV-02 | EXISTS_AND_VERIFIED (ticket issuance) | Live ticket role resolved through run scope; an already-open socket of a later-revoked student is a documented residual | `test_exam_authz_scope.py::test_live_*`, `test_exam_authz_route_guards.py::test_live_ticket_*` |
| GRD-07 | EXISTS_AND_VERIFIED | Gradebook, proctoring and lockdown routes scoped to the run / appeal / export / snapshot; ticket redemption stays bound to the issuing identity | route-guard + scope tests |

## 2. Identity, roster, account linking — Prompt 2

> The **Status** and **Smallest safe delta** columns below record the audit baseline. The outcome after the Prompt 2 implementation is in
> [§2.1](#21-status-after-the-prompt-2-implementation).

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| ID-01 | Institution / membership model | `domain/institution/*`; membership doc id `{institution_id}::{IDENTITY_KEY}` = uniqueness constraint; `authorization_version` | `test_institution_roster.py` (14), `test_institution_auth_service.py` (16) | EXISTS_AND_VERIFIED | — | — |
| ID-02 | College ID is an identifier, never a password | Session only after OTP proof (`verify_and_sign_in`) or verified link; no password store | `test_institution_auth_service.py` | EXISTS_AND_VERIFIED | — | — |
| ID-03 | CSV dry-run, bounded resumable import, leading zeros, per-institution normalization, duplicate/collision detection, row errors | `roster_import.py` (pure planner shared by dry-run/apply), `roster_service.py` (cursor `applied_cursor`); **JSON rows only — no CSV upload endpoint** (ADR 0010); normalization is global trim/collapse/UPPERCASE, not institution-specific | `test_institution_roster.py::test_leading_zeros_*`, `test_apply_batch_*`, `test_dry_run_*` | PARTIALLY_EXISTS | `multipart` CSV parser onto the same dry-run/apply endpoints; per-institution normalization hook only if an institution needs it (D-2) | P2 |
| ID-04 | Removing a roster row never deletes an account | No delete path anywhere; `revoke` is the explicit deactivation | ADR 0010 §2 (by construction) | EXISTS_AND_VERIFIED | — | — |
| ID-05 | OTP short-lived, single-use, rate-limited, HMAC-protected, delivered via a configured provider; never called MFA | `otp.py` (HMAC-SHA256+salt, TTL 300 s, 5 attempts), `SlidingWindowLimiter` 5/5 min challenge, 8/5 min verify; delivery = `DevSinkOtpDelivery` (logs the code); per-process fallback secret when `EXAM_OTP_HMAC_SECRET` unset | `test_institution_otp.py` (9), `test_institution_auth_service.py::test_verify_rejects_replayed_code/expired` | PARTIALLY_EXISTS (delivery `BLOCKED_BY_DECISION` D-6) | Real delivery adapter behind the existing `OtpDelivery` protocol once the institution chooses SSO / verified-email OTP | P2 |
| ID-06 | Account linking proves control of both identities, preserves UID, conflict recovery + audit | `link_existing_account` (session + OTP); conflict → 409 `IdentityAlreadyLinkedError` + warning log; recovery is manual | `test_institution_auth_service.py::test_link_*` (3) | PARTIALLY_EXISTS | Persisted audit event + operator recovery runbook/endpoint | P2 |
| ID-07 | Teacher assignment by an authorized super-admin; course-scoped capabilities | No assignment endpoint; faculty role only via roster rows (`role_scope=faculty`) uploaded by ADMIN/bootstrap; no course entity | none | MISSING (`BLOCKED_BY_DECISION` D-2) | After D-2: assignment API reusing `institution_memberships`; capability scoping per course | P2 |
| ID-08 | SSO adapter contract | `SsoIdentityProvider` / `UnconfiguredSsoProvider` stub | none | PARTIALLY_EXISTS (D-6) | Implement only when the institution picks a provider | P2 |
| ID-09 | Super-admin management surface | API-only: create institution, roster dry-run/apply, revoke; no UI; no super-admin concept | none | MISSING | After P1 role policy: super-admin-only institution management routes (already exist) + UI in P2/P7 | P2 |

### 2.1 Status after the Prompt 2 implementation

Implemented as [ADR 0018](../adr/0018-roster-linking-otp-and-teacher-assignment.md); migration notes in
[compatibility-and-migration.md §7](compatibility-and-migration.md). Evidence is unit- and HTTP-level with in-memory stand-ins (no Firestore emulator, no real
delivery provider, no browser session). Backend suite: 774 tests passing with `EXAMS_ENABLED=false` (627 before + 147 new); `ruff check .` clean.

| ID | Status after P2 | What changed | Evidence |
|---|---|---|---|
| ID-01 | EXISTS_AND_VERIFIED | Unchanged model; `Institution.identity_policy` added (optional) | `test_institution_roster_planner.py` |
| ID-02 | EXISTS_AND_VERIFIED | A CSV `password` column is rejected, not ignored; no password store added | `test_institution_roster_csv.py::test_a_password_column_*` |
| ID-03 | EXISTS_AND_VERIFIED (unit/HTTP) | CSV on the **same** dry-run/apply routes (multipart `file`, ≤ 1 MiB / 5000 rows, strict columns, row errors with line numbers); per-institution `identity_policy`; duplicates and formatting collisions judged across the whole file; apply bound to the dry-run by digest; replay is `skip_unchanged` | `test_institution_roster_csv.py` (13), `test_institution_roster_planner.py` (29), `test_institution_roster_service.py` (24), `test_institution_admin_routes.py` (26) |
| ID-04 | EXISTS_AND_VERIFIED | Added regression tests: rows absent from a later import are neither removed nor deactivated, and an import cannot reactivate a revoked member | `test_institution_roster_service.py::test_members_missing_*`, `::test_an_import_does_not_reactivate_*` |
| ID-05 | PARTIALLY_EXISTS — real provider `BLOCKED_BY_DECISION` D-6 | Delivery is an adapter contract selected by `EXAM_OTP_DELIVERY_PROVIDER`; dev sink outside production only; production fails closed with a uniform `503`; delivery failures look like success; a code is checked and consumed in one transaction; replay, expiry, attempt cap, newer-challenge invalidation and per-identity rate limits are tested. E-mail OTP is documented as not MFA | `test_institution_auth_service.py` (51), `test_institution_otp.py` |
| ID-06 | EXISTS_AND_VERIFIED (unit) | Link runs in a transaction; both conflicts (identity linked elsewhere, account already holds another identity) are refused and persisted to the audit log; link, provisioning, revoke, reinstate, unlink are audited with actor and reason; `unlink` / `reinstate` are the recovery path | `test_institution_auth_service.py::test_*link*`, `::test_unlink_*`, `::test_revoke_*` |
| ID-07 | EXISTS_AND_VERIFIED (unit) | **D-2 resolved: definition = course.** `POST …/teachers` (super-admin only) grants FACULTY/ADMIN; `ExamDefinition.instructor_uids` + `PUT …/definitions/{id}/instructors` (super-admin or ADMIN of the institution) assigns co-instructors who manage that definition's runs, blueprints and gradebook; assigned courses are listed | `test_exam_co_instructors.py` (17), `test_institution_roster_service.py::test_assign*` |
| ID-08 | PARTIALLY_EXISTS (D-6) | Unchanged: the SSO contract stays a stub until the institution chooses | — |
| ID-09 | PARTIALLY_EXISTS | API surface complete: list institutions, list members (masked e-mails), roster job status, teacher assignment, revoke / reinstate / unlink. No UI yet (Prompt 7) | `test_institution_admin_routes.py`, `test_exam_authz_route_guards.py` |
| AUTH-13 | EXISTS_AND_VERIFIED for the authority-changing routes | Recent sign-in (a recency check, not MFA) is now required to create an institution, apply a roster, revoke, reinstate, unlink, assign a teacher and change instructors | `test_exam_authz_route_guards.py::test_authority_changing_actions_require_recent_reauthentication` |

Defects found in the existing code while implementing (all fixed, none was in the audit baseline):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P2-1 | High | A roster apply overwrote an existing member's contact e-mail and role regardless of the uploader's standing; the e-mail is the OTP destination, so a FACULTY uploader could take over a student's (or a linked legacy) account or demote an ADMIN | Stored-state rules in the planner, re-checked inside a transaction; e-mail changes need ADMIN-level authority; version bump + audit |
| P2-2 | Medium | A roster apply silently reactivated revoked memberships | Revoked members are rejected; `reinstate` is the explicit path |
| P2-3 | Medium | The whole-document write could erase a concurrent login (`uid` re-provisioned, attempts orphaned) | Atomic create for students; transactional field updates that never rewrite `uid`/`status` |
| P2-4 | Medium | Apply was not bound to the dry-run and judged one batch at a time (a duplicate in a later batch overwrote the first row) | Digest binding (`409 roster_rows_changed`); whole-file structural pass |
| P2-5 | High (when enabled) | The dev OTP sink was the default in production and logged one-time codes | Adapter selection; production fails closed; codes never logged there |
| P2-6 | Low | A code could be consumed twice by concurrent requests | Check-and-consume in one transaction |
| P2-7 | Medium | Linking used a plain overwrite, persisted no audit, allowed two identities on one uid, and had no recovery | Transactional link, audited conflicts, one identity per uid per institution, `unlink` |

Roster authority change (a deliberate behavior change, ADR 0018 §5): only a super-admin grants FACULTY/ADMIN; institution ADMINs and FACULTY manage STUDENT rows.

## 3. Exam domain, policy, timing — Prompt 3

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| DOM-01 | ExamDefinition, immutable ExamRevision, ExamRun, Attempt, AttemptSlot, source references, audit events, durable jobs/outbox | `domain/exams/models.py` (definition, revision, run, attempt, draft, receipts, outbox); slot = derived id `{revision_id}:slot:{index}` (no entity); no general audit-event collection (only command receipts, proctoring log, evidence/export access logs) | `test_exams_state_machine.py` (6), `test_exams_decisions.py` (35), `test_exams_policy_and_outbox.py` (8) | PARTIALLY_EXISTS | Command audit with actor/reason in P3; authz audit in P1 | P3 |
| DOM-02 | Flags off by default; no legacy startup/seeding side effects | `exam_main.py` runs only a Firestore reachability check + loops when `EXAMS_ENABLED` | `test_exams_routes_smoke.py` | EXISTS_AND_VERIFIED | — | — |
| DOM-03 | Validate title, optional topics, question count, per-type counts, marks, negative marking, difficulty distribution, duration, schedule/timezone, randomization, variant policy, languages, warning thresholds, access policy | `ExamPolicy` (duration 1-1440, `warning_threshold` = proctoring warnings, late-admission grace, `allow_pause`, accommodations; `extra=forbid`); blueprint slot (item_type, objectives≥1, difficulty free string, marks>0, minutes≤180, languages, `requested_variant_count` 1-50). **Absent:** topics/objectives at exam level, counts totals, total marks, negative marking, difficulty distribution, schedule+timezone, randomization/variant policy, time-remaining thresholds, access policy | `test_exams_policy_and_outbox.py::test_policy_*` | PARTIALLY_EXISTS | Additive optional policy/blueprint fields whose defaults reproduce today's behavior (see delta-plan P3/P5) | P3 |
| DOM-04 | Preview total marks and resolved counts before publication | `publish_revision` takes a client-supplied `slot_count` unrelated to blueprint or approved candidates; no summary endpoint | none | MISSING | `GET` summary computed from blueprint + approved candidates; publish validates against it | P3 |
| DOM-05 | Source-derived topic suggestions with teacher confirmation | none | none | MISSING | After P4 grounding is real (needs extracted text) | P5 |
| DOM-06 | Legal state transitions + expected-version checks | `state_machine.py`, `decisions.py::decide_run_command` | `test_exams_state_machine.py`, `test_exams_decisions.py::test_command_rejects_stale_expected_version` | EXISTS_AND_VERIFIED | — | — |
| DOM-07 | Published edits create a new revision; active attempts stay pinned | `publish_revision` creates revision N+1; run pins `revision_id`; **revision snapshots only `title` + `slot_count`**, question content is a separate mutable/published collection | `test_exams_decisions.py` | PARTIALLY_EXISTS | Snapshot the approved-candidate id list into the revision at publish (additive) | P3 |
| DOM-08 | Finalized attempts cannot be reopened | `ATTEMPT_TRANSITIONS[FINALIZED] == ∅`; no reopen path | `test_exams_state_machine.py::test_finalized_attempt_has_no_legal_targets` | EXISTS_AND_VERIFIED | — | — |
| DOM-09 | Commands: grant/revoke, pause/resume, extend, terminate, recovery — scoped, reasoned, idempotent, receipted | Run-level `publish/release/pause/resume/close/end` (expected_version + idempotency key + receipts); per-student **extend** (no receipt); recovery for BLOCKED = `proctoring resolve`. **Missing:** per-student grant/revoke, per-student pause/resume/terminate, reason fields | `test_exams_decisions.py` (commands) | PARTIALLY_EXISTS | Per-attempt commands through the same transactional/receipt pattern | P3 |
| DOM-10 | Closing a cohort preserves accommodated / extended deadlines | `decide_finalization_batch`; deadline baked into attempt at admission | `test_exams_decisions.py::test_finalization_batch_preserves_accommodated_students_mixed_in` | EXISTS_AND_VERIFIED | — | — |
| DOM-11 | Audit: who / when / why for commands and extensions | `extend_time` route never passes `user["uid"]` to `grant_time_extension`; `CommandReceipt` has no actor | none | EXISTS_BUT_DEFECTIVE | Add `actor_uid` (+ optional reason) to receipts and extension records | P3 |
| DOM-12 | Durable jobs/outbox restart-safe and idempotent | Outbox claim/lease/attempt cap/handlers ✔ (`outbox.py`); **`ExamFinalizationSweeper` never started** | `test_exams_policy_and_outbox.py::test_drain_processes_unprocessed_entries_idempotently`; sweeper I/O shell untested (only pure `decide_finalization_batch`) | EXISTS_BUT_DEFECTIVE (High) | Start a finalization loop in `exam_main.lifespan` (existing loop pattern) | P3 |
| TMR-01 | Server controls deadlines; client clock cannot extend | `clock.py` `Clock`; `compute_deadline` server-side; extension is faculty-only | `test_exams_decisions.py` | EXISTS_AND_VERIFIED | — | — |
| TMR-02 | Server-side auto-finalization at deadline | see DOM-12: expired attempts are never auto-sealed/graded; `end` needs `force` while unresolved attempts exist | none for the I/O shell | EXISTS_BUT_DEFECTIVE (High) | as DOM-12 | P3 |
| TMR-03 | Timezone handling | UTC datetimes only; no per-exam timezone | none | MISSING | Store IANA tz on the run for display; keep UTC for enforcement | P3 |
| SCH-01 | Schedule window (start/end) and schedule/publish flow | Manual `release` only; no scheduled window | none | PARTIALLY_EXISTS | Optional `scheduled_start/end` on the run + sweeper-driven open/close | P3 |
| SHR-01 | Short share link (random, collision-checked, non-sequential, revocable, rate-limited; locator only) | none | none | MISSING (`BLOCKED_BY_DECISION` D-9) | New code lookup collection + route; authorization stays on enrollment | P7 |
| MRK-01 | Marks and negative marking | Per-slot `marks`; weighted aggregation; MCQ exact-match only; **no negative marking, no multi-select partial credit** | `test_judge_grading.py`, `test_gradebook_aggregation_service.py` | PARTIALLY_EXISTS (D-8) | Additive `negative_marks` per slot; scoring semantics doc | P3/P10 |
| RND-01 | Randomization, variants, option shuffle | Per-student variant assignment (`reserve_variants`, deterministic); **no option shuffle, no randomization policy** | `test_generation_review_and_variants.py` (10) | PARTIALLY_EXISTS | Seeded option-shuffle with stored mapping | P5 |

### 3.1 Status after the Prompt 3 implementation

Implemented as [ADR 0019](../adr/0019-exam-domain-commands-and-finalization.md); migration notes in
[compatibility-and-migration.md §8](compatibility-and-migration.md). Evidence is unit- and HTTP-level against an **in-memory Firestore with optimistic
transactions and thread jitter** (`backend/tests/exam_inmemory.py`) — real interleavings and retries, but no real Firestore emulator, no network
latency and no index behavior. Backend suite: 947 tests passing with `EXAMS_ENABLED=false` (774 before + 173 new); `ruff check` clean.

| ID | Status after P3 | What changed | Evidence |
|---|---|---|---|
| DOM-01 | PARTIALLY_EXISTS | Command, extension, terminate, access, recovery, publish, run-create and sweeper seals now write `exam_*` events (actor, subject, reason) to the shared `exam_authz_audit` collection **in the same transaction** as the change. `AttemptSlot` stays a derived id (`{revision_id}:slot:{index}`) — the revision now carries the frozen slot list | `test_exams_run_commands.py`, `test_exams_attempt_commands.py` |
| DOM-03 | PARTIALLY_EXISTS | Additive, validated: `timezone` (IANA, display only), `scheduled_start/end` (UTC-normalised, naive rejected, end > start), `variant_policy` (`unique`/`balanced_reuse`, honoured by reserve-variants), accommodation cap, blueprint `negative_marks` (≤ marks; rejected unless `EXAM_NEGATIVE_MARKING_ENABLED`). Counts, per-type counts, total marks and difficulty distribution are **computed** in the preview, not stored as editable policy. Still open: exam-level topics (P5), randomization/option shuffle (P5), time-remaining thresholds (P7) | `test_exams_policy_validation.py` (34) |
| DOM-04 | EXISTS_AND_VERIFIED (unit/HTTP) | `GET …/definitions/{id}/publish-preview` returns resolved counts, total marks, distributions, blockers and warnings; `publish` accepts `blueprint_id` and refuses on blockers (422 `publication_blocked`) | `test_exams_policy_validation.py`, `test_exams_run_commands.py`, `test_exams_faculty_routes_p3.py` |
| DOM-07 | EXISTS_AND_VERIFIED (unit) | A blueprint publish freezes slot specs and the approved-candidate ids (and source references) into the revision; revisions are written with create-only semantics; a run stays pinned to its `revision_id`; later candidate approvals/rejections do not change a published revision; publish is idempotent by key | `test_exams_run_commands.py::test_*revision*`, `::test_later_candidate_changes_*` |
| DOM-09 | PARTIALLY_EXISTS | Added, all scoped + idempotent + receipted + audited: `extend` (now transactional), `terminate` (reason required), `recover` (re-drives grading for a sealed attempt; never reopens), `access/grant`, `access/revoke` (refused once an attempt exists), `finalize-expired`. **Not added:** per-student pause/resume — it needs a deadline-freeze rule and collides with the proctoring `BLOCKED` state (new decision D-12); run-level pause/resume is unchanged | `test_exams_attempt_commands.py`, `test_exams_faculty_routes_p3.py` |
| DOM-10 | EXISTS_AND_VERIFIED | Regression-tested end to end: closing a cohort (manual or scheduled) stops admissions only; an accommodated or extended attempt keeps its own deadline and is sealed by the sweeper only after it | `test_exams_run_commands.py::test_close_needs_force_*`, `test_exams_attempt_commands.py::test_the_scheduler_closes_*` |
| DOM-11 | EXISTS_AND_VERIFIED (unit/HTTP) | `actor_uid` + optional `reason` on receipts; the extend route now passes the actor; extension audited with previous/new deadline | `test_exams_attempt_commands.py::test_extension_*`, `test_exams_faculty_routes_p3.py` |
| DOM-12 | EXISTS_AND_VERIFIED (unit) | The finalization loop is started in `exam_main.lifespan` (flag `EXAM_FINALIZATION_SWEEP_ENABLED`, default **on**); each seal is its own transaction; two sweepers racing seal each attempt once | `test_exams_attempt_commands.py::test_two_sweepers_*`, `::test_the_exam_api_starts_*` |
| TMR-02 | EXISTS_AND_VERIFIED (unit) | Expired attempts are sealed with a receipt over the last acknowledged drafts and an `attempt_finalized` outbox row (the grading trigger); blocked attempts are sealed too | `test_exams_attempt_commands.py::test_the_sweeper_*` |
| TMR-03 | EXISTS_AND_VERIFIED (unit) | IANA timezone stored on the policy for display; enforcement stays UTC | `test_exams_policy_validation.py` |
| SCH-01 | PARTIALLY_EXISTS | `scheduled_start/end` + sweeper-driven release/close behind `EXAM_SCHEDULE_AUTOMATION_ENABLED` (default **off**); never opens a window that already ended; duplicate schedulers apply once. No scheduling UI | `test_exams_attempt_commands.py::test_the_scheduler_*`, `::test_two_schedulers_*` |
| MRK-01 | PARTIALLY_EXISTS (D-8) | Per-slot `negative_marks` is validated, previewed and frozen into the revision, but **grading does not apply it**; it is rejected at blueprint creation and blocks publication unless `EXAM_NEGATIVE_MARKING_ENABLED` is on, so it can never be silently ignored. Scoring semantics remain decision D-8 | `test_exams_faculty_routes_p3.py::test_negative_marks_*` |
| RND-01 | PARTIALLY_EXISTS | Only the variant policy was added; option shuffle / randomization policy stays P5 | — |

Defects found in the existing code while implementing (all fixed, none was in the audit baseline except P3-1/P3-2, which refine DOM-12):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P3-1 | High | The finalization sweeper was never started | Loop in `exam_main.lifespan` |
| P3-2 | High | Even when run, a seal set no `submit_receipt` and emitted `attempt_expired`, which nothing consumes — an expired attempt would never be graded | Receipt + `attempt_finalized` outbox row + audit; `attempt_expired` kept for observers |
| P3-3 | Medium | `grant_time_extension` was a plain read-then-write: concurrent extensions lost updates, a late extension could change an attempt the sweeper had just sealed or resurrect an expired one, and a client retry added the minutes twice; no actor recorded | One transaction + `decide_time_extension` (rejects resolved / past-deadline / ended-run / over-cap), optional idempotency key, audit |
| P3-4 | Low | `close` counted attempts already past their own deadline as "unresolved", forcing `force` for a finished exam | Only attempts still inside their own deadline are counted |
| P3-5 | Low | `create_revision` used `set`, so a revision document could be overwritten | `create` (fails if it exists) |
| P3-6 | Medium | Publishing trusted a client-supplied `slot_count` unrelated to the blueprint or its approved candidates | Blueprint-derived path with blockers; legacy path unchanged |

## 4. Source ingestion — Prompt 4

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| ING-01 | Private source registry: multiple PDF/DOCX (+TXT) and approved URLs | `kt_uploads`, `kt_approved_sources`, review lifecycle with faculty-attested `RightsEvidence`; PDF/DOCX/TXT parsers | `test_kt_source_registry.py` (5), `test_kt_ingestion.py` (14) | PARTIALLY_EXISTS | URL sources have no extracted content (ING-05) | P4 |
| ING-02 | Private quarantine storage, generated object names | Bytes are **never persisted** (parsed in memory then discarded); `storage_ref="kt-uploads/{id}"` is a dangling label | none | MISSING (`BLOCKED_BY_DECISION` D-4) | Persist to a private bucket via the existing `evidence_storage` adapter pattern | P4 |
| ING-03 | Real signature, extension, MIME, size, count, decompression, parser-timeout validation | `file_validation.py` (magic bytes, MIME mismatch, 25 MB, zip entry/ratio/uncompressed caps, macro DOCX reject), `limits.py`, 20 s parser timeout; no per-teacher/definition file-count cap | `test_kt_ingestion.py` (14) | EXISTS_AND_VERIFIED (count cap absent) | Add a count cap | P4 |
| ING-04 | Malware scan integration; reject encrypted/unsupported with actionable errors | Default `NullMalwareScanner` is fail-closed → every upload ends `SCAN_UNAVAILABLE` and is **never parsed**; `EXAM_MALWARE_SCANNER_ENDPOINT` is read nowhere (warning only) | none | EXISTS_BUT_DEFECTIVE / `BLOCKED_BY_DECISION` D-4 | Scanner adapter (e.g. clamd) behind the existing protocol; honour the setting | P4 |
| ING-05 | Bounded-worker parsing; immutable extracted versions with content hashes and page/section anchors | Parsed **inside the API request** (`parse_bounded`, thread pool + timeout); `ExtractedVersion` immutable with per-paragraph anchors; only the **file** sha256 is recorded (no extracted-text hash); URL sources get no extracted version | `test_kt_ingestion.py::test_accept_plain_text` | EXISTS_BUT_DEFECTIVE | Move parsing to an outbox job; hash extracted text; extract URL text | P4 |
| ING-06 | OCR/table/image only via a qualified parser; surface low-confidence content | Text-only parsers; images/tables silently dropped | none | MISSING | Surface "unsupported content skipped" counts to the teacher first; OCR only after qualification | P4 |
| ING-07 | Upload progress, status, failure reason, retry, replace, delete; history | `UploadStatus` + `GET /sources[/{id}]`; **no retry/replace/delete routes, no upload-management UI** | none | PARTIALLY_EXISTS | Routes + UI preserving history | P4 |
| ING-08 | URL ingestion: SSRF/private/link-local/metadata blocked, redirects validated, size/time limits, canonical URL/hash/time | `url_resolver.py` (https only, IP-class denylist, metadata IPs, redirect re-validation ≤3, 25 MB, 10 s); documented TOCTOU/rebinding residual | `test_kt_url_resolver.py` (9) | EXISTS_AND_VERIFIED | Network-layer egress proxy remains an infra gate (P11) | P11 |
| ING-09 | Source content treated as untrusted data | Provider system prompt tells the model to ignore embedded instructions; source text is data in the request; nothing executes it | no injection-specific tests | PARTIALLY_EXISTS | Prompt-injection fixtures (see GEN-11) | P5 |

### 4.1 Status after the Prompt 4 implementation

Implemented as [ADR 0020](../adr/0020-durable-source-ingestion.md); migration notes in
[compatibility-and-migration.md §9](compatibility-and-migration.md). Evidence is unit-, HTTP- and component-level: the in-memory Firestore from Prompt 3, an
in-memory object store, a local stand-in for a clamd daemon, httpx mock transports and **real** child-process parsing of synthetic PDF/DOCX/HTML/TXT fixtures.
No real bucket, ClamAV, OCR engine, DNS or internet was used. Backend suite: 1117 tests passing with `EXAMS_ENABLED=false` (947 before + 170 new); `ruff check .`
clean; frontend `tsc --noEmit` clean, 17 new Vitest tests (the full frontend suite passes; `submit/page.test.tsx` timed out once under load and passes alone).

| ID | Status after P4 | What changed | Evidence |
|---|---|---|---|
| ING-01 | EXISTS_AND_VERIFIED (unit/HTTP) | Multiple PDF/DOCX/TXT uploads and URL sources (HTML/PDF/DOCX/TXT) all flow through one pipeline; URL sources now get extracted text; per-teacher active-upload cap; exact-duplicate refusal (same owner, same SHA-256) | `test_kt_pipeline.py::test_multiple_documents_*`, `::test_a_duplicate_*`, `::test_the_active_upload_cap_*`, `test_kt_routes.py` |
| ING-02 | EXISTS_AND_VERIFIED (in-memory store) / `BLOCKED_BY_DECISION` D-4 for the real bucket | Validated bytes are written to private storage under a generated `kt-quarantine/{random}` name (never the client filename) as opaque bytes, in the same step as the upload record + outbox job; a failed intake removes the object; production refuses uploads with `503 kt_storage_not_configured` while `EXAM_EVIDENCE_STORAGE_BUCKET` is unset | `test_kt_pipeline.py::test_accept_*`, `::test_a_failed_intake_*`, `test_kt_routes.py::test_production_without_*` |
| ING-03 | EXISTS_AND_VERIFIED | Added what was missing: file **extension** must match the real signature; a zip must actually be a Word package (`[Content_Types].xml` + `word/document.xml`); count cap | `test_kt_parsing_and_validation.py` (35) |
| ING-04 | PARTIALLY_EXISTS — real scanner `BLOCKED_BY_DECISION` D-4 | `EXAM_MALWARE_SCANNER_ENDPOINT=host:port` now selects a clamd (`zINSTREAM`) adapter; unreachable/erroring daemon = unavailable (retried, then settled `scan_unavailable`), signature = `infected` (bytes deleted); no/invalid endpoint stays fail-closed with an actionable message. **Not exercised against a real ClamAV** | `test_kt_scanner_and_urls.py`, `test_kt_pipeline.py::test_*scanner*` |
| ING-05 | EXISTS_AND_VERIFIED (unit) | Parsing moved out of the request into the existing outbox worker (own loop, longer lease) and into a **killable child process** with a scrubbed environment; versions are create-only with deterministic anchor ids, a content hash, parser name, warnings and stats; anchors are chunked so a large extract fits Firestore; a redelivered or concurrent job writes one version; URL sources get text | `test_kt_pipeline.py`, `test_kt_parsing_and_validation.py` |
| ING-06 | PARTIALLY_EXISTS | OCR/table/image extraction still **not** done (needs a qualified parser). What is skipped is now reported: tables, images, image-only pages, undecodable text, active PDF content, hidden HTML, truncation; a fully scanned PDF is refused ("looks scanned; OCR is not available") instead of "succeeding" empty | `test_kt_parsing_and_validation.py::test_*scanned*`, `::test_docx_tables_*`, `::test_html_*` |
| ING-07 | EXISTS_AND_VERIFIED (unit/component) | `GET /uploads`, `GET /uploads/{id}` (status, failure code/reason, versions, warnings), paged extracted text, `retry`, `replace` (new upload + links both ways), `delete` (bytes + text removed, row and audit kept, refused while an approved source cites it); progress/retry/replace/delete UI on the faculty exam page | `test_kt_pipeline.py`, `test_kt_routes.py`, `SourceUploadPanel.test.tsx` (10), `examsApi.test.ts` (7) |
| ING-08 | EXISTS_AND_VERIFIED | Hardened further: non-global addresses (CGNAT etc.), IPv4-mapped IPv6, embedded credentials, non-443 ports, relative redirects (each hop re-validated), HTTP errors not ingested, size cap enforced **while streaming**, optional host allowlist (`EXAM_KT_URL_ALLOWED_HOSTS`), canonical URL + fetch time recorded. DNS-rebinding TOCTOU and the network egress proxy remain P11 | `test_kt_scanner_and_urls.py` (65) |
| ING-09 | PARTIALLY_EXISTS | Source text is data throughout this phase (never executed or rendered as markup; hidden HTML text is dropped). Prompt-injection fixtures against the generation provider stay P5 | `test_kt_parsing_and_validation.py::test_html_hidden_*` |

Defects found in the existing code while implementing (all fixed, none was in the audit baseline):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P4-1 | High | Upload bytes were never persisted (`storage_ref` pointed at nothing), so nothing could be retried, replaced or re-parsed | Private quarantine storage + generated names |
| P4-2 | High | Parsing ran inside the request, and the default scanner meant nothing was ever parsed; the scanner setting was read nowhere | Outbox worker + clamd adapter + fail-closed default with an actionable message |
| P4-3 | High | A URL source stored an upload row that stayed `QUARANTINED` forever; its bytes were discarded and it could be approved with no text behind it | Same pipeline as uploads; approval requires parsed content and pins the reviewed version |
| P4-4 | Medium | The whole extracted text lived in one Firestore document (1 MiB limit), so a large document could not be saved | Chunked anchors with a commit-marker version document |
| P4-5 | Medium | Paragraphs over 4000 characters were silently cut | Split on whitespace boundaries, nothing lost |
| P4-6 | Medium | Tables, images and scanned pages were dropped silently; a scanned PDF "parsed" to zero anchors | Warnings per kind of skipped content; refusal when no text exists |
| P4-7 | Medium | The file extension was never checked; any zip with the DOCX MIME type passed | Extension + package-structure checks |
| P4-8 | Medium | URL fetch buffered the whole body before the size check, mishandled relative redirects, and accepted CGNAT/mapped-IPv6/credential/odd-port URLs and HTTP error pages | Streaming cap, `urljoin`, `is_global`, shape checks |
| P4-9 | Low | No per-teacher cap, duplicate detection or rate limit on uploads/links | Count cap, SHA-256 duplicate check, sliding-window limiters |

## 5. Blueprint, generation, review — Prompt 5 and Backend prompts A/B/C

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| GEN-01 | Blueprint: optional topics, question count, item types, marks, negative marking, difficulty distribution, duration, language allowlist, randomization, variant policy | `ExamBlueprint`/`BlueprintSlot` (see DOM-03); no negative marking, topics, distribution | `test_generation_provider_and_jobs.py` (15) | PARTIALLY_EXISTS | Additive optional fields | P5 |
| GEN-02 | Backend computes requested/generated/valid/approved counts; block publication on insufficient approved capacity unless an accepted shortfall | Per-request `ready_count/needs_revision_count/shortfall` only; no publish gate | none | MISSING | Capacity summary + publish gate (with explicit accepted-shortfall flag) | P5 |
| GEN-03 | Send only approved excerpts with exact source/version/anchor; no student PII | `GenerationRequest` has no PII field ✔; approved-only check ✔; **excerpt is the placeholder `"[excerpt for anchor …]"` and anchor ids are not verified against the extracted version** (`exams_generation.py::_resolve_source_bundle`) | `test_generation_validation.py::test_nonexistent_citation_is_blocked` (tautological against a client-built bundle) | EXISTS_BUT_DEFECTIVE | Resolve real anchor text from `ExtractedVersion`; reject unknown anchors | P5 |
| GEN-04 | Store provider/model, prompt version, source versions, job id, retry/cost metadata, status | `GenerationRequest` (model_name, prompt_version, attempts, cost_estimate, last_error, status) | `test_generation_provider_and_jobs.py` | EXISTS_AND_VERIFIED | — | — |
| GEN-05 | Structured output + deterministic validation (domains, anchors, objectives, mark totals, answer-key consistency, duplicate signatures, option-shuffle mappings) | `validation.py`: schema, anchors-in-bundle, objective alignment, marks; signature duplicates. **No answer-key consistency, near-duplicate, option-shuffle checks** | `test_generation_validation.py` (12) | PARTIALLY_EXISTS | Add the three missing checks | P5 |
| GEN-06 | Independent review (cannot upgrade) | `independent_review.py` + `IndependentReviewJobRunner`; schema `Literal` forbids approve; errors are returned in `independent_review_results` | `test_generation_independent_review.py` (12) | EXISTS_AND_VERIFIED | — | — |
| GEN-07 | Coding items: reference solutions + tests executed in the isolated judge; SQL items via disposable DB | Reference solutions/tests are **stored, not executed**; no SQL | `test_generation_validation.py` (structural only) | PARTIALLY_EXISTS (D-5) | Execute references in the judge once a real adapter exists | P8 |
| GEN-08 | Lifecycle draft→validating→needs_review→approved→published; model cannot approve/publish; teacher edit/reject/regenerate; substantive edit = new version + reset approval | `CandidateStatus` (draft, insufficient_source, needs_revision, ready_for_human_review, approved, rejected, published); provider-claimed status ignored; **review = approve/reject only; no edit, no versioned regenerate** | `test_generation_review_and_variants.py` (10) | PARTIALLY_EXISTS | Edit endpoint creating a new version with approval reset | P5 |
| GEN-09 | Variants generated in advance and assigned before the attempt; no LLM during the exam | `reserve_variants_for_slot` (deterministic, idempotent) before attempts; generation is on-demand per slot, never during an attempt | `test_generation_review_and_variants.py` | PARTIALLY_EXISTS | Batch pre-generation per run (optional) | P5 |
| GEN-10 | Post-exam learning feedback (Backend prompt C) | `feedback_service.py` + `TemplateFeedbackProvider` default; `GET /student/gradebook/runs/{id}/feedback` released-only | `test_gradebook_feedback.py` (15) | EXISTS_AND_VERIFIED | — | — |
| GEN-11 | Prompt-injection resilience | Structural: provider status ignored; envelope schema forbids approval; independent review only downgrades | no injection fixtures | PARTIALLY_EXISTS | Fixtures: instruction-bearing excerpts, forged JSON, fake anchors | P5 |

### 5.1 Status after the Prompt 5 implementation

Implemented as [ADR 0021](../adr/0021-grounded-generation-and-versioned-review.md); migration notes in
[compatibility-and-migration.md §10](compatibility-and-migration.md). Evidence is unit-, HTTP- and component-level: the in-memory Firestore, a spy/fake provider,
httpx mock transports and synthetic extracted versions. **No real provider, judge or browser session was used.** Backend suite: 1222 tests passing with
`EXAMS_ENABLED=false` (1117 before + 105 new / adjusted); `ruff check .` clean; frontend `tsc` and `eslint` clean, 9 new Vitest tests.

| ID | Status after P5 | What changed | Evidence |
|---|---|---|---|
| DOM-05 | EXISTS_AND_VERIFIED (unit/HTTP) | `POST …/definitions/{id}/topic-suggestions`: extractive (headings + frequent terms) from approved, parsed sources with page-level evidence; instruction-like passages ignored; nothing stored until the teacher confirms topics on the blueprint | `test_generation_p5_domain.py::test_topic_*`, `test_exams_generation_routes_p5.py::test_topic_*` |
| GEN-01 | EXISTS_AND_VERIFIED (unit/HTTP) | Blueprint accepts topics (confirmed), duration, language allowlist (checked against the judge registry), option shuffle, variant policy and a compact `spec` (question count, per-type counts, marks, negative marks, difficulty %, languages, variants) expanded deterministically; slot indexes validated at creation | `test_generation_p5_domain.py::test_spec_*`, `test_exams_generation_routes_p5.py::test_*blueprint*` |
| GEN-02 | EXISTS_AND_VERIFIED (unit/HTTP) | `GET …/capacity` computes requested / generated / valid / approved per slot from stored candidates; publication blocks a short slot unless the shortfall is explicitly accepted (reason, audited); an empty slot always blocks | `test_*capacity*`, `test_publication_is_blocked_by_insufficient_approved_capacity_*`, `test_exams_policy_validation.py::test_a_short_pool_blocks_*` |
| GEN-03 | EXISTS_AND_VERIFIED (unit/HTTP) | Bundle built from the source's pinned extracted version: real text, exact version/anchor/page/section, relevance-ranked when auto, bundle hash stored; unknown anchors refused before any provider call; no student field exists on the request | `test_bundle_carries_real_text_*`, `test_a_nonexistent_anchor_is_refused_*`, `test_no_student_identity_ever_reaches_the_provider` |
| GEN-04 | EXISTS_AND_VERIFIED (unit/HTTP) | Also stored: provider class, source versions, bundle hash, attempts, retries, latency, usage / cost estimate, completion time, excluded passages | `test_generation_sends_real_excerpts_*` |
| GEN-05 | EXISTS_AND_VERIFIED (unit) | Added answer-key consistency, option uniqueness, multi-select canonical order, positional-option blocker under shuffle, rubric-points = marks, coding reference-solution/test structure, near-duplicate detection (batch + live candidates) | `test_generation_p5_domain.py::test_*` (validation block) |
| GEN-06 | EXISTS_AND_VERIFIED (unit) | The backend now compares the reviewer's blind answer with the key; "agree" with a different or missing answer still pulls the item back | `test_a_reviewer_who_solved_it_differently_*` |
| GEN-07 | PARTIALLY_EXISTS (D-5) | Reference solutions and hidden tests are now preserved and structurally validated (a provider's hidden tests used to be dropped); **not executed**; `execution_verified` + an optional approval requirement (`EXAM_REQUIRE_EXECUTED_CODING_VERIFICATION`, off) are the seam. **No SQL item type or evaluator exists** | `test_coding_needs_a_reference_solution_*`, `test_coding_approval_can_require_executed_verification` |
| GEN-08 | EXISTS_AND_VERIFIED (unit/HTTP) | Approve/reject in a transaction with audit; edit endpoint: substantive edit = new version + reset approval + old version `superseded`, minor edit in place; regenerate; `published` set by publication only; published candidates cannot be edited | `test_an_edit_creates_a_new_version_*`, `test_a_minor_edit_*`, `test_only_a_teacher_decision_*`, `test_a_published_candidate_cannot_be_edited` |
| GEN-09 | EXISTS_AND_VERIFIED (unit/HTTP) | `POST …/runs/{id}/variants/assign` assigns every enrolled student every slot before the attempt from the revision-pinned candidates, create-only, with a seeded option order; nothing on the live path calls a model | `test_every_enrolled_student_gets_a_variant_*`, `test_reassigning_is_idempotent_*`, `test_the_live_exam_path_never_calls_a_model` |
| GEN-11 | EXISTS_AND_VERIFIED (unit/HTTP) for the named fixtures; detection is heuristic | Source injection withheld + reported; forged JSON / approval claims rejected; fabricated anchors blocked; hostile/oversized/non-JSON provider responses are provider errors | `test_instruction_like_source_text_is_withheld_*`, `test_malformed_or_hostile_provider_responses_*`, `test_a_model_that_claims_approval_*` |
| ING-09 | EXISTS_AND_VERIFIED (named fixtures) | Closed together with GEN-11 | as GEN-11 |
| RND-01 | EXISTS_AND_VERIFIED (unit/HTTP) | Per-student seeded option order stored with the assignment and applied by the student question route (key stays with its text); variant policy honoured at assignment | `test_option_order_is_a_stable_permutation_*`, `test_exams_student_service.py` (unchanged green) |

Named test scenarios from the prompt: malformed model JSON, source injection, nonexistent anchors, incorrect answer, duplicate / near-duplicate, insufficient
source, provider timeout and fewer-than-requested output each have at least one test (see the table above and ADR 0021 §Test evidence).

Defects found in the existing code while implementing (all fixed, none was in the audit baseline except P5-1, which refines GEN-03):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P5-1 | High | The model received a placeholder excerpt and an unverified anchor/version, so generated items cited text the model never saw | Grounded bundle from the pinned extracted version |
| P5-2 | High | The provider's hidden tests were dropped when building the candidate | Preserved and validated |
| P5-3 | High | A student's variant assignment was overwritten on re-reserve, and the pool was read live | Create-only assignments; assignment from the revision-pinned candidates |
| P5-4 | Medium | Review was a read-then-write: an approval could race an edit or another review | Compare-and-set transaction + audit |
| P5-5 | Medium | The generation runner had no atomic claim, retry cap or metadata; the HTTP adapters were stubs | `claim_request`, attempt cap, stored metadata, provider-neutral transport |
| P5-6 | Medium | No answer-key, duplicate-option, near-duplicate or shuffle-safety check; "duplicate" meant an identical signature string | Added to validation |
| P5-7 | Medium | A short approved pool was only a publication warning | Blocker unless the shortfall is explicitly accepted |
| P5-8 | Low | No edit/versioning; `published` was never set | Edit endpoint, versions, `mark_published` |
| P5-9 | Low | The fake provider's "Option A …" text would be flagged by the new shuffle check | Neutral wording |

Residual: coding execution (D-5) and SQL (no evaluator); multi-select grading format mismatch (frontend array vs grader string, D-8 / Prompt 10); provider adapters
mock-tested only (D-13); heuristic injection detection; lexical near-duplicate detection.

## 6. Student attempts, drafts, immutable submit — Prompt 6

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| ATT-01 | Allowlisted student DTOs, physically separate from keys/hidden tests/rubrics | `PublicQuestionRef` + `build_public_content` (pure allowlist) | `test_generation_public_content.py` (marker planted in every private field) | EXISTS_AND_VERIFIED | — | — |
| ATT-02 | Only currently released slots of the caller's own active attempt; enforce membership, ownership, run/window, epoch, deadline, slot release on every request | Service checks enrollment, run `RELEASED`, attempt status, deadline, `current_slot_index` only; **membership never checked**; **no previous-question access**; `question_id` on draft routes is not validated against the released slot | `test_exams_decisions.py::test_is_content_reachable_*` | PARTIALLY_EXISTS | Membership via AUTH-05/06 (P1); validate `question_id` ↔ released slot (P6) | P1 / P6 |
| ATT-03 | Versioned, idempotent draft saves | `decide_save_draft`, `Draft.version` | `test_exams_decisions.py` | EXISTS_AND_VERIFIED | — | — |
| ATT-04 | Submit separate/irreversible; expected versions; scoped idempotency keys; durable receipt; same key+different payload = conflict | `decide_submit`, `SubmitReceipt` | `test_exams_decisions.py` (9 submit tests) | EXISTS_AND_VERIFIED | — | — |
| ATT-05 | Lost response recoverable via receipt lookup without late work | `GET /receipt` (read-only) | route + decisions tests | EXISTS_AND_VERIFIED | — | — |
| ATT-06 | Concurrent submit/timeout/teacher-close = one serialized outcome; seal last acknowledged draft or blank once | Per-attempt transactions ✔; **non-transactional draft-version read** between decision and commit (`student_service.py:240`, ADR 0008 §1); timeout path depends on the unscheduled sweeper | pure decision tests only | EXISTS_BUT_DEFECTIVE | Real-Firestore/emulator interleaving test (P6/P11); fix with sweeper (P3) | P3 / P6 |
| ATT-07 | Authenticated attempt/question/result APIs not cacheable; unsent answers in memory only; no offline final submit | `_set_no_store` on every student route + app-level `no-store` middleware; frontend never persists answers (no `localStorage/sessionStorage/IndexedDB` use for exam data) | grep audit; `fe:exam/page.test.tsx` | EXISTS_AND_VERIFIED | — | — |
| ATT-08 | Hidden-content leakage inspection of API/socket/page/cache payloads | DTO-level tests only; no socket/page/cache payload inspection | `test_judge_grading.py` (DTO) | PARTIALLY_EXISTS | End-to-end payload inspection | P6 / P11 |

### 6.1 Status after the Prompt 6 implementation

Implemented as [ADR 0022](../adr/0022-student-attempts-gating-and-idempotent-submit.md); migration notes in
[compatibility-and-migration.md §11](compatibility-and-migration.md). Evidence is HTTP- and service-level against the in-memory Firestore with optimistic
transactions (`exam_inmemory.py`, now also modelling query reads); **no real Firestore, browser session or socket was used.** The student routes had no
HTTP-level tests before this phase, and exercising them showed that three rows rated `EXISTS_AND_VERIFIED` at the audit were not. Backend suite: 1271 tests
passing with `EXAMS_ENABLED=false` (1222 before + 49 new); `ruff check .` clean; frontend `tsc` and `eslint` clean, 1 new Vitest test.

| ID | Status after P6 | What changed | Evidence |
|---|---|---|---|
| ATT-01 | EXISTS_AND_VERIFIED | Unchanged; now also checked over HTTP with private markers planted in every private field | `test_no_student_response_carries_private_content_*` |
| ATT-02 | EXISTS_AND_VERIFIED (unit/HTTP) | `question_id` ↔ released-slot validation on draft save/read, question read and the judge public run; navigator `GET …/slots`; earlier released slots readable behind `EXAM_STUDENT_REVISIT_RELEASED_SLOTS` (default off, decision D-3); future slots never; membership/ownership/run/deadline/state enforced per request. **Epoch** is checked on draft saves and submit but is a constant 1 (nothing advances it) | `test_a_guessed_or_future_question_id_*`, `test_only_the_current_slot_*`, `test_direct_calls_by_a_student_who_is_not_enrolled_*`, `test_the_public_test_run_refuses_*` |
| ATT-03 | EXISTS_AND_VERIFIED (was defective in practice) | A lost-response retry was a conflict. `client_save_id` makes it idempotent; same id + different content conflicts; size cap; mark-for-review flag | `test_a_lost_save_response_is_recovered_*`, `test_a_save_retried_with_its_own_id_*`, `test_an_oversized_answer_*` |
| ATT-04 | EXISTS_AND_VERIFIED (was defective) | The receipt is returned for the same key in **every** resolved state (`SUBMITTED` was missed); request hash on the receipt; reused key with another payload or command conflicts | `test_a_retry_with_the_same_key_*`, `test_the_same_key_with_a_different_payload_*`, `test_a_double_click_submit_*` |
| ATT-05 | EXISTS_AND_VERIFIED (was defective) | Recovery by retry or `GET …/receipt` works after the deadline and never permits late work (a new key, a draft save and a finish all conflict) | `test_a_lost_submit_response_is_recovered_after_the_deadline_*` |
| ATT-06 | EXISTS_AND_VERIFIED (fake store) / UNVERIFIED (real Firestore) | Draft versions are read inside the seal transaction in submit/finish, the sweeper and terminate; submit/terminate/expiry serialize on the attempt; expiry seals the last acknowledged draft or blank once; a crashed seal leaves nothing | `test_concurrent_submit_and_faculty_terminate_*`, `test_a_draft_saved_between_the_read_and_the_commit_*`, `test_the_receipt_always_lists_exactly_*`, `test_a_sweeper_that_crashes_mid_seal_*`, `test_expiry_seals_*` |
| ATT-07 | EXISTS_AND_VERIFIED | Unchanged; regression tests assert `no-store` on every student route and on a full set of responses; the service worker still never intercepts `/api/` | `test_every_student_route_sets_no_store`, `test_no_student_response_carries_private_content_*` |
| ATT-08 | PARTIALLY_EXISTS | HTTP responses (16 calls) and live event payloads are inspected for planted private markers. Browser page / DOM / HTTP cache / socket frames in a real browser are not inspected (Prompt 10/11) | `test_no_student_response_*`, `test_live_events_carry_no_question_or_answer_material` |

Defects found in the existing code while implementing (all fixed):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P6-1 | High | A submit retry after commit returned `attempt_not_in_progress` instead of the receipt (state `SUBMITTED` was not handled; the unit test used `FINALIZED`) | Receipt answers in all resolved states |
| P6-2 | Medium | A reused key with a different payload was not detected | Request hash on the receipt |
| P6-3 | Medium | A draft save retried after a lost response was a conflict | `client_save_id` replay |
| P6-4 | High | Draft routes accepted any question id: unbounded documents of unbounded size, and a draft id that was not a slot id crashed grading | Released-slot validation, size cap, grading skips non-slot ids |
| P6-5 | High | The public test run accepted guessed and future question ids (public tests of an unreleased slot could be run), returned 500 on a malformed id, and worked before admission and after sealing | Released-slot + active-attempt checks |
| P6-6 | High | Submit/finish, the sweeper and terminate read draft versions outside their transaction | In-transaction read |

Residual: real-Firestore verification of the transactional query read and a synchronized submit burst (Prompt 11); `epoch` is not an advancing fence (a two-tab
takeover policy is a product choice); decision D-3 (sequential release with review of released slots) is a flag until the institution decides; grading reads
the current draft, not a sealed copy.

## 7. UI: timer, navigator, themes, control room — Prompt 7

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| UI-01 | Continuously updating timer from the server deadline + server-time samples, not the browser wall clock | `ExamShellHeader.formatRemaining` = `deadline − Date.now()` at render; **no interval/tick found** in the header or student page (refreshes only on re-render); no server-time sample/skew correction | `fe:ExamShellHeader.test.tsx` (mm:ss format, placeholder) | EXISTS_BUT_DEFECTIVE (runtime behavior UNVERIFIED) | Ticking hook anchored to a server-time offset sampled from API responses | P7 |
| UI-02 | Time-remaining warnings (10 min default + configured), accessible, not repetitive | none (only proctoring warnings, `aria-live="polite"`) | none | MISSING | Threshold list in policy + announce-once live region | P7 |
| UI-03 | Previous/Next across released questions | Next only (`POST /next-question`, "forward-only" by design); no previous route/UI | none | MISSING (`BLOCKED_BY_DECISION` D-3) | Read access to already-released slots + view index separate from release index | P6/P7 |
| UI-04 | Right-side numbered navigator with answered/unanswered/marked/locked counts | none (single-question flow) | none | MISSING | After D-3 | P7 |
| UI-05 | Mark-for-review | none (no model field, route or UI) | none | MISSING | Per-slot flag on the draft (additive) | P6/P7 |
| UI-06 | Draft-save status and connection status | `Saved (vN)` + connection badge (connecting/connected/reconnecting/polling/offline) | `fe:ExamShellHeader.test.tsx` | EXISTS_AND_VERIFIED | — | — |
| UI-07 | Light/dark mode; contrast | Global `ThemeContext` + `ThemeToggle` (root layout) + CSS variables used on exam pages; contrast not verified | `fe:ThemeContext.test.tsx` | PARTIALLY_EXISTS | Contrast checks in axe on exam pages | P7/P10 |
| UI-08 | Keyboard/screen-reader behavior; responsive layout | Timer `aria-live`, labelled inputs, `role` dialog/status; axe scan only on `/exams/login` | `e2e:exams-accessibility.spec.ts` (2) | PARTIALLY_EXISTS | Axe + keyboard coverage of exam pages | P10 |
| UI-09 | Submit confirmation, final receipt, draft vs final distinction | Confirmation dialog + receipt page | `fe:exam/page.test.tsx` (4) | EXISTS_AND_VERIFIED | — | — |
| UI-10 | Reconcile from the server on reconnect | `useLiveSocket` (`onReconcile` per event, jittered backoff, 10 s polling fallback after 5 failures) | `fe:liveSocket.test.ts` (7) | EXISTS_AND_VERIFIED | — | — |
| UI-11 | Teacher views: builder, sources, topics, blueprint/scoring, question review, schedule/publish, short link, live status (freshness), per-student grant/revoke, pause/resume, extension, terminate/recovery, incidents, gradebook | Exist and are wired to real routes: builder, URL-source registration + review, blueprint (single-choice + coding), generate, candidate review, publish/release, variant assignment, control room (pause/resume/close/end, "Snapshot as of" = client fetch time), extension, incident resolve, gradebook/appeals/similarity/lockdown. **Missing:** topics, scoring config, short link, per-student grant/revoke, terminate, upload management, institution/roster UI | `fe:results/page.test.tsx` (2) only; live-verified 2026-10-04 | PARTIALLY_EXISTS | Per-feature, after the corresponding backend deltas | P7 |
| UI-12 | Destructive actions confirm, audit, show conflict feedback | Release confirmation exists; 409 conflicts show raw errors; confirmation for close/end UNVERIFIED | none | PARTIALLY_EXISTS | Conflict-specific message + confirms | P7 |
| UI-13 | Existing design tokens/routes only | CSS variables, `.card`, `.badge*`, `.btn-*` reused | — | EXISTS_AND_VERIFIED | — | — |
| UI-14 | Exam API client: pinned origin, `getClientAuthToken`, `no-store` | `examsApi.ts` | — | EXISTS_AND_VERIFIED (login page calls `getIdToken` directly — acceptable pre-session) | — | — |

### 7.1 Status after the Prompt 7 implementation

Implemented as [ADR 0023](../adr/0023-exam-workspace-ui-timer-navigator-and-teacher-controls.md); migration notes in
[compatibility-and-migration.md §12](compatibility-and-migration.md). Evidence is backend HTTP tests against the in-memory Firestore plus jsdom component tests
with fake timers; **no real browser, screen reader, axe run or Firestore was used.** Backend suite: 1303 tests passing with `EXAMS_ENABLED=false` (1271 before
+ 32 new); `ruff check .` clean; frontend `tsc` and `eslint` clean; Vitest 163 passing (run with `--maxWorkers=2 --testTimeout=20000` - under default
parallelism the already-slow `signup`, `submit`, `BlueprintPanel` and `CodeEditorPanel` files time out on this machine, as before this phase).

| ID | Status after P7 | What changed | Evidence |
|---|---|---|---|
| UI-01 | EXISTS_AND_VERIFIED (jsdom, fake timers) | Server-time offset from `performance.now()` (monotonic); a once-a-second tick; `server_time` on state, slots and the faculty run/attempt lists. A device clock set to 1999 still shows the server's time left | `serverClock.test.ts`, `page.test.tsx::shows the time left from the server's clock…`, `::keeps ticking…`, `test_exams_ui_backend_p7.py` |
| UI-02 | EXISTS_AND_VERIFIED (jsdom) | `ExamPolicy.time_warning_minutes` (default `[10]`); announced once per threshold in a polite live region; time-up is the only `alert`, locks editing and polls for the sealed state; **never auto-submits** | `page.test.tsx` (timer block), `test_exams_ui_backend_p7.py::test_the_default_warning_*`, `::test_invalid_warning_*` |
| UI-03 | PARTIALLY_EXISTS (D-3) | Previous/Next over released slots, view index separate from release index; with `EXAM_STUDENT_REVISIT_RELEASED_SLOTS` off (default) Previous is disabled and no earlier question is requested | `page.test.tsx` (navigator block) |
| UI-04 | EXISTS_AND_VERIFIED (jsdom) | Right-hand navigator: numbered buttons with answered / unanswered / marked / locked state in text (not colour only) and four counts | `QuestionNavigator.test.tsx` (8) |
| UI-05 | EXISTS_AND_VERIFIED (jsdom) | Mark-for-review toggle (`aria-pressed`) saved with the draft | `page.test.tsx::marks a question for review…` |
| UI-07 | PARTIALLY_EXISTS | Real finding: dark `--text-muted` was ≈3.9:1 on the card surface. Exam-scoped override (`.dark .exam-scope`) raises it; token ratios are tested. No rendered-page axe run | `examTheme.test.ts` (4) |
| UI-08 | PARTIALLY_EXISTS | Labelled controls, focus-managed dialogs (start on Cancel, Escape, Tab contained, focus restored), `aria-current`, live regions. Not tested with a screen reader or in a real browser | `ConfirmDialog.test.tsx` (7), `QuestionNavigator.test.tsx` |
| UI-11 | PARTIALLY_EXISTS | Added: schedule window/timezone/duration/warnings and topics/marks fields, short-link panel, per-student extend / end attempt / recover, per-student grant / revoke, server-stamped freshness. **Still missing:** upload management beyond the existing panel, institution/roster UI, source-derived topic suggestion UI, a negative-marking control (grading does not apply it, D-8) | `AttemptActions.test.tsx` (6), `ShortLinkPanel.test.tsx` (7), `control/page.test.tsx` (8), `examSchedule.test.ts` |
| UI-12 | EXISTS_AND_VERIFIED (jsdom) | Every state change confirms, names the student/run and consequence, asks for an audited reason where it matters, uses a key generated at dialog open, and reloads after success **or** rejection; 409s become plain sentences | `examConflicts.test.ts`, `control/page.test.tsx::on a version conflict…` |
| SHR-01 | PARTIALLY_EXISTS (flag, default off) | `EXAM_SHORT_LINKS_ENABLED`; random 8-char code, one active per run, revocable, regenerate; resolver needs an enrolled signed-in student, identical 404 for every refusal, rate-limited, audited | `test_exams_ui_backend_p7.py` (short-link tests), `join/[code]/page.test.tsx` (3) |
| LIV-01 | EXISTS_AND_VERIFIED (jsdom) | Freshness line shows the **server's** time at the last refresh and a ticking age; stale warning when the feed is not connected and data is >30 s old | `control/page.test.tsx` (3) |

Defects found in the existing code while implementing (all fixed):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P7-1 | High | The timer was `deadline − Date.now()` computed once per render with no tick, and used the device clock | Server-offset clock + 1 s tick |
| P7-2 | Medium | Run-command conflicts replaced the whole control room with a generic error page | Inline plain-language feedback + reload |
| P7-3 | Medium | "Snapshot as of" was the teacher's browser time, implying server freshness | Server time + age |
| P7-4 | Medium | Dark-theme muted text below AA | Exam-scoped override |

Residual: no real-browser, screen-reader or axe verification; the timer is accurate to one round trip; `epoch` is still a constant; institution/roster teacher
screens are not built; decisions D-3 (revisit) and D-9 (warning thresholds) stay with the institution; nothing ran against real Firestore.

## 8. Coding and SQL judge — Prompt 8

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| JDG-01 | Hardened judge adapter + durable worker; no user code on the app host | No subprocess/exec anywhere in `backend/app` (grep); durable outbox + sweep ✔; **`Judge0HttpAdapter.submit` raises `NotImplementedError`**; default provider = in-memory `DeterministicFakeJudgeProvider` singleton | `test_judge_provider.py` (8), `test_judge_worker.py` (11), `test_judge_service.py` (13) | PARTIALLY_EXISTS (`BLOCKED_BY_DECISION` D-5: architecture approval) | Real adapter only after approval; staging isolation evidence | P8 |
| JDG-02 | Server-owned language registry (C, C++, Java, Python, JS/TS) | `default_allowlist()`: python3.11, java17, cpp17; none validated against a provider; no C, JS/TS | `test_judge_runtime_allowlist_and_submission.py` (11) | PARTIALLY_EXISTS | Extend after provider validation | P8 |
| JDG-03 | SQL only with a qualified disposable runner | No SQL item type | none | MISSING (D-5) | Out until qualified | P8 |
| JDG-04 | Pinned versions; result bound to submission hash, variant, suite version | `GradingResult` carries `submission_hash`, `candidate_id`, `runtime_image`, `test_suite_version` | `test_judge_service.py` | EXISTS_AND_VERIFIED | — | — |
| JDG-05 | CPU/memory/wall/process/output/FS/concurrency limits; block network/metadata/host mounts | Constants (cpu 5 s, memory 256 MB) sent to the provider; everything else is provider infrastructure | `test_judge_worker.py` | UNVERIFIED (infra) | Staging evidence | P8 / P11 |
| JDG-06 | Hidden tests/expected output never reach the student | DTOs by omission (`public_test_result_dto`, `sealed_grade_summary_dto`) | `test_judge_grading.py` (20) | EXISTS_AND_VERIFIED | — | — |
| JDG-07 | Infra failures pending/retryable, not silent zero | `INFRA_ERROR` retry with cap; **terminal state when retries are exhausted is undefined** (critical log only) | `test_judge_worker.py` | EXISTS_BUT_DEFECTIVE | Explicit exhausted state + operator path | P8 |
| JDG-08 | Provider callbacks authenticated or polled securely and de-duplicated | Poll-only; per-test token ownership verified; terminal submissions are no-ops | `test_judge_worker.py` | EXISTS_AND_VERIFIED | — | — |
| JDG-09 | Regrade lineage | `create_regrade` → new version; MCQ/short-answer likewise | `test_judge_service.py`, `test_judge_grading.py` | EXISTS_AND_VERIFIED | — | — |
| JDG-10 | MCQ / short-answer semantics | MCQ exact-match only; short answer manual review queue | `test_judge_outbox_handler.py` (8) | PARTIALLY_EXISTS (D-8) | Partial credit / negative marking semantics | P3/P10 |
| JDG-11 | Grading triggered idempotently after finalization | `GradingTriggerOutboxHandler` | `test_judge_outbox_handler.py` | EXISTS_AND_VERIFIED | Only for attempts that actually finalize (TMR-02) | — |
| JDG-12 | Authorization on judge routes | Student public-run relies on variant assignment (no enrollment check); faculty routes unscoped | none route-level | EXISTS_BUT_DEFECTIVE | See AUTH-05 / AUTH-09 | P1 |
| JDG-13 | Student "Run public tests" end to end | Rate limit 10 / 300 s per (run, uid, question); stored as `PUBLIC_RUN` submissions | `test_judge_service.py` | EXISTS_AND_VERIFIED (fake provider) | — | — |
| JDG-14 | Clipboard blocking | Client-only DOM handlers; recorded as `CLIPBOARD_BLOCKED` informational observation (never counted) | `fe:CodeEditorPanel.test.tsx` | EXISTS_AND_VERIFIED as a deterrent; not enforcement | State limitation in incident UI | P9 |

### 8.1 Status after the Prompt 8 implementation

Implemented as [ADR 0024](../adr/0024-judge-hardening-durable-grading-and-registry.md); migration notes in
[compatibility-and-migration.md §13](compatibility-and-migration.md). **Decision D-5 (judge architecture) is not approved, so the real adapter
and SQL were not built** and no code ran anywhere: every test below uses the deterministic fake provider and in-memory repositories. **Mocks
do not prove sandbox security** - isolation, resource enforcement, egress / metadata blocking and host-mount denial need staging evidence from
the real judge (Prompt 11). Backend suite: 1355 tests passing with `EXAMS_ENABLED=false` (1303 before: one obsolete test removed, 53 added);
`ruff check .` clean; frontend `tsc` and `eslint` clean.

| ID | Status after P8 | What changed | Evidence |
|---|---|---|---|
| JDG-01 | PARTIALLY_EXISTS (BLOCKED_BY_DECISION D-5) | The fake judge ("Accepted" for everything) is no longer used in production: `UnconfiguredJudgeProvider` reports the judge unavailable. The HTTP adapter is still unimplemented, so a configured provider yields infrastructure errors, never verdicts. No user code runs in the API process (the API process opens no socket for hostile source) | `test_the_dev_fake_is_not_used_in_production`, `test_without_a_configured_judge_*`, `test_source_that_targets_the_network_*` |
| JDG-02 | PARTIALLY_EXISTS | Registry now lists C, C++, Java, Python, JavaScript and TypeScript candidates; with a real provider only keys in `EXAM_JUDGE_VALIDATED_LANGUAGES` are usable (students and blueprints); none is validated by default. Judge0 ids are unverified defaults | `test_the_registry_names_exactly_*`, `test_with_a_real_judge_only_languages_*`, `test_only_registry_keys_resolve_*` (8 hostile keys) |
| JDG-03 | MISSING (BLOCKED_BY_DECISION D-5) | Unchanged on purpose; a test pins that no SQL item type or registry entry exists | `test_sql_stays_out_until_a_disposable_runner_is_qualified` |
| JDG-04 | EXISTS_AND_VERIFIED | Suite version is now the variant's version (was constant 1); final submissions, grading results and first MCQ / review records have deterministic ids | `test_a_result_is_bound_to_the_exact_code_variant_runtime_and_suite_version` |
| JDG-05 | UNVERIFIED (infra) | The application side is now server-owned and tested: CPU, wall, memory, stack, process and file-size limits and `enable_network=False` sent with every test (no callback / image / flag field exists); stored output bounded to 16 384 chars per stream; student test runs refused with 503 when the queue is saturated. Enforcement is the judge's | `test_every_request_to_the_judge_carries_*`, `test_the_sandbox_network_is_off_*`, `test_a_request_has_no_field_*`, `test_an_output_flood_*`, `test_a_saturated_judge_*` |
| JDG-07 | EXISTS_AND_VERIFIED (fake provider) | Was defective. Unsent or failed sealed submissions are retried with backoff by the sweep; after `EXAM_JUDGE_MAX_INFRA_ATTEMPTS` failures or `EXAM_JUDGE_STALL_SECONDS` without a result they become `infra_exhausted`, are listed for the run and can be requeued (audited); the gradebook keeps them PENDING, never zero | `test_a_failed_submission_is_retried_*`, `test_when_retries_run_out_*`, `test_requeue_only_touches_the_named_run`, `test_a_submission_the_judge_never_finishes_*`, route tests |
| JDG-08 | EXISTS_AND_VERIFIED | Still poll-only. Results are written under deterministic ids before a submission is marked done; one failing submission no longer stops the sweep; the public-run poll checks ownership, run, question and purpose before polling | `test_a_duplicate_provider_result_*`, `test_one_bad_submission_*`, `test_a_submission_that_is_not_the_students_own_test_run_is_never_polled` |
| JDG-09 | EXISTS_AND_VERIFIED (was defective) | A regrade that finishes after the first poll is now recorded on the submission and lands once as the next version; earlier versions untouched; a crash after the write adds no third version | `test_a_regrade_that_finishes_later_*`, `test_a_crash_after_the_regrade_was_written_*` |
| JDG-12 / JDG-13 | EXISTS_AND_VERIFIED | Unchanged scope checks; a test run that could not be sent answers `503 judge_unavailable` / `judge_busy` with `Retry-After`, and the student UI says nothing ran | `test_judge_routes_p8.py`, `page.test.tsx` (3) |

Scenario coverage requested by the prompt (what each test can and cannot show):

| Scenario | Covered by | Limit |
|---|---|---|
| Correct / incorrect code, compile / runtime errors, time and memory limits | existing `test_judge_worker.py` (status → outcome mapping) + compile-message test | The fake returns scripted statuses; no code is compiled or run |
| Output flood | storage bound test | Does not show the judge stops a flood |
| Network / metadata denial | request carries `enable_network=False`, no steering fields, API process opens no socket | Denial itself is the judge's; UNVERIFIED |
| Hidden-data leakage | marker planted in hidden expected output/stdout/stderr is absent from the summary DTO, the test-run response and what a test run sends | Hidden tests are still sent to the provider with a sealed submission |
| Queue saturation, duplicate results, worker crash, infrastructure retry, regrade lineage | the tests listed above | In-memory store; real Firestore transactions and `create` conflicts are unverified |

Defects found in the existing code while implementing (all fixed):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P8-1 | High | Production without a provider used the fake judge ("Accepted") | `UnconfiguredJudgeProvider` |
| P8-2 | High | Infrastructure errors never retried; no exhausted state; unsent submissions stranded | Sweep retry, backoff, `infra_exhausted`, operator routes |
| P8-3 | High | A regrade that was not instant was silently dropped | Regrade marker on the submission |
| P8-4 | High | A crash between "marked done" and "result written" lost the grade | Commit-before-done, deterministic ids |
| P8-5 | Medium | Retries duplicated submissions, MCQ results and short-answer reviews (and could reset a scored review) | Deterministic ids, create-if-absent |
| P8-6 | Medium | One failing poll aborted the whole sweep tick | Per-submission isolation |
| P8-7 | Medium | Compile message dropped; stdout/stderr unbounded | Compiler output as stderr; 16 384-char cap |
| P8-8 | Low | Constant suite version; poll before ownership check; only CPU/memory limits sent; unlisted language aborted an attempt's grading loop | Variant version; check first; full limit set; operator state |

Residual: no approved judge, no real execution, no SQL; Judge0 ids and images unvalidated; sandbox isolation and limit enforcement unverified;
hidden tests travel to the provider with each sealed submission; the in-flight cap and limiters are per instance; no UI for the stalled list yet
(the routes exist); real-Firestore behaviour of create-if-absent and the new queries unverified.

## 9. Proctoring, live control, lockdown — Prompt 9 (and Prompt 7 control room)

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| PRC-01 | Institution-approved proctoring profile | Per-run `warning_threshold`; grace/cooldown are global env (`EXAM_PROCTORING_GRACE_SECONDS` 2.0, `COOLDOWN` 5.0); no institution profile entity | `test_proctoring_ledger_service.py` | PARTIALLY_EXISTS (D-7) | Per-run/institution overrides if required | P9 |
| PRC-02 | Append-only incident ledger | `IncidentLogRepository` has create/list only | `test_proctoring_decisions.py` (20), `test_proctoring_ledger_service.py` (7) | EXISTS_AND_VERIFIED | — | — |
| PRC-03 | Observations, not proof; dedupe, grace, cooldown | `correlate_observation` (bounded recent-event ids, grace, cooldown) | `test_proctoring_decisions.py::test_duplicate_event_id_is_deduped`, `…flapping…`, `…burst…` | EXISTS_AND_VERIFIED | — | — |
| PRC-04 | Camera/permission/network failures informational | `INFORMATIONAL_KINDS` | `…test_blur_alone_is_informational…`, `…permission_denied_and_camera_error…` | EXISTS_AND_VERIFIED | — | — |
| PRC-05 | Threshold → BLOCKED transactionally; stop writes/delivery; preserve drafts; audited review/recovery/appeal | `record_observation` sets `BLOCKED`; submit/question gating (`attempt_blocked`); `resolve_block`; appeals service | `test_proctoring_ledger_service.py`, `test_exams_decisions.py::test_submit_rejects_when_blocked`, `test_proctoring_appeals_service.py` (4) | EXISTS_AND_VERIFIED | — | — |
| PRC-06 | Ledger never reset to unblock | `resolve_block` never touches counts | `…test_resolve_block_*` | EXISTS_AND_VERIFIED | — | — |
| PRC-07 | Private evidence, short-lived tickets, access audit, retention across derivatives | Single-use expiring upload/view tickets; `evidence_access_log`; retention sweeper; **default storage = `InMemoryEvidenceStorage` unless `EXAM_EVIDENCE_STORAGE_BUCKET`** | `test_proctoring_evidence_service.py` (11), `test_proctoring_retention.py` (2) | EXISTS_AND_VERIFIED (real bucket/IAM UNVERIFIED) | Staging IAM evidence | P11 |
| PRC-08 | Submission never fails solely because evidence upload failed | `student_service` does not import the evidence service | by construction (no explicit test) | EXISTS_AND_VERIFIED (add test) | One explicit test | P9 |
| PRC-09 | No face recognition / biometric / automatic AI misconduct decisions | None present (grep) | — | EXISTS_AND_VERIFIED | — | — |
| LIV-01 | Live candidate status with freshness; realtime + fallback | Ticketed WebSocket; per-run `seq`/`state_version`; heartbeat; client polling fallback; control page "Snapshot as of" (client time) | `test_live_*` (18), `fe:liveSocket.test.ts` (7) | EXISTS_AND_VERIFIED (single-process connection manager) | Server-stamped freshness (optional) | P7 |
| LIV-02 | Live authorization scoped to the run | `exams_live._resolve_role`: any faculty obtains a FACULTY ticket for **any** run | none route-level | EXISTS_BUT_DEFECTIVE | AUTH-09 scope on ticket issuance | P1 |
| LCK-01 | Managed-lockdown (SEB) admission, honestly scoped | `verify_seb_admission` (header hash; fail-closed when enabled without keys; never User-Agent) | `test_lockdown_decisions.py` (11), `test_lockdown_service.py` (6) | EXISTS_AND_VERIFIED (no real SEB pilot) | — | P11 |

### 9.1 Status after the Prompt 9 implementation

Implemented as [ADR 0025](../adr/0025-proctoring-ledger-review-appeals-and-evidence.md); migration notes in
[compatibility-and-migration.md §14](compatibility-and-migration.md). Evidence is HTTP- and service-level against the in-memory Firestore with
optimistic transactions and concurrent callers (`exam_inmemory.py`); the earlier "verified" rows rested on pure decision tests and
pass-through fakes. **No real browser, camera, bucket, IAM policy or identity was used.** Backend suite: 1423 tests passing with
`EXAMS_ENABLED=false` (1355 before: four obsolete appeal tests replaced, 72 added); `ruff check .` clean; frontend `tsc` and `eslint` clean, Vitest 193 passing
(run with `--maxWorkers=2 --testTimeout=20000`).

| ID | Status after P9 | What changed | Evidence |
|---|---|---|---|
| PRC-01 | PARTIALLY_EXISTS (D-7) | Optional per-run `proctoring_profile`: grace, cooldown, allowance after a review, evidence retention (bounded; server stamps `set_by`); unset = deployment defaults; teacher UI fields. **Not built:** an institutional approval step for a profile and the institution's retention periods (D-7) | `test_proctoring_p9.py` (profile block), `examSchedule.test.ts` |
| PRC-02 | EXISTS_AND_VERIFIED (HTTP) | Append-only now also pinned: no delete / update / reset method exists on the ledger repositories; reviewer decisions are entries (`source="reviewer"`, `actor_uid`), late events are `source="server"` | `test_the_ledger_has_no_delete_or_reset_path`, `test_allowing_to_continue_never_resets_the_ledger_*` |
| PRC-03 | EXISTS_AND_VERIFIED (HTTP, concurrent) | Replay storms and 12 simultaneous deliveries of one event count once; flapping inside the cooldown counts once. Residual: the dedupe window is the last 64 ids | `test_a_replay_storm_*`, `test_the_same_event_delivered_by_many_requests_at_once_*`, `test_a_flapping_departure_*` |
| PRC-04 | EXISTS_AND_VERIFIED (HTTP) | 6 technical / informational kinds x 40 events never count or block | `test_technical_and_informational_signals_*` |
| PRC-05 | EXISTS_AND_VERIFIED (was defective) | Threshold-vs-submit race has one outcome (20 raced pairs); events after the attempt ended are recorded, never counted, never an error; a blocked attempt refuses draft writes, submit, question delivery and next-question while keeping its drafts; it still expires and is sealed with the last saved answers; the stale sweep survives an ended attempt; the next trivial blip no longer re-blocks after a review | `test_a_threshold_crossing_racing_a_submit_*`, `test_events_after_the_attempt_ended_*`, `test_a_blocked_attempt_*`, `test_the_stale_sweep_survives_*`, `test_allowing_to_continue_*` |
| PRC-06 | EXISTS_AND_VERIFIED (HTTP) | A review keeps every count and entry; blocking resumes only past `review_floor` (run allowance); reversal keeps every step; each decision needs a reason and is audited | `test_the_runs_allowance_*`, `test_a_review_can_be_reversed_*`, `test_upholding_leaves_the_block_*` |
| PRC-07 | EXISTS_AND_VERIFIED (fake storage) / UNVERIFIED (real bucket, IAM) | Production without a bucket refuses instead of using memory; byte-level image check; verified type served with `nosniff`; stale requests become missing; view tickets only for verified evidence and audited (issued / viewed / denied); retention oldest-first, per-run window, held for blocked attempts and pending appeals, failure-isolated; no derivatives exist | `test_proctoring_p9.py` (evidence and retention blocks) |
| PRC-08 | EXISTS_AND_VERIFIED | Explicit tests: a submit succeeds while evidence storage fails or is unconfigured, and the student module never imports the evidence module | `test_submission_succeeds_when_evidence_cannot_be_stored`, `test_submission_does_not_depend_on_the_evidence_module_at_all` |
| PRC-09 | EXISTS_AND_VERIFIED | A test scans the proctoring package for face / biometric / liveness / AI-misconduct terms | `test_no_biometric_or_automatic_misconduct_capability_exists` |
| (appeals) | EXISTS_AND_VERIFIED (was defective) | One pending appeal per attempt; decided only as upheld / overturned; every decision appended and audited; concurrent reviewers lose neither; an appeal never changes the attempt; rate-limited; fileable after the window | `test_proctoring_appeals_service.py` (10), `test_an_appeal_*` |

Scenarios requested by the prompt and where they are covered: event bursts / replay (PRC-03 rows), threshold-submit race (PRC-05), permission
denial (PRC-04 and `test_a_missing_snapshot_for_a_denied_camera_*`), missing / stale evidence (`test_a_request_that_never_completes_*`,
`test_an_upload_that_is_not_an_image_*`), unauthorized evidence access (`test_unauthorized_evidence_access_*`, `test_another_students_upload_ticket_*`,
route-scope check), retention deletion (retention block), review reversal and appeal (PRC-06, appeals), expiry while blocked (PRC-05).

Defects found in the existing code while implementing (all fixed):

| # | Severity | Defect | Fix |
|---|---|---|---|
| P9-1 | High | An observation after submit / expiry raised an illegal transition (5xx) and kept changing the ledger | Attempt read first; late events recorded as server entries |
| P9-2 | High | After a reviewer allowed a student to continue, the next uncounted blip blocked them again | Block only on a newly counted incident past the review floor |
| P9-3 | Medium | One ended attempt aborted the stale-incident sweep for everyone | Per-state isolation; close without counting |
| P9-4 | High | Appeals could be reset to pending and an earlier decision was overwritten in place | Append-only decision history in a transaction |
| P9-5 | High | In production without a bucket evidence lived in process memory while records said verified | 503 and no retention loop |
| P9-6 | Medium | Retention could not reach due evidence beyond the first 100 documents, and deleted evidence under review | Oldest-first paging, holds |
| P9-7 | Medium | Reviews were not audited, were labelled as browser observations, took no reason, and the notes were visible to the student | Reviewer entries, audit event, reason required, student sees the decision only |
| P9-8 | Medium | Uploads were trusted by label; viewer could sniff; stale requests stayed `requested`; tickets issued for nothing; issuing / refusals unaudited | Signature check, `nosniff`, expiry sweep, verified-only tickets, access outcomes |
| P9-9 | Low | The screens crashed on a conflict; the camera hook posted to `/upload/undefined` after a refused request | Inline feedback and confirm dialogs; honest missing record |

Residual: no real browser / camera / bucket / IAM evidence (Prompt 10 / 11); signals and their timestamps come from the browser and can be wrong or forged;
the dedupe window is 64 ids; the camera-hook change is not unit-tested; limiters are per instance; access-log entries and consumed tickets are not purged;
D-7 (who approves a profile, retention periods for evidence and the ledger) is open.

## 10. Gradebook, accessibility, PWA, exports — Prompt 10

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| GRD-01 | Scoped gradebook, released results, exact count semantics | Student routes use token uid, require a release; counts via `compute_all_counts`; negative marking/partial-credit semantics undefined | `test_gradebook_aggregation_service.py` (12), `test_gradebook_release_service.py` (8) | PARTIALLY_EXISTS (D-8) | Define semantics with MRK-01 | P10 |
| GRD-02 | Original submissions preserved; corrected results = new version + reason + audit | `ReleaseRecord` versions; regrade lineage; append-only | `test_gradebook_release_service.py::test_second_release_increments_version_and_keeps_history` | EXISTS_AND_VERIFIED | — | — |
| GRD-03 | Similarity is reviewer evidence only; excludes starter code; no automatic penalty | `similarity_service` strips starter code; never mutates scores | `test_gradebook_similarity.py` (7) | EXISTS_AND_VERIFIED (real JPlag unqualified) | — | — |
| GRD-04 | Authenticated, scoped, expiring exports; CSV formula injection protected | Single-use expiring tickets bound to the issuing caller; `sanitize_csv_cell` | `test_gradebook_export_service.py` (11), `test_gradebook_decisions.py` (24) | EXISTS_AND_VERIFIED | — | — |
| GRD-05 | Unreleased results/feedback denied to students | `_require_released` / `ResultsNotReleasedError` | `test_gradebook_feedback.py::test_feedback_service_raises_when_not_released` | EXISTS_AND_VERIFIED | — | — |
| GRD-06 | Appeals (grade + proctoring) | Both workflows with resolver audit | `test_gradebook_release_service.py`, `test_proctoring_appeals_service.py` | EXISTS_AND_VERIFIED | — | — |
| GRD-07 | Faculty gradebook/proctoring/lockdown routes scoped to the run | `require_exam_faculty` only; ids (`appeal_id`, `snapshot_id`, `job_id`) checked for existence, not for the caller's relation | none | EXISTS_BUT_DEFECTIVE | AUTH-09 | P1 |
| PWA-01 | Only the static shell cached; authenticated pages/APIs network-only/no-store at SW, framework, CDN, API | SW precaches 4 static files and never calls `respondWith` for API/page requests; API: per-route + middleware `no-store`; Next: only `/_next/static` headers (prod immutable) — **no explicit `no-store` for `/exams/**` HTML** | `e2e:exams-pwa.spec.ts` (2) | PARTIALLY_EXISTS | Add `Cache-Control: private, no-store` for `/exams/:path*` in `next.config.ts` | P10 |
| PWA-02 | SW updates never interrupt an active attempt; cache hygiene on logout | `useExamServiceWorker({attemptActive})` holds `skipWaiting`; SW caches no user data so there is nothing to purge | `fe:useExamServiceWorker.test.ts` (7), e2e above | EXISTS_AND_VERIFIED | — | — |
| A11Y-01 | Accessibility automation: axe, keyboard, themes/contrast, accommodations | Axe + keyboard only for `/exams/login`; accommodation tests for camera waiver; manual checklist doc | `e2e:exams-accessibility.spec.ts` (2), `fe:useCameraCapture.test.ts` (4) | PARTIALLY_EXISTS | Cover exam pages and theme contrast | P10 |

### 10.1 Status after the Prompt 10 implementation

Implemented as [ADR 0026](../adr/0026-gradebook-frozen-releases-scoring-semantics-exports-and-cache-boundary.md); semantics in
[scoring-and-gradebook-semantics.md](scoring-and-gradebook-semantics.md); migration notes in [compatibility-and-migration.md §15](compatibility-and-migration.md).
Gradebook evidence is HTTP- and service-level against the in-memory Firestore (real routers and repositories, real resource-scope checks); the service worker is tested by running the **shipped** `exams-sw.js` against fakes;
`next.config.ts` was checked on a production build (`next start`); two Playwright specs ran in real Chromium against the dev server. **No real screen reader, zoom, device, CDN, managed client or axe colour-contrast run was used.**
Backend suite: 1516 tests passing with `EXAMS_ENABLED=false` (1423 before); `ruff check .` clean; frontend `tsc` and `eslint` clean, Vitest 237 passing in 35 files (193 / 28 before; `--maxWorkers=2 --testTimeout=20000`).

| ID | Status after P10 | What changed | Evidence |
|---|---|---|---|
| GRD-01 | EXISTS_AND_VERIFIED (D-8 open for policy) | Exact semantics written and enforced: multiple select graded as an exact set (was always wrong), short-answer score bounded, latest defective resolution wins, `presented` needs a started attempt, integrity label follows the block threshold; counts always add up to `assigned` | `test_judge_choice_grading_p10.py`, `test_gradebook_routes_p10.py` (counts, multiple select, defective order, score range), `test_gradebook_aggregation_service.py` |
| GRD-02 | EXISTS_AND_VERIFIED (was defective) | Students are served the release's **frozen** rows; v2+ needs a reason and is audited; MCQ / short-answer / coding regrades stay new versions (short-answer re-review now keeps `history`) | `test_a_regrade_after_release_*`, `test_a_defective_question_fix_reaches_students_only_with_a_new_release`, `test_gradebook_release_service.py` (24) |
| GRD-03 | EXISTS_AND_VERIFIED (real JPlag unqualified) | Unchanged; an HTTP test shows a stored 99% match changes no score and appears in no student response | `test_similarity_evidence_never_changes_a_score_or_reaches_a_student` |
| GRD-04 | EXISTS_AND_VERIFIED (was defective) | Content expires (`EXAM_EXPORT_RETENTION_HOURS`) and is swept; an expired job refuses tickets and downloads; CSV neutralises 15 prefix variants; tickets bound to the caller and single-use; scoped by run owner. Residual: in-memory content when no bucket | `test_gradebook_export_p10.py`, `test_an_export_*` (3) |
| GRD-05 | EXISTS_AND_VERIFIED | Results, feedback and disputes denied before a release; own row only | `test_a_student_gets_nothing_before_a_release_*` |
| GRD-06 | EXISTS_AND_VERIFIED (was defective for grade disputes) | Disputes need a release and a question in the student's row, one open per question, bounded, race-safe, audited; proctoring appeals unchanged | `test_grade_appeals_*`, `test_only_the_runs_owner_can_resolve_a_grade_appeal`, release-service tests |
| GRD-07 | EXISTS_AND_VERIFIED | Every gradebook route denied to a teacher who does not own the run (`scope_denied`), exercised with the real scope dependencies | `test_a_teacher_who_does_not_own_the_run_is_denied_every_gradebook_route` (9) |
| PWA-01 | EXISTS_AND_VERIFIED (CDN UNVERIFIED) | `Cache-Control: private, no-store` for `/exams/:path*` (verified on a production build, static and dynamic pages); the shipped worker never intercepts API / page requests, never writes a runtime cache, holds exactly the 4 static files through a session / account switch / logout; offline shell shown on a failed navigation | `examHeaders.test.ts`, `examServiceWorker.test.ts` (18 cases), `exams-pwa.spec.ts`, `exams-pwa-cache.spec.ts` |
| PWA-02 | EXISTS_AND_VERIFIED (was defective) | One module-level attempt lock: a layout instance can no longer release a waiting update or reload during an attempt; unknown (loading) and blocked count as active; the last release lets the update through | `useExamServiceWorker.test.ts` (4 new; they fail on the old hook) |
| A11Y-01 | PARTIALLY_EXISTS (automation only) | axe in jsdom on the exam page (short answer, single, multiple select, time-up), student results, blocked page and the faculty gradebook; the helper is proven to detect violations; keyboard-only multiple select to Submit; named controls; polite live warnings; theme contrast tokens (existing `examTheme.test.ts`); camera waiver accommodation (existing). **Not checked:** colour contrast computed by axe, zoom / reflow, screen-reader output, real devices | `page.a11y.test.tsx` (3 files), `axe.test.tsx`, `page.release.test.tsx` |

Defects found in the existing code while implementing:

| # | Severity | Defect | Fix |
|---|---|---|---|
| P10-1 | High | Students saw the live gradebook after any release; regrades leaked instantly, unreleased and unaudited | Frozen per-release rows, reasoned and audited corrections |
| P10-2 | High | Multiple-select answers were always graded wrong | Exact-set comparison (applies to newly finalized attempts) |
| P10-3 | High | The layout's service-worker hook released a waiting update, and reloaded the page, during an attempt | Shared attempt lock; loading / blocked held |
| P10-4 | Medium | Exports never expired | Retention + sweep + refusal after expiry |
| P10-5 | Medium | CSV only neutralised a first `= + - @` | TAB, CR, LF, look-alikes, leading whitespace / zero-width |
| P10-6 | Medium | Never-started students counted as presented; defective resolution chosen by store order; short-answer score above the maximum | Fixed with tests |
| P10-7 | Medium | Grade disputes: before release, any question id, unlimited, racing; a re-review overwrote the earlier score | Guards, derived ids, `history` |
| P10-8 | Low | Integrity label "blocked upheld" for any counted incident | Threshold- and review-based |
| P10-9 | Low | Student results showed grading provenance; faculty results page replaced itself with an error on a refused release; unlabeled inputs | Student view, inline errors with acknowledge / reason flow, labels |
| P10-10 | Low | No framework-level `no-store` for exam pages | `next.config.ts` |

Residual: real screen-reader / zoom / device / contrast evidence; CDN and managed-client (lockdown) profile; D-8 (partial credit, negative marking, official count definitions);
no append-only history for grade disputes; export content in process memory without a bucket; orphan snapshot rows when two releases collide; `next.config.ts` carries an unrelated, uncommitted `turbopack.root` change of the user.

## 11. Staging, release readiness, operations — Prompt 11

| ID | Requirement | Current implementation | Evidence / tests | Status | Smallest safe delta | Phase |
|---|---|---|---|---|---|---|
| OPS-01 | CI runs HireLog regression + exam suites | `ci.yml` (backend ruff/pytest/compileall, eval gate, pip-audit(informational)/bandit/npm audit, frontend lint/type/test/build, e2e smoke) triggers on PR / push-to-`main` only; **branch history unrelated to `origin/main`**; 11 smoke tests depend on local `.env` | baseline §2 | EXISTS_BUT_DEFECTIVE | Rebase onto `origin/main` (D-10); pin `EXAMS_ENABLED=false` in tests | P11 |
| OPS-02 | Staging qualification (concurrency, p95/p99, burst) | Only a local-emulator admission load test (60 users, loopback) | `production-qualification-report.md` §3 | MISSING (real staging) | Staging run | P11 |
| OPS-03 | Failure injection (disconnect, restart, outages) | Mapped to unit tests; no real outage rehearsal | qualification report §5 | PARTIALLY_EXISTS | Staging chaos pass | P11 |
| OPS-04 | Rollback: disable new admissions while preserving completion; never delete data | `EXAM_NEW_ADMISSIONS_PAUSED`, per-run pause, `EXAMS_ENABLED` | `test_exams_student_service.py` (4), `rollback-runbook.md` | EXISTS_AND_VERIFIED | — | — |
| OPS-05 | Secrets/config checklist, alerts, runbooks | `.env.example`, `deployment-guide.md`, `rollback-runbook.md`, `backup-dr.md`; alert thresholds are proposals — **no metrics/alert instrumentation** | docs | PARTIALLY_EXISTS | Instrument outbox age, judge retries, finalization lag | P11 |
| OPS-06 | Backup restore with measured RPO/RTO | `docs/runbooks/backup-dr.md` (procedure); no measured restore | — | MISSING (measurement) | Isolated restore drill | P11 |
| OPS-07 | Dependency vulnerabilities | `next` 16.2.10 (2 critical), `websocket-driver`, firebase/grpc/nanoid/source-map-js highs; backend `starlette`, `transformers`, … | `npm audit` 2026-10-09 | EXISTS_BUT_DEFECTIVE | `next ≥ 16.3.8` upgrade pass (separate decision) | P11 |

### 11.1 Status after the Prompt 11 implementation

Evidence and the release recommendation are in [production-qualification-report.md](production-qualification-report.md) (rewritten this phase). **No staging environment, real Firebase project, container runtime or load generator exists in this workspace**;
everything measured came from a local Firestore emulator on one laptop and is labelled that way. Backend 1529 tests (1516 before), frontend 237, build, legacy search gate and three Playwright specs pass.

| ID | Status after P11 | What changed | Evidence |
|---|---|---|---|
| OPS-01 | PARTIALLY_EXISTS (D-10 open) | `conftest.py` pins `EXAMS_ENABLED=false` (was: 11 tests failed on a machine whose `.env` enables the module). The branch still has **no merge base with `origin/main`** and CI triggers only on PR / push to `main`, so CI has never run this work; Python 3.11 (production, CI) vs 3.12 (local) parity untested | full suite with the variable unset and `.env` = true |
| OPS-02 | MISSING (real staging) — local emulator evidence added | Synchronized 300-student submit burst (admit, drafts, barrier submit, lost-response retry): 0 errors, exactly-once receipts and outbox entries, queue age p50 19 s / p95 31 s; latencies are single-worker laptop saturation, **not capacity** | report §3 |
| OPS-03 | PARTIALLY_EXISTS | Live API kill / restart during a burst (150 students): 150/150 submitted once, receipts identical, outbox 1:1. **Defect found and fixed:** contention exhaustion and datastore errors were bare 500s, now retryable 503 (`temporarily_unavailable`, `Retry-After`). Database outage covered by tests only; no real outage rehearsal | `test_ops_and_outage_p11.py`, report §4 |
| OPS-04 | EXISTS_AND_VERIFIED | Rehearsed live: with `EXAM_NEW_ADMISSIONS_PAUSED=true` an admitted student still reads state, saves and submits (receipt); a new student gets 403 `new_admissions_paused`; clearing the flag restores admission | report §4 |
| OPS-05 | PARTIALLY_EXISTS | `GET /api/exams/v1/ops/queue-health` (super-admin, counts only): outbox backlog / age / dead entries, judge waiting / retrying / exhausted, attempts overdue for sealing; alert thresholds written against it. **No alerting platform is wired**; skipped-grading outbox entries are not counted (finding) | `test_queue_health_*`, report §10 |
| OPS-06 | MISSING (measurement) | Not done: no backup, no real project, the emulator offers no export | report §12 |
| OPS-07 | EXISTS_BUT_DEFECTIVE → improved | `next` 16.2.10 → 16.4.0 clears both **critical** advisories (frontend audit 11 → 6, 0 critical); lockfile updates for `websocket-driver`, `nanoid`, `source-map-js`. Open: `firebase` / `grpc-js` highs (only offer is a major downgrade), `postcss` and one more moderate; backend `starlette` 0.45.3 and `python-dotenv` 1.0.1 (a FastAPI 0.143 / Starlette 1.7 trial passed 1515 of 1529 tests — only the 14 route-introspection tripwires fail) | report §5 |
| AUTH-08 (rules) / ING-08 (egress) / PRC-07 (IAM) | MISSING (real environment) | Not run: rules emulator (`firebase-tools` absent), sandbox egress (no real judge, D-5), real bucket / IAM | report §14 |

Defects found while implementing: P11-1 .. P11-5 in the report (§4).

## 12. Master-prompt invariants (cross-reference)

| # | Invariant | Status | Rows |
|---|---|---|---|
| 1 | Server authorizes every protected read/write | PARTIALLY_EXISTS (baseline) → HTTP routes closed in Prompt 1, see §1.1; WebSocket stream and `check_revoked` remain | AUTH-05, AUTH-06, AUTH-09 |
| 2 | College ID is an identifier; no public role escalation | EXISTS_AND_VERIFIED | AUTH-03, AUTH-07, ID-02 |
| 3 | Server time controls admission/deadline/extension/close/finalization | EXISTS_AND_VERIFIED (unit; sweeper on by default, see ADR 0019) | TMR-01, TMR-02 |
| 4 | Published revisions and final submissions immutable; draft ≠ submit | EXISTS_AND_VERIFIED (DOM-07 partial) | DOM-07, DOM-08, ATT-03/04 |
| 5 | Locked content never reaches the student early | EXISTS_AND_VERIFIED | ATT-01, ATT-08 |
| 6 | AI items stay drafts until checks + teacher approval | PARTIALLY_EXISTS | GEN-05, GEN-08 |
| 7 | Uploaded/fetched content is untrusted data | PARTIALLY_EXISTS | ING-09, GEN-11 |
| 8 | Student code never executes in the API process; SQL never touches production | EXISTS_AND_VERIFIED | JDG-01, JDG-03 |
| 9 | Durable jobs/outbox restart-safe and idempotent | PARTIALLY_EXISTS | DOM-12, TMR-02 |
| 10 | Private data not public/cacheable | PARTIALLY_EXISTS | ATT-07, PWA-01 |
| 11 | Proctoring signals not proof; recovery/appeal preserved | EXISTS_AND_VERIFIED | PRC-03..06 |
| 12 | Flags default off for new behavior | EXISTS_AND_VERIFIED | AUTH-11 |
| 13 | Preserve legacy HireLog and working exam flows | EXISTS_AND_VERIFIED | AUTH-10 |
| 14 | No overclaiming of mocks | EXISTS_AND_VERIFIED | baseline §6 |
| 15 | Synthetic fixtures; no secrets in artifacts | EXISTS_AND_VERIFIED | baseline §1 |

## 13. Backend model prompts

| Prompt | Where implemented | Status |
|---|---|---|
| A — structured question generation | `generation/provider.py` (`GENERATION_SYSTEM_PROMPT_A_V2` default, V1 retained; `DeterministicFakeProvider`, `HttpGenerationProviderAdapter`), `FamilyConstraints` on `BlueprintSlot`, provider notes on `GenerationCandidate`, `generation/validation.py` | PARTIALLY_EXISTS — v2 prompt, family constraints (API only, no faculty UI) and reviewer notes done (compat §17); SQL items unsupported (D-5); no real provider exercised; see GEN-03, GEN-05, GEN-07 |
| B — independent review | `generation/independent_review*.py` | EXISTS_AND_VERIFIED (GEN-06) |
| C — learning feedback | `gradebook/feedback_*.py` | EXISTS_AND_VERIFIED (GEN-10) |

## 14. Decisions required (referenced above)

| # | Decision | Needed by |
|---|---|---|
| D-1 | Final bootstrap super-admin list: the master prompt names three addresses, Prompt 1's text says "four" (the v1.0 fourth was removed) | P1 deployment config |
| D-2 | **Resolved in Prompt 2: "definition = course"** (co-instructors on an exam definition, assigned by a super-admin or the institution's ADMIN). Still open: finer capabilities inside a course (e.g. a proctor-only assistant) and any separate Course entity | P2 (done) |
| D-3 | Slot-release / navigation policy: v1.0 contract = forward-only sequential release; v1.1 requires Previous/Next + navigator over released slots. **Prompt 6 delivered the mechanism behind `EXAM_STUDENT_REVISIT_RELEASED_SLOTS` (default off = forward-only); the institution still chooses** | P6 (mechanism, done) / P7 (UI) |
| D-4 | Malware scanner, private object storage and retention for uploads. **Prompt 4 delivered the clamd adapter, the storage seam and teacher delete; still open: provisioning a real ClamAV and bucket, and a retention window for uploaded sources (only evidence has a retention sweeper)** | P4 (adapter, done) / deployment |
| D-5 | Judge architecture/provider approval, language set (C, JS/TS), SQL runner qualification | P8 |
| D-6 | Institution SSO vs verified-email OTP; OTP delivery provider. Prompt 2 delivered the adapter contract, the selection setting and the fail-closed production behavior; **no real provider exists until this is decided** | P2 (provider) |
| D-7 | Institution proctoring profile contents (thresholds, evidence policy, retention) | P9 |
| D-8 | Negative marking and multi-select partial-credit semantics; official grade-count definitions. **Prompt 3 delivered the schema, validation, preview and a default-off flag only; grading still does not apply negative marks** | P3 (schema, done) / P10 (scoring) |
| D-9 | Short-link domain, code format, expiry and rate-limit policy | P7 |
| D-10 | History strategy for CI (rebase exam branch onto `origin/main` or add a branch trigger) | P11 |
| D-11 | Legacy `PLACEMENT_CELL_EMAILS` promotion: verified-email/Google-only policy | outside exams |
| D-12 | Per-student pause/resume: does the student's clock freeze (deadline shifts by the paused time), who may do it, and how it composes with the proctoring `BLOCKED` state. Not implemented in Prompt 3 | P7/P9 |
| D-13 | Which generation provider / gateway the institution runs, and a separate one for the independent review (Prompt 5 delivered a provider-neutral https adapter tested against a mock transport only) | P5 (adapter, done) / deployment |
