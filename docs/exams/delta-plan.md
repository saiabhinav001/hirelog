# Hirelog Exams — phased delta plan (confirmed gaps only)

Prompt 0 (v1.1), audited 2026-10-09 at `5b0ad3b`. Every item below traces to a row classified `EXISTS_BUT_DEFECTIVE`, `PARTIALLY_EXISTS`,
`MISSING` or `BLOCKED_BY_DECISION` in [requirements-traceability.md](requirements-traceability.md); rows classified `EXISTS_AND_VERIFIED` are
**not** reworked (regression coverage only where listed). Contracts that must not move: [compatibility-and-migration.md](compatibility-and-migration.md).
Wiring and callers: [integration-map.md](integration-map.md).

**Plan rules.** Reuse existing routes, services, collections, queue (`exam_outbox`), realtime channel and design tokens. New behavior is flagged or
additive; secure-by-default only for the flag-gated, not-yet-onboarded exam module. Each phase ends with the mandatory post-phase compatibility check.

**Planned duplicates: none.** No second endpoint family, role store, auth system, queue/worker, realtime path, deployment or design system is
planned. The only brand-new persisted concepts (Prompt 1) have no existing home: staff-binding records and an authorization audit log (ADR 0017).

## 1. Phase summary

| Phase | Confirmed gaps (IDs) | Smallest safe delta | New flag / config | Primary risk |
|---|---|---|---|---|
| **1 — authorization & roles** | AUTH-01/02/04/05/06/09, LIV-02, GRD-07, JDG-12, ATT-02 (membership), tests for AUTH-03/07/08/10/11 | Pure role policy + verified-email staff bootstrap with UID binding/audit; membership freshness in `get_current_exam_user`; owner/institution scope dependencies on every faculty route; student run scope on every student route; optional `institution_id` on definition/run | `EXAM_SUPER_ADMIN_EMAILS`, `EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL` (dev only) | Behavior tightening for pilots (see migration notes) |
| 2 — roster / linking / super-admin management **(implemented — see §5; the real OTP provider still waits on D-6)** | ID-03 (CSV upload), ID-05/06/08 (delivery, audit, recovery), ID-07, ID-09, AUTH-13 | `multipart` CSV onto existing dry-run/apply; real OTP delivery adapter behind `OtpDelivery`; persisted link-conflict audit; teacher assignment API after D-2 | provider env vars | Decisions D-2, D-6 |
| 3 — domain, policy, state machines **(implemented — see §6)** | **DOM-12/TMR-02 (finalization sweeper never started — High)**, DOM-03/04/07/09/11, TMR-03, SCH-01, MRK-01 | Start `ExamFinalizationSweeper` from the existing `lifespan` loop pattern; actor/reason on commands and extensions; optional policy/blueprint fields; preview summary; publish validation; per-attempt commands | `EXAM_FINALIZATION_SWEEP_ENABLED` (default **on** — the missing sweep is a defect), policy fields optional | `extra="forbid"` on `ExamPolicy` affects rollback (compat §4) |
| 4 — ingestion | ING-01/02/04/05/06/07 | Private object storage via the existing `evidence_storage` adapter pattern; scanner adapter honouring `EXAM_MALWARE_SCANNER_ENDPOINT`; parse as an outbox job; extracted-text hash; retry/replace/delete routes | storage / scanner env vars | Decision D-4 |
| 5 — blueprint, generation, review | GEN-01/02/03/05/07/08/09/11, DOM-05, RND-01 | Resolve **real** anchor text and reject unknown anchors (today placeholder excerpts); capacity summary + publish gate; answer-key / near-duplicate / option-shuffle validation; candidate edit → new version + approval reset; injection fixtures | optional blueprint fields | Highest correctness gap in the pipeline |
| 6 — attempts, drafts, immutable submit | ATT-02 (slot validation, previous access), ATT-06, ATT-08, UI-03/05 backend | Validate `question_id` against released slot; read access to released slots; mark-for-review flag on drafts; emulator interleaving tests | none | Decision D-3 |
| 7 — timer, navigator, themes, control room | UI-01/02/03/04/05/11/12, SHR-01, LIV-01 freshness | Ticking server-offset timer; threshold warnings; navigator + counts; short link; missing teacher controls | policy thresholds | Decisions D-3, D-9 |
| 8 — judge | JDG-01/02/03/05/07 | Exhausted-retry state; language registry extension after provider validation; real adapter **only** after architecture approval | provider env vars | Decision D-5; infra evidence |
| 9 — proctoring | PRC-01, PRC-08 (test), JDG-14 note | Per-run grace/cooldown if D-7 requires; explicit evidence-failure-vs-submit test | — | Decision D-7 |
| 10 — gradebook, a11y, PWA | GRD-01, PWA-01, A11Y-01, UI-07/08 | `Cache-Control: private, no-store` for `/exams/:path*`; axe/keyboard/contrast on exam pages; scoring semantics doc | — | Decision D-8 |
| 11 — staging & release | OPS-01/02/03/05/06/07, AUTH-08 (emulator), ING-08 (egress), PRC-07 (IAM) | CI history (D-10), `conftest` pins `EXAMS_ENABLED=false`, staging load/chaos/restore drills, rules emulator suite, `next` upgrade pass | — | Needs real staging |

Suggested order after Phase 1: **3 (sweeper) → 5 (real grounding) → 6/7 → 4 → 8–11**; Phase 4 waits for D-4. Phase 2 was done out of order once D-2 was answered; its
remaining item, a real OTP provider, waits on D-6.

## 2. Phase 1 — file-by-file plan

| File | Change |
|---|---|
| `backend/app/core/config.py` | `EXAM_SUPER_ADMIN_EMAILS` (default empty; **no address in source**), `EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL` (default false; ignored with an error log when `ENV=production`), `exam_super_admin_emails_set()` / `unverified_staff_email_allowed()` helpers |
| `backend/.env.example` | Document both variables and the migration note |
| `backend/app/api/dependencies.py` | `get_current_user_uncached` also returns `email_verified` and `sign_in_provider` from the verified token (legacy `get_current_user` untouched) |
| `backend/app/domain/authz/policy.py` (new) | `ExamRole` (`student`/`teacher`/`super_admin`), `ExamPrincipal`, pure functions: staff-grant resolution (verified allowlists), role mapping from `RoleScope`, owner/institution/super-admin resource scope, student institution scope, token authz-version freshness, creation-scope resolution |
| `backend/app/domain/authz/staff_binding.py` (new) | `exam_staff_bindings/{sha256(email)}` create-if-absent binding of a verified allowlisted email to its first UID; conflict ⇒ deny + audit; `exam_authz_audit` append-only events |
| `backend/app/domain/exams/models.py`, `commands.py` | Optional `institution_id` on `ExamDefinition` and `ExamRun` (run copies the definition's value server-side) |
| `backend/app/api/exams_dependencies.py` | `get_current_exam_user` enforces membership freshness when the token carries `exam_institution_id`; `resolve_principal`; `require_exam_faculty` (teacher or super-admin, verified, bound) keeps its name and `user` return value; `require_exam_super_admin`; `require_student_run_scope`; `require_seb_admission` composes the student scope |
| `backend/app/api/exams_scope.py` (new) | Dependencies/resolvers: definition, run, blueprint, candidate, generation request, source/upload, submission, short-answer review, grade appeal, proctoring appeal, export job, snapshot, institution |
| Routes | `exams_faculty`, `exams_generation`, `exams_kt`, `exams_gradebook`, `exams_judge`, `exams_proctoring`, `exams_lockdown`: swap `Depends(require_exam_faculty)` for the matching scope dependency (handler bodies unchanged) and inline checks where the id is in the body; `exams_institution_admin`: super-admin-only create, institution-scoped roster/revoke; `exams_live`: ticket role from the principal + run scope; `exams_student`, `exams_judge`/`exams_proctoring`/`exams_gradebook` student routes: `require_student_run_scope`; `exams_me`: additive `exam_roles` / `is_super_admin` / `email_verified` |
| Tests | New: policy, staff binding, dependencies (freshness/verified/legacy), scope matrix, route-guard graph, profile + rules static checks. Intentionally updated: one fixture in `test_institution_reconciliation.py` (`test_require_exam_faculty_still_accepts_bootstrap_email_allowlist`) |
| Docs | ADR 0017, `docs/adr/README.md`, migration notes in `compatibility-and-migration.md §6`, status update in the traceability matrix |

Out of Phase 1 on purpose: course entity (D-2), CSV upload / OTP delivery / recovery (Phase 2), legacy `PLACEMENT_CELL_EMAILS` (D-11),
`check_revoked` (documented residual risk), sweeper and command audit (Phase 3), any frontend change.

## 3. Phase 1 acceptance checklist

Status after implementation (evidence is unit-level with in-memory doubles; see [requirements-traceability.md §1.1](requirements-traceability.md#11-status-after-the-prompt-1-implementation)).

- [x] Allowlisted staff authority requires `email_verified`; unverified token ⇒ no teacher/super-admin role (tested, incl. body-spoofed email).
- [x] No super-admin e-mail exists in tracked source; the allowlist is configuration only (checked by `git grep` and a scan of the new files for the pack's addresses).
- [x] First verified sign-in binds email→uid with an audit event; same uid again is a no-op; a different uid is denied and audited; removing the e-mail from the allowlist removes authority immediately. *(Logic tested; concurrent-writer atomicity of `create()` needs an emulator.)*
- [x] A revoked or version-bumped membership is rejected on the next request even with a valid ID token (student and staff, every exam route).
- [x] Every `/api/exams/v1/**` route is covered by the route-guard graph test (identity, flag, and the appropriate scope dependency, or an explicit documented exception).
- [x] A teacher cannot read or mutate another teacher's definition/run/blueprint/candidate/source/gradebook/appeal/export/incident/evidence/lockdown/live-ticket; super-admin and same-institution ADMIN can.
- [x] Only super-admins create institutions; roster import/revoke require super-admin or ADMIN of that institution (FACULTY may import STUDENT rows only).
- [x] Student routes require enrollment **and** (when the run has an `institution_id`) an ACTIVE student membership in it.
- [x] Legacy users (no memberships) still sign in and use legacy workflows; `/me` returns an empty, error-free capability set. *(Unit-level; no live sign-in was driven.)*
- [x] `firestore.rules` static regression test passes; API authorization tests do not depend on rules.
- [x] Full backend suite (`EXAMS_ENABLED=false`: 627 passed) and ruff pass; frontend suites unaffected (no frontend file changed by this phase).
- [x] Migration notes and ADR 0017 written; no production data touched; no secrets added.

## 4. Global acceptance checklist (every phase)

- [ ] Delta limited to the rows of that phase; verified rows only gain regression tests.
- [ ] No duplicate endpoint, role/membership store, collection, queue/worker, realtime mechanism or design system (compare against [integration-map.md](integration-map.md)).
- [ ] No breaking API/schema change without a documented compatibility migration (compat doc §4).
- [ ] No secrets, answers, hidden tests or private evidence in logs/caches.
- [ ] Existing and new tests run with failures reported honestly; mocks never described as proof of sandbox security, load capacity or lockdown.
- [ ] Decisions in traceability §14 recorded as ADRs when taken.

## 5. Phase 2 — roster, account linking, super-admin management (implemented, ADR 0018)

Phase 2 was gated on decisions D-2 and D-6 (§1). D-2 was answered ("definition = course"); D-6 was not, so only the adapter contract and the fail-closed
selection were built, which is exactly what the prompt allows. Statuses per requirement: [requirements-traceability.md §2.1](requirements-traceability.md#21-status-after-the-prompt-2-implementation).

| File | Change |
|---|---|
| `backend/app/domain/institution/models.py` | `IdentityKeyPolicy` and optional `Institution.identity_policy`; optional `RosterRowResult.first_seen_row` / `line_number`; optional `RosterImportJob.rows_digest` |
| `backend/app/domain/institution/normalization.py` | Optional policy on `normalize_identity_key` (fixed point of the universal step); `identity_key_problem`; `is_valid_contact_email` |
| `backend/app/domain/institution/roster_csv.py` (new) | Pure CSV parser: limits, strict columns, row-level `RosterParseError` with line numbers |
| `backend/app/domain/institution/roster_import.py` | Whole-file structural pass; stored-state rules (revoked, outranked, e-mail change, `skip_unchanged`); `rows_digest`; `batch_identity_keys`; `classify_row` |
| `backend/app/domain/institution/roster_service.py` | Digest-bound resumable apply, atomic student create, transactional re-check + audit for role/e-mail changes, batch audit, `assign_teacher` (one-row roster rules) |
| `backend/app/domain/institution/repository.py` | `get_many`, `create_if_absent`, `create_in_transaction`, `list_all`, transactional OTP access; roster jobs written without nulls |
| `backend/app/domain/institution/identity_provider.py`, `core/config.py`, `.env.example` | `EXAM_OTP_DELIVERY_PROVIDER`; `UnconfiguredOtpDelivery`; `build_otp_delivery`; dev sink never built in production |
| `backend/app/domain/institution/auth_service.py` | Policy-normalized keys; uniform 503 when OTP is unavailable; delivery failures swallowed; atomic OTP consume; transactional audited link with two conflict types; `revoke` / `reinstate` / `unlink` with role check, version bump and audit |
| `backend/app/domain/institution/errors.py` | `AccountAlreadyLinkedError`, `RosterRowsChangedError`, `RosterRowRejectedError` |
| `backend/app/domain/authz/staff_binding.py`, `policy.py` | Audit event gains `actor_uid` / `institution_id`, builder and transactional append (same collection); `max_grantable_role_scope` (super-admin grants FACULTY/ADMIN), `can_administer_institution`, `co_owner_uids` |
| `backend/app/domain/exams/models.py`, `repository.py`, `institution_enrollment.py` | `ExamDefinition.instructor_uids`; `list_by_instructor`, transactional setter; `assign_instructors` |
| `backend/app/api/exams_scope.py` | Co-instructors in the definition ownership chain; `authorize_instructor_assignment` / `require_instructor_admin_scope` |
| `backend/app/api/routes/exams_institution_admin.py` | JSON-or-CSV body on dry-run/apply (scope check first), list/read routes, reinstate, unlink, teacher assignment, recent sign-in on authority-changing actions |
| `backend/app/api/routes/exams_institution_auth.py`, `exams_faculty.py` | Structured link-conflict `409`; co-instructor listing and `GET`/`PUT …/instructors` |
| Tests | New: CSV, planner, roster service, admin routes (HTTP), co-instructors; extended: auth service. Intentionally updated: `max_grantable_role_scope` matrix, scope-test fixtures and roster matrix, route-guard mapping, auth-service fixtures |
| Docs | ADR 0018 (+ index, amendment note on ADR 0017), compat §7, traceability §2.1, integration map |

Out of Phase 2 on purpose: any frontend/UI (Prompt 7), a real OTP provider or SSO adapter (D-6), a separate Course entity or finer in-course capabilities,
changing an institution's identity policy after creation, emulator-backed transaction tests (Phase 11).

### Phase 2 acceptance checklist

- [x] CSV dry-run and bounded, resumable import on the existing routes; leading zeros preserved; row-level errors (file problems rejected with a code).
- [x] Per-institution normalization (immutable policy); duplicate and formatting-collision detection across the whole file, including across batches.
- [x] Import replay is a no-op; apply is bound to the validated rows; an import never deletes, deactivates or reactivates a member and never outranks its uploader.
- [x] Unauthorized role grants refused (only a super-admin grants FACULTY/ADMIN); contact-e-mail changes need ADMIN-level authority.
- [x] OTP: short-lived, single-use (one transaction), attempt-capped, rate-limited, HMAC-protected, delivered only through a configured adapter; unavailable (uniform 503) in production without one; never described as MFA. *(No real provider: D-6.)*
- [x] Account linking proves both identities, preserves the uid, refuses and audits both conflict types; `unlink` / `reinstate` / `revoke` recover with audit.
- [x] Teacher assignment by a super-admin; co-instructors on definitions ("definition = course") by a super-admin or the institution's ADMIN.
- [x] Revoked membership, expired/replayed codes, rate limits and legacy accounts without exam membership are tested.
- [x] Full backend suite (`EXAMS_ENABLED=false`: 774 passed) and ruff pass; frontend not changed.
- [x] Migration notes and ADR 0018 written; no production data touched; no secrets or addresses added. *(Emulator, concurrent-writer and real-provider behavior not verified.)*

## 6. Phase 3 — domain, policy, commands, finalization (implemented, ADR 0019)

Phase 3 was gated on decision D-8 only for scoring: the schema, validation and a default-off flag shipped, grading semantics did not. Per-student pause/resume
needed a new decision (D-12) and was not guessed.

| Area | Change |
|---|---|
| `backend/app/domain/exams/finalization.py`, `backend/app/exam_main.py` | The finalization sweeper is started from `lifespan` (flag `EXAM_FINALIZATION_SWEEP_ENABLED`, default **on**); a seal writes `submit_receipt`, `finalization_reason`, `finalized_by`, an `attempt_finalized` outbox row (grading trigger) and an audit event; `sweep_active_runs_once` covers released/paused/closed and recently ended runs; `run_schedule_automation_once` (flag `EXAM_SCHEDULE_AUTOMATION_ENABLED`, default **off**) releases/closes through the ordinary command path |
| `backend/app/domain/exams/commands.py`, `decisions.py`, `errors.py` | `actor_uid`/`reason` on commands and receipts; `grant_time_extension` is one transaction with `decide_time_extension`; new `terminate_attempt`, `recover_attempt`, `change_access`; blueprint-derived `publish_revision` with idempotency key; `preview_publication`; run creation audited; `CommandRejectedError`, `PublicationBlockedError` |
| `backend/app/domain/exams/publication.py` (new) | Pure preview/validation (counts, total marks, distributions, blockers, warnings) and the revision snapshot |
| `backend/app/domain/exams/policy.py`, `models.py`, `generation/models.py` | Additive optional fields: policy `timezone`, `scheduled_start/end`, `variant_policy`; `ExamRevision.blueprint_id/total_marks/slots/publish_reason` + `RevisionSlot`; `Attempt.finalization_reason/finalized_by`; `CommandReceipt.actor_uid/reason`; blueprint `negative_marks` |
| `backend/app/domain/exams/repository.py` | Create-only revision writes; receipts written without unset optional fields; `list_by_statuses`, `list_ended_since`, `set_enrolled_in_transaction`, `create_in_transaction` (single-field queries, no new composite index) |
| `backend/app/api/routes/exams_faculty.py`, `exams_generation.py`, `exams_errors.py` | New routes: `GET …/publish-preview`, `POST …/attempts/{uid}/terminate`, `…/recover`, `…/access/grant`, `…/access/revoke`, `…/finalize-expired`; extended bodies (`reason`, `idempotency_key`, `blueprint_id`); blueprint negative-marks gate; reserve-variants honours `variant_policy` |
| Config / deps | `EXAM_FINALIZATION_SWEEP_ENABLED`, `…_INTERVAL_SECONDS`, `…_ENDED_RUN_WINDOW_HOURS`, `EXAM_SCHEDULE_AUTOMATION_ENABLED`, `EXAM_NEGATIVE_MARKING_ENABLED`; `tzdata` pinned |
| Tests | New: `exam_inmemory.py` (optimistic in-memory Firestore), `test_exams_policy_validation.py`, `test_exams_run_commands.py`, `test_exams_attempt_commands.py`, `test_exams_faculty_routes_p3.py` |
| Docs | ADR 0019 (+ index), compat §8, traceability §3.1, integration map |

Out of Phase 3 on purpose: any frontend/UI, per-student pause/resume (D-12), applying negative marks in grading (D-8/P10), source-derived topic suggestions (DOM-05,
P5), randomization/option shuffle (P5), time-remaining thresholds (P7), real-Firestore/emulator interleaving evidence (P11).

### Phase 3 acceptance checklist

- [x] Invalid policies are rejected (duration/threshold/grace bounds, unknown or malformed timezone, naive or reversed schedule, unknown variant policy, unknown fields, accommodation cap, negative marks above marks).
- [x] Preview shows total marks, per-type counts, difficulty distribution, languages and blockers before publication; blockers refuse publication and write nothing.
- [x] Revisions are immutable and create-only; a published edit is a new revision; a run stays pinned; later candidate changes do not alter a published revision.
- [x] Illegal run transitions and stale versions are rejected without side effects; duplicate retries (sequential and concurrent) apply exactly once; concurrent commands at one version apply exactly one.
- [x] Extension/expiry race: an extension granted before the deadline protects the attempt, one after it (or after sealing) is rejected; threaded interleavings never produce "extended and sealed" or "neither".
- [x] Finalized/terminated attempts are never reopened; terminate and recover require a reason; grant/revoke never strands an attempt.
- [x] Closing a cohort preserves accommodated/extended deadlines (manual and scheduled).
- [x] Sweeper started, bounded, idempotent across concurrent sweepers; a seal produces a receipt and the grading trigger.
- [x] Every new route is bound to run/definition scope; every state change has actor, reason (where applicable) and an audit event written in the same transaction.
- [x] Full backend suite (`EXAMS_ENABLED=false`: 947 passed) and ruff pass; frontend not changed.
- [x] Migration notes and ADR 0019 written; no production data touched; no secrets or addresses added. *(Real Firestore/emulator transaction behavior, index needs at scale and live websocket fan-out from the sweeper were not verified.)*
