# Hirelog Exams — compatibility and migration constraints

Prompt 0 (v1.1), audited 2026-10-09 at `5b0ad3b`. Lists the contracts and persisted data that **must stay stable** while the delta plan in
[delta-plan.md](delta-plan.md) is executed, the change classes that are safe, and the migration/rollback expectations. Related:
[integration-map.md](integration-map.md), [requirements-traceability.md](requirements-traceability.md), [baseline.md](baseline.md).

Governing rule (master prompt): do not rename/move public APIs or change persisted schemas without a demonstrated need, a compatibility plan, a
migration dry run and a rollback plan; preserve existing data; never run destructive migrations or production changes without explicit authorization.

## 1. Stable API contracts

| Contract | Detail | Consumers |
|---|---|---|
| Path families | `/api/exams/v1/{faculty,student,auth,live}/…`, `/api/exams/v1/me`, plus `/`, `/health/live`, `/health/ready` — 88 exam routes at the audit, **96 after Prompt 2** (+8, see §7.3), + 3 root routes (see [integration-map.md §3](integration-map.md)) | `frontend/src/lib/examEndpoints.ts`, e2e, ops scripts (`backend/scripts/exam_*.py`) |
| Auth | `Authorization: Bearer <Firebase ID token>`; WebSocket uses a single-use ticket query param | frontend `getClientAuthToken`, `liveSocket.ts` |
| Disabled behavior | `EXAMS_ENABLED=false` → every exam route returns 503, **including unauthenticated callers** (router-level dependency) | `test_exams_routes_smoke.py`, deployment guide |
| Caching | Every exam API response is `Cache-Control: no-store` (route headers + app middleware) | PWA, CDN |
| `GET /api/exams/v1/me` | keys `uid`, `email`, `email_verified`, `is_bootstrap_faculty`, `is_super_admin`, `exam_roles`, `memberships[{institution_id, role_scope, status, authorization_version}]`, `has_any_exam_membership`; legacy user → empty list, never an error (`email_verified`, `is_super_admin`, `exam_roles` added in Prompt 1) | `frontend/src/lib/examAuth.tsx` (`ExamCapabilities`). **Additive keys only** |
| Error shapes | `HTTPException.detail` is either a string or `{code, message,…}`; status mapping lives in `api/exams_errors.py` (404 lookup, 403 not enrolled / not released / blocked, 409 conflicts, 401 invalid tickets). Conflict `code`s used by the UI/tests: `draft_version_conflict`, `attempt_blocked`, `run_not_released`, `new_admissions_paused`, `reauthentication_required`, `membership_not_active`, `stale_authorization_version` | frontend, tests |
| Idempotency | Run commands: `expected_version` + `idempotency_key`, receipt doc `{run}__{command}__{key}`; submit/finish: `idempotency_key` + `client_epoch` + `expected_draft_versions`, `SubmitReceipt.receipt_id` | clients, `decisions.py` |
| Payload casing | snake_case request/response keys (engineering standard) | frontend types mirror backend keys |
| Student DTOs | Allowlisted: `PublicQuestionRef`, `build_public_content`, `public_test_result_dto`, `sealed_grade_summary_dto` — private fields must never be added to them | tests with planted-marker assertions |

## 2. Stable persisted data

All collections are Admin-SDK-only (default-deny rules). **Collection names, document-id schemes and existing field names/types must not change.**

| Concern | Stable detail |
|---|---|
| Exam documents | `exam_definitions/{id}` (+`revisions/{id}`), `exam_runs/{id}` (+`attempts/{uid}` → +`drafts/{question_id}`), `exam_command_receipts/{run}__{command}__{key}`, `exam_outbox/{id}`; question id format `{revision_id}:slot:{index}` |
| Institution documents | `institution_memberships/{institution_id}::{NORMALIZED_IDENTITY_KEY}` (uniqueness constraint; identity keys are strings, never coerced to numbers); `schema_version` stamped on memberships and roster imports; `authorization_version` semantics (bumped on revoke) |
| Identity | Custom-token uids `exam-{uuid4}`; token claims `exam_authz_version`, `exam_institution_id`; linked accounts keep the legacy uid |
| Enumerations | `RunStatus`, `AttemptStatus`, `CandidateStatus`, `UploadStatus`, `ObservationKind`, `RoleScope`, `MembershipStatus`, `ItemType` string values are persisted — add, never rename/remove |
| Legacy | `users/{uid}` fields and rules (ADR 0007), `interview_experiences`, `practice_lists`, `metadata`; `users.role` ∈ `viewer/contributor/placement_cell` and its semantics; **exam authority must never be stored on `users/{uid}`** |
| Indexes | No composite indexes exist for exam collections; new queries must be single-field equality (or ship an index in `firestore.indexes.json`) |
| Pydantic strictness | `ExamPolicy`, `BlueprintSlot`, `RosterRowInput` use `extra="forbid"`. Adding a field to those models means **documents written by new code cannot be read by old code** → rollback of such a change needs a data note (see §4). Other stored models ignore unknown fields, so additive optional fields are rollback-safe |
| Outbox | `kind` values `run_command`, `attempt_finalized`; handlers must stay idempotent (at-least-once) |

## 3. Stable configuration

Existing env names/defaults in [integration-map.md §8](integration-map.md) must not be renamed. New behavior ships behind a new setting whose default
reproduces current behavior **unless** the change is a security fix to a module that is itself flag-gated and not yet in production use
(`EXAMS_ENABLED=false` by default) — in that case the secure behavior is the default and the migration note lists the configuration to set.
Frontend: `NEXT_PUBLIC_EXAMS_ENABLED`, `NEXT_PUBLIC_EXAMS_API_BASE_URL`, CSP `connect-src` for the exam origin, SW scope/manifest `/exams/`.

## 4. Change classes and rollback expectations

| Class | Examples | Dry run | Rollback |
|---|---|---|---|
| Additive optional field on a non-strict model | `ExamDefinition.institution_id`, `ExamRun.institution_id` | none needed (no backfill; legacy documents read as `None`) | old code ignores the field |
| Additive field on a strict model | future `ExamPolicy.negative_marking`, schedule/timezone | list affected runs read-only | keep old runs readable: write the field only when set; roll back by redeploying new code (not old) or by a one-off cleanup after authorization |
| New collection | `exam_staff_bindings`, `exam_authz_audit` | n/a | stop writing; data is inert; never delete without authorization |
| New route | summary/preview, per-attempt commands | n/a | remove route; no data impact |
| Behavior tightening (authorization) | verified-email requirement, resource scope, membership freshness | run the new tests + read-only `git grep` of callers; communicate required config | revert the commit; data unaffected |
| Background loop | finalization sweeper | run `sweep_once` against a non-production project first | stop the loop (flag); the sweeper only seals attempts past their own deadline, re-checked per write |
| Rename / move / removal of routes or fields | **not planned** | — | — |

Global rollback levers already available: per-run `pause`, `EXAM_NEW_ADMISSIONS_PAUSED`, `EXAMS_ENABLED=false` ([rollback-runbook.md](rollback-runbook.md)).
**No rollback procedure may delete exam data.**

## 5. Compatibility hazards found during the audit

| Hazard | Why it matters | Handling |
|---|---|---|
| Tests assume `EXAMS_ENABLED=false` but read the developer's gitignored `.env` | Phase work reports false failures | Run suites with `$env:EXAMS_ENABLED='false'`; fix isolation in P11 (`conftest`) |
| `require_exam_faculty` is unit-tested with user dicts lacking `email_verified` (`test_institution_reconciliation.py`) | Verified-email enforcement changes the contract those tests encode | **Done in P1:** one fixture (`test_require_exam_faculty_still_accepts_bootstrap_email_allowlist`) now supplies `token_email` + `email_verified` and patches the binding service; the membership-based tests were unaffected |
| Branch history unrelated to `origin/main` | CI/PR cannot compare; a later rebase rewrites SHAs | Decide D-10 before opening a PR; never force-push without authorization |
| Exam API image ships only `requirements-exams.txt` | New server code must not import legacy/ML dependencies | Imports in new modules limited to stdlib, FastAPI, pydantic, firebase-admin, google-api-core (already transitive) |
| In-memory per-process state (`live_connection_manager`, fake judge singleton, `InMemoryEvidenceStorage`, SlidingWindowLimiter fallback) | Multi-instance deployments behave differently | Out of scope for P1; tracked under P11 |
| Operators using `EXAM_FACULTY_EMAILS` for institution administration via API | P1 narrows admin authority to super-admins / institution ADMINs | Migration note in §6 |

## 6. Prompt 1 migration notes

Implemented as [ADR 0017](../adr/0017-exam-role-policy-and-resource-scope.md). **No data migration, backfill or production-user edit is needed or
performed.** Everything below is configuration, behavior change, or lazily created private data.

### 6.1 Configuration to set before `EXAMS_ENABLED=true`

| Setting (exam API environment only) | Action |
|---|---|
| `EXAM_SUPER_ADMIN_EMAILS` | **New.** Comma-separated platform administrators. Empty (default) means nobody is a super-admin. The master prompt says "four" addresses but lists three (D-1); the operator supplies the final list here. Never commit the values. |
| `EXAM_FACULTY_EMAILS` | Existing name, tightened meaning: the address grants `teacher` only, and only for a **verified** token e-mail. Review the list. |
| `EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL` | **New, development only** (default `false`). Lets Firebase-emulator e-mail/password accounts that report `email_verified=false` use the allowlists. Ignored (and logged) when `ENV=production`. |

Staff must sign in with a provider that verifies the address (for example Google sign-in) or verify their e-mail. An unverified e-mail/password account
that merely claims an allowlisted address now receives no staff authority.

### 6.2 Behavior changes operators and teachers will see

| Area | Before | Now |
|---|---|---|
| Teacher access | Any faculty account could open any definition, run, blueprint, answer key, submission, export or camera-evidence ticket | Owner only, plus ADMINs of the resource's institution, plus super-admins |
| Source library | `list_sources` returned everyone's sources | Each teacher sees their own (legacy sources resolve their owner through the upload) |
| Pending short-answer reviews | Global list | Only reviews on runs the caller may manage |
| Institution creation | Any faculty | Super-admin only |
| Roster import / apply | Any faculty, any institution | ADMIN of that institution or FACULTY (STUDENT rows only); super-admin always; apply is bound to the path institution. *(Amended in Prompt 2, §7.2: an ADMIN no longer grants ADMIN/FACULTY — only a super-admin does.)* |
| Membership revocation | Any faculty (with recent re-authentication) | ADMIN of that institution or super-admin, and still requires recent re-authentication |
| Revoked / stale session | Token stayed usable until expiry | 403 `membership_not_active` / `stale_authorization_version` on the next request |
| New definition scope | None recorded | Defaults to the teacher's only teaching institution; `institution_id` required when they teach in several; bootstrap/pilot teachers with no membership stay unscoped |
| Scoped run enrollment | Any uid list | Only ACTIVE students of the run's institution (422 `enrollment_outside_institution`); variant reservation only for enrolled uids (422 `uids_not_enrolled`) |
| `GET /api/exams/v1/me` | `is_bootstrap_faculty`, `memberships`, … | Same keys plus `email_verified`, `is_super_admin`, `exam_roles` (additive) |

A pilot operator who previously used `EXAM_FACULTY_EMAILS` to administer everything should be added to `EXAM_SUPER_ADMIN_EMAILS` (platform-wide) or
made an institution ADMIN (one institution).

### 6.3 Data and schema

- **New collections** (Admin SDK only, created lazily, covered by the default-deny rule): `exam_staff_bindings/{sha256(normalized e-mail)}` and
  `exam_authz_audit/{id}` (append-only; events `staff_bound`, `staff_binding_conflict`).
- **Additive optional fields on non-strict models:** `ExamDefinition.institution_id`, `ExamRun.institution_id`, `ApprovedSource.created_by`. Legacy documents read
  as `None`: a legacy definition/run is reachable by its owner and super-admins; a legacy source with no `created_by` resolves its owner through its
  upload, otherwise super-admin only.
- `firestore.rules` and `firestore.indexes.json` are unchanged. No new composite index is required.

### 6.4 Operator procedure: staff binding conflict

The first UID to prove an allowlisted verified e-mail is bound to it. If a different UID later presents the same e-mail it is denied and a
`staff_binding_conflict` audit event is written; the bound UID keeps its access. To legitimately move authority to another account, an authorized
operator confirms the person's identity and deletes that one binding document (the audit trail stays). The document id is the SHA-256 of the lower-cased
e-mail:

```
python -c "import hashlib; print(hashlib.sha256(b'person@college.edu').hexdigest())"
```

This is a production data change: do it only with explicit authorization, and never delete other collections as part of it.

### 6.5 Rollback

Revert the Prompt 1 commit. The two new collections and the optional fields are inert for the previous code; nothing is deleted. Unused environment
variables are ignored. Be aware that reverting also restores the pre-fix behavior described in [baseline.md](baseline.md) §5 (revocation not enforced,
any-faculty access, unverified e-mail trusted).

### 6.6 Not verified in this phase

The Firestore emulator, concurrent-writer behavior of the binding `create()`, and a real browser session were not exercised; unit tests use in-memory
doubles. These are tracked for the emulator/staging phase in [delta-plan.md](delta-plan.md).

## 7. Prompt 2 migration notes

Implemented as [ADR 0018](../adr/0018-roster-linking-otp-and-teacher-assignment.md). **No data migration, backfill or production-user edit is needed or
performed**; no secrets or e-mail addresses were added.

### 7.1 Configuration

| Setting (exam API environment) | Action |
|---|---|
| `EXAM_OTP_HMAC_SECRET` | Existing. **Now required in production for OTP sign-in** (the challenge returns 503 while it is unset). Use one stable secret shared by every instance. |
| `EXAM_OTP_DELIVERY_PROVIDER` | **New.** Empty (default) = none chosen: the synthetic dev sink outside production; in production OTP sign-in is unavailable (`503 otp_delivery_not_configured`) until an adapter exists. `dev_sink` is ignored in production. No real provider adapter is shipped — that waits on decision D-6 (SSO versus verified-e-mail OTP). |

An e-mail OTP is a proof of mailbox control, not multi-factor authentication; do not describe it as MFA in any UI or document.

### 7.2 Behavior changes operators will see

| Area | Before | Now |
|---|---|---|
| Granting FACULTY / ADMIN | Super-admin, or an institution ADMIN (up to ADMIN), through roster rows | **Super-admin only** — roster rows with those roles, or `POST …/teachers`. ADMINs and FACULTY manage STUDENT rows |
| Roster row for an existing member | Whole membership overwritten (e-mail, role, status) | Revoked members stay revoked (`membership_revoked_reinstate_required`); members who outrank the uploader are untouched; changing a contact e-mail needs a super-admin or an ADMIN of that institution; identical rows are `skip_unchanged`; `uid` and `status` are never rewritten |
| Role or e-mail change by roster | Silent | Bumps `authorization_version` (existing sessions end) and writes an audit event |
| Roster apply | Any rows were applied | Only the rows validated by the dry-run (`409 roster_rows_changed` otherwise); duplicates and collisions are judged across the whole file; needs a recent sign-in |
| Roster input | JSON `{"rows": [...]}` | Unchanged, **plus** a multipart CSV `file` on the same two routes; row errors carry CSV line numbers |
| College-ID format | Universal trim/collapse/UPPERCASE | Same by default; an institution may set an `identity_policy` at creation (separator removal, length, alphanumeric) |
| Revoke | ADMIN / super-admin, recent sign-in | Also needs the authority to grant the member's role (an ADMIN cannot revoke FACULTY/ADMIN); optional `reason`; audited; revoking twice changes nothing |
| Reinstate / unlink | not available | New; `reason` (≥ 3 characters) required; audited; recent sign-in |
| Link conflict | `409` with a string `detail`, warning log | `409` with `{code, message}`; persisted audit event; new code `account_already_linked_in_institution` when the account already holds another identity in that institution |
| OTP code use | Check, then consume | One transaction: a code cannot be consumed twice |
| OTP delivery failure | Error to the caller | Indistinguishable from success (no enumeration); logged |
| Create institution | Super-admin | Also a recent sign-in; optional `identity_policy`; audited |
| `GET …/faculty/definitions` | Owned definitions | Owned **and** co-instructed |

### 7.3 New routes (all under `/api/exams/v1/faculty`, behind the exam flag)

| Route | Who | Purpose |
|---|---|---|
| `GET /institutions` | faculty / super-admin | Institutions the caller can see (all for a super-admin) |
| `GET /institutions/{id}/memberships` | roster scope | Members, masked e-mails, no uids; `role_scope`, `status`, `limit`, `after` |
| `GET /institutions/{id}/roster/{job_id}` | roster scope | Job status, counts, first 200 row errors (`include_rows=true` for all) |
| `POST /institutions/{id}/teachers` | super-admin | Grant FACULTY/ADMIN (audited, recent sign-in) |
| `POST /institutions/{id}/memberships/reinstate` | institution admin | Undo a revocation |
| `POST /institutions/{id}/memberships/unlink` | institution admin | Detach a linked account (conflict recovery) |
| `GET /definitions/{id}/instructors` | definition scope | Owner and co-instructors |
| `PUT /definitions/{id}/instructors` | super-admin or ADMIN of the definition's institution | Replace the co-instructor list (identity keys of linked, active FACULTY/ADMIN members) |

### 7.4 Data and schema

- **Additive optional fields on non-strict models:** `Institution.identity_policy`, `RosterImportJob.rows_digest`, `RosterRowResult.first_seen_row` /
  `line_number`, `ExamDefinition.instructor_uids`. Legacy documents read as `None` / `[]`. A roster job created before this change has no digest: its
  apply checks only the row count. `RosterRowInput` stays `extra="forbid"` and unchanged.
- **No new collection.** Audit events reuse `exam_authz_audit` (kinds listed in ADR 0018 §10). `list_by_instructor` is a single-field `array-contains` query:
  no composite index.
- Roster job documents are now written without null fields (a 5000-row job stays under Firestore's 1 MiB document limit).

### 7.5 Operator procedures

- **Import a roster:** dry-run (JSON or CSV file) → read `errors` from the response or `GET …/roster/{job_id}` → apply repeatedly, each time sending the **same**
  rows (the same CSV file) with `batch_size`, until the status is `completed`. Re-running a finished or half-finished apply is safe.
- **CSV format:** UTF-8, header `identity_key,contact_email[,role_scope]`, at most 5000 rows / 1 MiB. Open the file as text: a spreadsheet that stores IDs as
  numbers has already dropped leading zeros before the upload.
- **Identity policy:** supplied once when the institution is created; there is no way to change it later.
- **Conflict recovery:** confirm the person's identity, `unlink` the wrong account with a reason, then the rightful owner signs in with the OTP or links their
  existing account (`/auth/link`). Runs created earlier still enroll the old uid and need re-enrolling.
- **Reinstating:** `reinstate` with a reason; a roster import never does it.
- **Teacher onboarding:** a super-admin grants the role (`POST …/teachers`); the teacher proves their e-mail with an OTP and links the account they teach with; a
  super-admin or the institution's ADMIN then adds them to each definition they co-teach (`PUT …/instructors`).

### 7.6 Rollback

Revert the Prompt 2 commit. The optional fields and audit events are inert for the previous code; nothing is deleted and imported memberships remain. Reverting
restores the defects listed in ADR 0018 (roster overwrites, silent reactivation, dev OTP sink in production).

### 7.7 Not verified in this phase

The Firestore emulator and concurrent writers (transactions are tested with in-memory stand-ins), a real OTP delivery provider, and any browser flow. The
frontend was not changed and has no UI for these routes yet.

## 8. Prompt 3 migration notes

Implemented as [ADR 0019](../adr/0019-exam-domain-commands-and-finalization.md). **No data migration, backfill or production-document edit is needed or
performed**; no secrets or addresses were added.

### 8.1 Configuration

| Setting (exam API environment) | Default | Action |
|---|---|---|
| `EXAM_FINALIZATION_SWEEP_ENABLED` | `true` | **New and on by default** — the exam API now starts the finalization loop. Set `false` only as a rollback lever (a warning is logged); with it off, expired attempts are never sealed or graded |
| `EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS` | `15` | Seconds between sweeps. Each tick reads the IN_PROGRESS/BLOCKED attempts of every active run |
| `EXAM_FINALIZATION_ENDED_RUN_WINDOW_HOURS` | `24` | How long after `ended_at` a run is still swept |
| `EXAM_FINALIZATION_BATCH_SIZE` | `50` | Existing, previously unused; now the per-run seal cap per tick |
| `EXAM_SCHEDULE_AUTOMATION_ENABLED` | `false` | Opt-in: the sweeper releases a PUBLISHED run at `scheduled_start` and closes it at `scheduled_end`. Runs in the same loop, so it also needs the sweep flag |
| `EXAM_NEGATIVE_MARKING_ENABLED` | `false` | While false, blueprint slots with `negative_marks > 0` are rejected. Turning it on does **not** make grading apply them (decision D-8) |
| `tzdata` | pinned in both requirements files | Needed to validate IANA timezone names on slim images and Windows |

### 8.2 Behavior changes operators and teachers will see

| Area | Before | Now |
|---|---|---|
| Expired attempts | Stayed IN_PROGRESS forever; never graded | Sealed within about one sweep interval after their **own** deadline (accommodated and extended deadlines respected), with a receipt, audit event and the grading trigger |
| `POST …/attempts/{uid}/extend` | Plain update; no actor; a retry added minutes again; could touch a sealed attempt | Transactional; actor audited; optional `idempotency_key`/`reason`; `409 command_rejected` (`deadline_passed`, `attempt_resolved`, `run_ended`, `extension_limit_exceeded`); response adds `deadline`, `manual_extension_minutes`, `reused_existing_receipt`. Clients that send only `extra_minutes` keep working |
| `close` | Any IN_PROGRESS attempt (even past its deadline) counted as unresolved | Only attempts still inside their own deadline are counted; `force` acknowledges them. Closing never cuts them off |
| Run commands | `expected_version`, key, `force` | Same, plus optional `reason`; receipts and audit carry the actor |
| `POST …/definitions/{id}/publish` | `{slot_count}` | Same body still works and returns the same shape; new optional `blueprint_id`, `reason`, `idempotency_key`. With a blueprint the count and marks are derived and blockers return `422 publication_blocked` |
| Revision documents | `set` (overwritable) | Create-only; blueprint publishes also carry `blueprint_id`, `total_marks`, `slots` |
| Blueprint slots | No negative marks | Optional `negative_marks` (default 0) |
| Run policy | duration, warnings, grace, pause flag, accommodations | Optional `timezone`, `scheduled_start`, `scheduled_end`, `variant_policy`; invalid values are `422` |
| Reserve variants | `allow_balanced_reuse` request flag only | Also pre-authorised by `policy.variant_policy == "balanced_reuse"` |

### 8.3 New routes (all under `/api/exams/v1/faculty`, behind the exam flag, run/definition scope required)

| Route | Purpose |
|---|---|
| `GET /definitions/{id}/publish-preview?blueprint_id=&duration_minutes=` | Resolved counts, total marks, distributions, blockers, warnings; writes nothing |
| `POST /runs/{id}/attempts/{uid}/terminate` | Seal one live attempt now; `reason` (≥ 3 chars) and `idempotency_key` required |
| `POST /runs/{id}/attempts/{uid}/recover` | Re-drive grading for a sealed attempt; `reason` and key required |
| `POST /runs/{id}/access/grant`, `…/access/revoke` | Add / remove one student (`uid`, `idempotency_key`, optional `reason`); revoke refused once an attempt exists |
| `POST /runs/{id}/finalize-expired` | One bounded sweep for a run (same code as the background loop) |

### 8.4 Data and schema

- **Additive optional fields, omitted when unset:** `ExamRevision.blueprint_id/total_marks/slots/publish_reason`, `Attempt.finalization_reason/finalized_by`,
  `CommandReceipt.actor_uid/reason`, policy `timezone/scheduled_start/scheduled_end/variant_policy`, blueprint slot `negative_marks`. Legacy documents read with
  neutral defaults.
- **No new collection.** Audit events (`exam_*` kinds) reuse `exam_authz_audit`; the new commands reuse `exam_command_receipts` with keys
  `{run_id}__{command}:{uid}__{key}`. New queries (`status in [...]`, `ended_at >=`) are single-field: no composite index.
- **Rollback hazard (compat §4):** `ExamPolicy` is `extra="forbid"`. A run created with the new policy fields cannot be read by the previous build. Do not roll the
  API back while such runs exist; the sweeper flag and schedule flag can be turned off without a rollback.

### 8.5 Operator procedures

- **Sweeper stuck or disabled:** `POST …/runs/{id}/finalize-expired` seals what is past its own deadline; `…/attempts/{uid}/recover` re-drives grading for an attempt
  that was sealed but not graded.
- **Student lost time (outage):** `extend` **before** their deadline passes (with a reason and a key). After it passes the attempt is sealed and cannot be reopened.
- **Add a late student:** `access/grant` (institution runs: must be an active student of that institution), then they can be admitted while the run is RELEASED.
- **Remove a student:** `access/revoke` if they never started; otherwise `terminate` with a reason (the work is preserved and graded).

### 8.6 Rollback

Turn off `EXAM_FINALIZATION_SWEEP_ENABLED` / `EXAM_SCHEDULE_AUTOMATION_ENABLED` for a behavior rollback without a deploy. Reverting the Prompt 3 commit restores the
defects in ADR 0019 and fails to read runs that carry the new policy fields (see §8.4).

### 8.7 Not verified in this phase

Real Firestore transaction contention and the emulator (tests use an in-memory Firestore with optimistic transactions and thread jitter), read cost of the sweep at
scale, live websocket fan-out from the sweeper, and any browser flow. The frontend was not changed and has no UI for these routes yet.

## 9. Prompt 4 migration notes

Implemented as [ADR 0020](../adr/0020-durable-source-ingestion.md). **No data migration or backfill is needed or performed**; no secrets or addresses were added.

### 9.1 Configuration

| Setting (exam API environment) | Default | Action |
|---|---|---|
| `EXAM_EVIDENCE_STORAGE_BUCKET` | unset | **Now required in production for source uploads** (`503 kt_storage_not_configured` otherwise; the ingestion loop is not started). Uploaded bytes use the `kt-quarantine/` prefix of the same bucket. Give the bucket a lifecycle/retention policy: the app has no retention sweeper for sources (decision D-4) |
| `EXAM_MALWARE_SCANNER_ENDPOINT` | unset | **Now honoured**: `host:port` (or `tcp://host:port`) of a clamd daemon. Unset or invalid keeps every upload at `scan_unavailable` (nothing is read) |
| `EXAM_KT_MAX_ACTIVE_UPLOADS_PER_USER` | `200` | Per-teacher cap on live uploads (rejected and deleted rows do not count) |
| `EXAM_KT_PARSE_MEMORY_MB` | `1024` | Address-space limit of the parser child process (POSIX only) |
| `EXAM_KT_URL_ALLOWED_HOSTS` | empty | Comma-separated host suffixes URL sources may come from; empty = any public host |

The parser child needs the same Python environment as the API (it runs `python -m app.domain.kt.parse_worker` with the backend directory on `PYTHONPATH`).

### 9.2 Behavior changes operators and teachers will see

| Area | Before | Now |
|---|---|---|
| `POST …/kt/uploads` | Parsed inside the request, returned `{status: parsed, extracted_version_id, anchor_count}` (or `scan_unavailable`) | `202 {upload_id, status: "quarantined", queued: true}`; poll `GET …/uploads/{id}` |
| Invalid upload | `422` with a string `detail`; a rejected row was stored | `422` with `{code: "invalid_file", message, upload_id}`; same history row; the extension must now match the content |
| `POST …/kt/sources/from-url` | Fetched, bytes discarded, upload row stuck at `quarantined` | `202`; bytes stored, scanned, parsed; HTML/PDF/DOCX/TXT accepted by content; `422 unsafe_or_unreachable_url` / `invalid_file` on refusal |
| Approving a source | Possible with no extracted text (always for URL sources) | `409 content_not_ready` until the upload is `parsed`; approval pins `extracted_version_id` |
| Scanned PDF | "Parsed" with zero passages | `parse_failed / no_text` with an explanation |
| Tables, images, scanned pages | Silently dropped | Listed as warnings on the extracted version |
| Duplicates / quota | Allowed | `409 duplicate_upload` (same owner, same bytes) / `409 upload_limit_reached`; `429` when uploading too fast |
| Default scanner (none configured) | Status `scan_unavailable`, no message | Same status, with "ask an administrator to enable scanning, then retry" |

### 9.3 New routes (all under `/api/exams/v1/faculty/kt`, behind the exam flag, upload scope = owner / institution admin / super-admin)

| Route | Purpose |
|---|---|
| `GET /uploads` | Own uploads, newest first (`include_deleted`) |
| `GET /uploads/{id}` | Status, failure code/reason, versions (counts, hash, warnings), registered sources |
| `GET /uploads/{id}/versions/{version_id}/anchors?offset=&limit=` | Extracted text, paged (untrusted data: show as plain text) |
| `POST /uploads/{id}/retry` | New attempt for `parse_failed` / `scan_unavailable` |
| `POST /uploads/{id}/replace` | Multipart corrected file; new upload linked to the old one |
| `DELETE /uploads/{id}` | Remove bytes and text; keep the row and audit; refused (`409`) while an approved source cites it |

### 9.4 Data and schema

- **Additive optional fields:** `UploadedSource.source_kind/origin_url/final_url/fetched_at/ingest_attempt/failure_code/latest_extracted_version_id/replaces_upload_id/superseded_by_upload_id/deleted_at/deleted_by`;
  `ExtractedAnchor.section`; `ExtractedVersion.content_sha256/parser/warnings/stats/ingest_attempt/anchor_count/chunk_count`; new `UploadStatus` values `parsing`, `deleted`.
- **New subcollection** `kt_uploads/{id}/extracted_versions/{version}/anchor_chunks`; versions written before this change keep their anchors inline and read as before.
- **Outbox:** new kind `kt_ingest` (`run_id` is empty). `claim_batch` / `drain_outbox_once` gained optional `kinds` / `exclude_kinds`.
- Audit events reuse `exam_authz_audit` (`kt_upload_accepted|rejected|replaced|retried|deleted|parsed|failed|infected|scan_unavailable`).
- New queries are single-field (`uploaded_by`, `sha256`, `upload_id`); the ingest loop uses `processed == false` plus `kind in [...]` (equality + `in`).

### 9.5 Operator procedures

- **Enable reading of documents:** provision a bucket (`EXAM_EVIDENCE_STORAGE_BUCKET`) and a clamd daemon (`EXAM_MALWARE_SCANNER_ENDPOINT`), restart, then teachers use **Retry** on uploads that show "Not scanned".
- **A document will not read:** the status shows the reason. `no_text` (scanned), `encrypted`, `corrupt`: ask for a text-based or unprotected file and **Replace**. `parse_timeout` / `parse_crash`: **Retry**, then replace with a simpler export.
- **Remove a confidential upload:** **Delete** (bytes and text go; the metadata row and audit remain). If an approved source depends on it the request is refused — that source backs published questions.
- **Stuck `parsing`:** the job is redelivered after its lease; after the last delivery it settles as `parse_failed / processing_error` and can be retried.

### 9.6 Rollback

Turn ingestion off by leaving the bucket unset (uploads return 503) or the scanner unset (nothing is read). Reverting the Prompt 4 commit restores inline parsing and the defects in ADR 0020;
uploads created meanwhile keep their stored bytes and rows (the previous code ignores the extra fields) but their anchors are in `anchor_chunks`, which the previous code does not read.

### 9.7 Not verified in this phase

A real ClamAV daemon, a real bucket, the POSIX memory limit, Firestore behavior of the equality + `in` outbox query, parser behavior on real-world (not synthetic) documents, and any browser flow
against a running API. OCR and table/image extraction are not implemented.

## 10. Prompt 5 migration notes

Implemented as [ADR 0021](../adr/0021-grounded-generation-and-versioned-review.md). **No data migration or backfill is needed or performed**; no secrets or addresses were added.

### 10.1 Configuration

| Setting (exam API environment) | Default | Action |
|---|---|---|
| `EXAM_GENERATION_PROVIDER_BASE_URL` / `EXAM_GENERATION_PROVIDER_API_KEY` | unset | Both set = the real adapter (https JSON gateway you run, see `.env.example`); otherwise the deterministic fake (development only). Same for `EXAM_REVIEW_PROVIDER_*` (use a different provider/endpoint for the review) |
| `EXAM_GENERATION_HTTP_TIMEOUT_SECONDS` / `EXAM_GENERATION_HTTP_MAX_RETRIES` | `60` / `2` | Per call timeout and transport retries (timeout, 408/425/429/5xx) |
| `EXAM_GENERATION_MAX_ATTEMPTS` | `3` | Attempts per generation request before it stays failed |
| `EXAM_REQUIRE_EXECUTED_CODING_VERIFICATION` | `false` | Leave off: the judge-backed verification step (D-5) does not exist yet; on, no coding item could be approved |

### 10.2 Behavior changes teachers and operators will see

| Area | Before | Now |
|---|---|---|
| Generation input | Placeholder excerpt, any anchor id | Real excerpts from the approved, parsed source; `422 unknown_anchor` / `no_sources` / `no_usable_passages`; `409` for an unapproved or unparsed source |
| Short approved pool | Publication warning | Publication **blocker**; `POST …/blueprints/{id}/shortfall` (reason) accepts a short, non-empty slot. Existing blueprints with `requested_variant_count` above what was approved will now need approval of more variants or an accepted shortfall before the next publish |
| Choice options | Bare strings tolerated | `{key, text}` objects with an answer key naming a key; candidates already stored are unaffected (validation runs on new/edited ones) |
| Review | Last write wins | Compare-and-set; a superseded or already-decided candidate returns `409` |
| `published` status | Never set | Set when a revision containing the candidate is published |
| Variant assignment | Re-reserve overwrote | Create-only; use `POST …/runs/{id}/variants/assign` before the session starts |
| Student question | Option order as authored | Stored per-student order when the blueprint shuffles; otherwise unchanged |

### 10.3 New or changed routes (all under `/api/exams/v1/faculty`, behind the exam flag and resource scope)

| Route | Purpose |
|---|---|
| `POST /blueprints` | Now also accepts `spec`, `topics` (+`topics_confirmed`), `duration_minutes`, `language_allowlist`, `shuffle_options`, `variant_policy` |
| `GET /blueprints/{id}/capacity`, `POST /blueprints/{id}/generate`, `POST /blueprints/{id}/shortfall` | Counts, bounded multi-slot generation (5 slots per call), accepted shortfall |
| `POST /generation-requests` (changed), `POST /generation-requests/{id}/retry` | Grounded generation with `source_ids` and/or `source_anchors` (anchor optional); retry a failed request |
| `POST /candidates/{id}/edit`, `POST /candidates/{id}/regenerate` | Versioned edit; fresh candidates re-grounded on the same sources |
| `POST /runs/{id}/variants/assign` | Pre-attempt assignment from the revision's frozen candidates (`allow_balanced_reuse`) |
| `POST /definitions/{id}/topic-suggestions` | Extractive suggestions; nothing stored |

### 10.4 Data and schema

- **Additive optional fields:** `ExamBlueprint.topics/topics_confirmed_by/duration_minutes/language_allowlist/shuffle_options/variant_policy/shortfall_*`; `GenerationRequest` provider/latency/retries/bundle hash/source versions/excluded anchors/usage/completed_at/running_since/regenerates_candidate_id; `GenerationCandidate.version/previous_version_id/superseded_by_id/edited_by/edited_at/published_revision_id/execution_verified`; `ExamRevision.topics/shuffle_options/variant_policy/accepted_shortfall_slots/shortfall_reason`; new candidate status `superseded`; assignments gain `option_order`.
- No new collections or indexes (queries stay equality / `in` on single fields).
- Audit events reuse `exam_authz_audit`: `exam_candidate_reviewed`, `exam_candidate_edited`, `exam_shortfall_accepted|withdrawn`, `exam_variants_assigned`.

### 10.5 Operator procedures

- **Turn on a real provider:** run an https gateway that accepts `{system_prompt, prompt_version, input}` and returns one JSON object, set both generation variables (and the review pair, preferably a different provider), restart. Start with one slot and read the stored request (provider, latency, usage, bundle hash).
- **A request failed:** capacity shows the last error per slot; **Retry** (up to the attempt cap) or generate again.
- **Publishing is blocked:** the preview lists each blocker; approve more variants, or accept the shortfall with a reason. An empty slot cannot be waived.
- **Before a session:** publish, schedule, then **Assign variants**; re-running only fills gaps.
- **Rollback:** the new fields are optional; reverting the code leaves them ignored. Candidates in `superseded` or `published` would be unknown statuses to the old code, so roll back before editing/publishing, or treat both as not servable.

## 11. Prompt 6 migration notes

Implemented as [ADR 0022](../adr/0022-student-attempts-gating-and-idempotent-submit.md). **No data migration or backfill is needed or performed**; no secrets or addresses were added.

### 11.1 Configuration

| Setting (exam API environment) | Default | Action |
|---|---|---|
| `EXAM_STUDENT_REVISIT_RELEASED_SLOTS` | `false` | On = a student may re-open questions they were already shown (`GET …/questions/{n}`); off keeps current-slot-only. Decide with the institution (D-3) before Prompt 7's navigator is used |
| `EXAM_DRAFT_MAX_BYTES` | `100000` | Largest stored answer (`413 answer_too_large`); source-code answers are the largest in practice |

### 11.2 Behavior changes teachers, students and integrators will see

| Area | Before | Now |
|---|---|---|
| Submit retried after it committed | `409 attempt_not_in_progress` | `200` with the same receipt (same key); `409 already_submitted` (other key) |
| Same key, different request | Not detected | `409 idempotency_key_reused` |
| `PATCH …/drafts/{question_id}` with an unknown / future id | Created a draft | `404 question_not_in_run` / `403 slot_not_released` |
| `GET …/drafts/{question_id}` with an unknown id | Empty draft | `404` / `403` |
| `PATCH …/drafts/…` response | `{accepted, version}` | adds `replayed`; body accepts optional `client_save_id`, `client_epoch`, `marked_for_review` |
| `GET …/drafts/{id}` response | `{version, answer}` | adds `marked_for_review` |
| Public test run with an unknown / future id, or after sealing | Accepted (500 on a malformed id) | `404` / `403` |
| Oversized answer | Stored (or 500 at the document limit) | `413` |

Existing clients keep working: every new request field is optional. A client that has never sent `client_save_id` keeps the old "stale save = conflict" behavior.

### 11.3 New routes (all under `/api/exams/v1/student`, behind the exam flag, student run scope, `no-store`)

| Route | Purpose |
|---|---|
| `GET /runs/{id}/slots` | Navigator data for released slots (ids, answered, marked, version, locked count); no content |
| `GET /runs/{id}/questions/{n}` | A released slot's question; earlier slots only with the flag; later slots `403 slot_not_released` |

### 11.4 Data and schema

- **Additive optional fields:** `SubmitReceipt.request_hash`; `Draft.marked_for_review`, `Draft.last_save_id`, `Draft.last_content_hash`. Old documents read as before (receipts without a hash are matched by key only).
- No new collections or indexes. Seals now read the draft subcollection inside the transaction (a collection read, no new index).

### 11.5 Operator procedures

- **A student reports "submit failed":** ask them to reload; the page shows the receipt if the first submit committed. Support can read the receipt with the same key; a new key will answer `already_submitted`.
- **Grading skipped a draft:** a draft id that is not a slot id is logged and skipped (`draft id … is not a slot id`); drafts written before this change under an invalid id are left in place.
- **Rollback:** the new fields are optional and ignored by the previous code; reverting restores the old (weaker) retry and slot behavior. Do not roll back mid-exam: the old code answers a retried submit with a conflict.
