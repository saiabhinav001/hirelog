# Hirelog Exams — integration map (existing components and their callers)

Audited 2026-10-09 at `5b0ad3b` (pre-Prompt-1). Read-only evidence: every row below was read from code, not inferred from
design documents. Companion documents: [baseline.md](baseline.md), [requirements-traceability.md](requirements-traceability.md),
[compatibility-and-migration.md](compatibility-and-migration.md), [delta-plan.md](delta-plan.md).

## 1. Processes and entry points

| Process | Entry point | Image | Notes |
|---|---|---|---|
| Legacy API | [backend/app/main.py](../../backend/app/main.py) | `Dockerfile` | Search / experiences / practice / dashboard / users routers only. **Does not mount any exam router** (verified: `include_router` lines 241-245) |
| Legacy worker | [backend/app/worker_main.py](../../backend/app/worker_main.py) | `Dockerfile.worker` | Search-index queue only |
| **Exam API** | [backend/app/exam_main.py](../../backend/app/exam_main.py) | `Dockerfile.exams` (`requirements-exams.txt`, no torch/spaCy/FAISS) | Independently deployable (ADR 0008 §9); `docker-compose.backend.yml` service `exams` on port 8020 (container default 7862). CORS from `settings.allowed_origins_list()`; `_no_store_middleware` adds `Cache-Control: no-store` to every response |
| Frontend | `frontend/` (Next.js App Router) | — | `/exams/**` pages call the exam API through `examApiFetch` pinned to `NEXT_PUBLIC_EXAMS_API_BASE_URL` |

## 2. Authentication / authorization chain (as audited)

```
Authorization: Bearer <Firebase ID token>
  └─ api/dependencies.py::get_current_user_uncached      verify_id_token (no check_revoked); re-reads users/{uid}; adds auth_time + exam_* claims
       └─ api/exams_dependencies.py::get_current_exam_user        + EXAMS_ENABLED (503 when off)
            ├─ require_exam_faculty        bootstrap email ∈ EXAM_FACULTY_EMAILS (NO email_verified)  OR  ACTIVE institution FACULTY/ADMIN membership
            ├─ require_recent_staff_reauth auth_time recency only (documented as NOT MFA)
            ├─ require_active_institution_membership   DEFINED, CALLED BY NO ROUTE (only unit tests)
            └─ require_seb_admission       SEB header hash check; no-op unless the run's lockdown profile is enabled
```

| Dependency | Callers (routes) |
|---|---|
| `get_current_exam_user` | `exams_student` (list runs, accommodations, state, get/list drafts, receipt), `exams_me`, `exams_institution_auth.link`, `exams_live.ticket`, `exams_judge` student routes, `exams_proctoring` student routes, `exams_gradebook` student routes |
| `require_seb_admission` | `exams_student`: admit, question, next-question, patch draft, submit, finish (6 routes) |
| `require_exam_faculty` | **every** faculty route in `exams_faculty`, `exams_kt`, `exams_generation`, `exams_institution_admin`, `exams_judge`, `exams_proctoring`, `exams_gradebook`, `exams_lockdown` — it is the **only** authorization check on them (no owner / institution / resource scoping) |
| `require_recent_staff_reauth` | `exams_institution_admin.revoke_membership` only |
| `_has_institution_faculty_membership` | `require_exam_faculty`; imported privately by `exams_live._resolve_role` |
| `InstitutionAuthService.check_active_membership` | `require_active_institution_membership` only (unused) |
| `resolve_enrolled_uids_from_institution` | tests only — `POST /faculty/runs` takes `enrolled_uids` straight from the request body |

Role sources today: (a) `EXAM_FACULTY_EMAILS` env allowlist (also grants `RoleScope.ADMIN` inside `exams_institution_admin._requester_max_role_scope`),
(b) `institution_memberships` (`RoleScope` student/faculty/proctor/admin; `PROCTOR` grants nothing), (c) legacy `users/{uid}.role`
(`viewer/contributor/placement_cell`, **not** used for exam authority — ADR 0007/0008 §3). Token claims minted by the OTP flow:
`exam_authz_version`, `exam_institution_id`; synthetic uids `exam-{uuid4}`.

### 2.1 After Prompt 1 (ADR 0017)

Sections 2–8 otherwise describe the audited baseline. The authorization chain now is:

```
Authorization: Bearer <Firebase ID token>
  └─ api/dependencies.py::get_current_user_uncached      + token_email, email_verified, sign_in_provider (stored users/{uid}.email is NOT an authority input)
       └─ api/exams_dependencies.py::get_current_exam_user        + EXAMS_ENABLED; membership status + exam_authz_version enforced when the token carries exam_institution_id
            ├─ resolve_principal                    verified allowlists (UID-bound via domain/authz/staff_binding.py) + memberships, memoized per request
            ├─ require_exam_faculty / require_exam_super_admin   ROLE gates (teacher incl. super-admin / super-admin only)
            ├─ api/exams_scope.py::require_*_scope   RESOURCE gates: owner, ADMIN of the resource's institution, or super-admin (every faculty path id)
            ├─ require_student_run_scope            enrolled + ACTIVE student membership when the run has an institution_id
            └─ require_seb_admission                composes the student scope, then the SEB header check
```

Policy logic is pure (`domain/authz/policy.py`). Ticket-redemption routes stay unscoped by id because the ticket is minted by a scoped route and bound to
the issuing identity. The route-table tripwires are in `backend/tests/test_exam_authz_route_guards.py`.

### 2.2 After Prompt 2 (ADR 0018)

- The resource chain gained co-instructors: definition → (`owner_uid` **or** `instructor_uids`) → run / blueprint / candidate / gradebook. Only a super-admin or an
  ADMIN of the definition's institution may change the list.
- Granting FACULTY/ADMIN is super-admin only (`max_grantable_role_scope`); institution ADMINs and FACULTY manage STUDENT rows. Revoke / reinstate / unlink need the
  authority to grant the member's role.
- `require_recent_staff_reauth` now also gates creating an institution, applying a roster, reinstating, unlinking, assigning a teacher and changing instructors.
- OTP sign-in goes through an `OtpDelivery` adapter chosen by `EXAM_OTP_DELIVERY_PROVIDER` (none in production until D-6); the audit log
  (`exam_authz_audit`) now also records identity and roster administration.

## 3. Route inventory (88 routes + 3 root/health = 91 at the audit; **96 + 3 after Prompt 2**, see §2.2 and compat §7.3; Prompt 3 adds six faculty routes, see compat §8.3)

All routers carry router-level `dependencies=[Depends(require_exams_enabled)]` (regression-tested by `test_exams_routes_smoke.py`).

| Router (prefix) | Routes | Auth dependency | Main services → collections |
|---|---|---|---|
| `exams_faculty` (`/api/exams/v1/faculty`) | POST `/definitions`; POST `/definitions/{id}/publish`; POST `/runs`; GET `/runs/{id}`; POST `/runs/{id}/commands/{publish\|release\|pause\|resume\|close\|end}`; POST `/runs/{id}/attempts/{uid}/extend`; GET `/definitions` (owned **and** co-instructed); GET `/definitions/{id}/runs`; GET `/definitions/{id}/blueprints`; GET `/runs/{id}/attempts` (Prompt 7: + `server_time`); GET/POST `/runs/{id}/short-link` and POST `/runs/{id}/short-link/revoke` (Prompt 7, ADR 0023, flag `EXAM_SHORT_LINKS_ENABLED`); GET/PUT `/definitions/{id}/instructors` | faculty | `ExamCommandService` → `exam_definitions`, `exam_runs`, `exam_command_receipts`, `exam_outbox`; `ShortLinkService` → `exam_short_links`, authz audit; `live.publisher` |
| `exams_student` (`…/student`) | GET `/runs`; GET `/runs/{id}/accommodations`; GET `/runs/{id}/state` (Prompt 7: + `server_time`, `time_warning_minutes`); GET `/short-links/{code}` (Prompt 7: enrolled student only, same 404 for every refusal); POST `/runs/{id}/admit`; GET `/runs/{id}/question`; GET `/runs/{id}/questions/{n}` and `/runs/{id}/slots` (Prompt 6: released slots only); POST `/runs/{id}/next-question`; PATCH/GET `/runs/{id}/drafts/{qid}` (released slots only, idempotent by `client_save_id`); GET `/runs/{id}/drafts`; POST `/runs/{id}/submit`; POST `/runs/{id}/finish`; GET `/runs/{id}/receipt` | user / SEB | `ExamStudentService` → runs, attempts, drafts, outbox; `build_public_content` → `generation_candidates`, `exam_variant_assignments` |
| `exams_kt` (`…/faculty/kt`) | POST/GET `/uploads`; GET `/uploads/{id}` (+ `/versions/{id}/anchors`); POST `/uploads/{id}/retry` and `/replace`; DELETE `/uploads/{id}` (Prompt 4); POST `/sources/from-upload`; POST `/sources/from-url`; GET `/sources`; GET `/sources/{id}`; POST `/sources/{id}/review` | faculty | `kt_uploads` (+`extracted_versions`), `kt_approved_sources` |
| `exams_generation` (`…/faculty`) | POST `/blueprints` (slots or spec); GET `/blueprints/{id}`, `/candidates`, `/capacity`; POST `/blueprints/{id}/generate`, `/shortfall`; POST `/generation-requests`; GET `/generation-requests/{id}`; POST `/generation-requests/{id}/retry`; GET `/candidates/{id}`; POST `/candidates/{id}/review`, `/edit`, `/regenerate`; POST `/blueprints/{id}/slots/{n}/reserve-variants`; POST `/runs/{id}/variants/assign`; POST `/definitions/{id}/topic-suggestions` (Prompt 5, ADR 0021) | faculty | `exam_blueprints`, `generation_requests`, `generation_candidates`, `generation_malformed_candidates`, `exam_variant_assignments`; reads `kt_approved_sources` + `extracted_versions` |
| `exams_institution_auth` (`/api/exams/v1/auth`) | POST `/challenge`, POST `/verify` (**unauthenticated by design** — they mint the session); POST `/link` | none / user | `institution_memberships`, `institution_otp_challenges`; Firebase `create_custom_token` |
| `exams_institution_admin` (`…/faculty/institutions`) | POST `` (create institution); GET `` (list); GET `/{id}/memberships`; POST `/{id}/roster/dry-run` and `/{id}/roster/{job}/apply` (**JSON or multipart CSV**); GET `/{id}/roster/{job}`; POST `/{id}/memberships/revoke`, `/reinstate`, `/unlink`; POST `/{id}/teachers` | faculty / super-admin (+reauth on every authority-changing action) | `institutions`, `institution_roster_imports`, `institution_memberships`, `exam_authz_audit` |
| `exams_me` (`/api/exams/v1`) | GET `/me` | user | `institution_memberships` |
| `exams_judge` (`…/student/judge`, `…/faculty/judge`) | student: POST `/public-run`, GET `/public-run/{id}`; faculty: GET/POST `/grading-results/{id}[/regrade]`, POST `/mcq-results/{run}/{uid}/{q}/regrade`, GET `/short-answer-reviews/pending`, POST `/short-answer-reviews/{id}`; Prompt 8: GET `/runs/{run_id}/stalled-submissions`, POST `/runs/{run_id}/requeue-stalled` (run scope; requeue audited) | user / faculty | `judge_submissions`, `judge_grading_results`, `judge_mcq_results`, `judge_short_answer_reviews`; authz audit |
| `exams_live` (`…/live`) | POST `/ticket`; WebSocket `/ws?ticket&run_id` | user (ticket) | `live_tickets`; in-memory `live_connection_manager` |
| `exams_proctoring` (`…/student/proctoring`, `…/faculty/proctoring`) | student: POST observations/snapshots/appeals, GET incidents, POST snapshot upload + missing; faculty: GET incidents/snapshots/appeals, POST resolve/view-ticket/appeal-resolve, GET `/view/{ticket}` | user / faculty | `proctoring_states`, `proctoring_log`, `evidence_snapshots`, `evidence_upload_tickets`, `evidence_view_tickets`, `evidence_access_log`, `proctoring_appeals` |
| `exams_gradebook` (`…/student/gradebook`, `…/faculty/gradebook`) | student: results, feedback, grade appeals; faculty: rows, counts, release(+list), defective(+list), appeals(+resolve), similarity (3), export (create/get/ticket/download) | user / faculty | `gradebook_releases`, `gradebook_defective_questions`, `gradebook_grade_appeals`, `gradebook_export_*`, `similarity_matches` |
| `exams_lockdown` (`…/faculty/lockdown`) | GET/POST `/runs/{id}/profile`; GET `/runs/{id}/admission-log` | faculty | `exam_lockdown_profiles`, `exam_lockdown_admission_log` |

## 4. Domain packages (pure decision modules are Firestore-free)

| Package | Pure / decision modules | Imperative shell (Firestore) | Key callers |
|---|---|---|---|
| `exams` | `state_machine`, `decisions`, `policy`, `clock`, `models`, `errors` | `repository`, `commands` (`ExamCommandService`), `student_service`, `finalization` (`ExamFinalizationSweeper` — started from `exam_main.lifespan` since Prompt 3), `publication` (Prompt 3, pure preview/snapshot), `outbox`, `institution_enrollment` (tests only) | `exams_faculty`, `exams_student`, `exams_live`, `exams_proctoring`, `exams_gradebook`, `exam_main` |
| `institution` | `normalization`, `roster_import`, `roster_csv` (Prompt 2), `otp`, `models`, `errors` | `repository`, `roster_service`, `auth_service`, `identity_provider` (`DevSinkOtpDelivery`, `UnconfiguredOtpDelivery`, `build_otp_delivery`, `UnconfiguredSsoProvider`) | `exams_institution_*`, `exams_me`, `exams_dependencies` |
| `kt` | `file_validation`, `limits`, `source_registry`, `url_resolver`, `parsers`, `parse_worker` (child entry point) | `repository`, `ingestion` (`KtIngestionService` + outbox handler, Prompt 4), `parse_isolation`, `storage`, `malware_scan` (`NullMalwareScanner` default, `ClamdScanner` when configured) | `exams_kt`, `exam_main` (`_kt_ingestion_loop`) |
| `generation` | `validation`, `variants`, `review`, `independent_review`, `public_content`, `grounding`, `capacity`, `edit`, `blueprint_spec`, `topics` (Prompt 5) | `repository`, `commands` (review / edit / shortfall / publish marking / variant assignment), `jobs`, `provider` (fake default; provider-neutral HTTP adapter), `provider_http`, `independent_review_provider` | `exams_generation`, `exams_student`, `exams_judge`, `exams.commands.publish_revision` |
| `judge` | `grading`, `runtime_allowlist` (registry + `configured_allowlist`), `submission`, `converters`, `limits` (Prompt 8) | `repository`, `service`, `worker`, `outbox_handler`, `provider` (`DeterministicFakeJudgeProvider` - dev / test only; `UnconfiguredJudgeProvider` in production; `Judge0HttpAdapter.submit` raises `NotImplementedError`, D-5) | `exams_judge`, `exam_main` (outbox + sweep loop) |
| `proctoring` | `decisions` (correlation/grace/cooldown, `effective_proctoring`, image signature check), `models` | `repository`, `ledger_service` (attempt read first; reviewer entries + audit; review floor), `evidence_service`, `evidence_storage` (`InMemoryEvidenceStorage` in development only; production needs `EXAM_EVIDENCE_STORAGE_BUCKET`), `appeals_service` (append-only decisions), `retention` (oldest-first, holds) - Prompt 9 | `exams_proctoring`, `exam_main` |
| `live` | `events`, `tickets`, `models` | `connection_manager` (per-process memory), `publisher`, `repository` | `exams_live`, publishers in faculty/student/proctoring routes |
| `gradebook` | `decisions` (CSV sanitisation, starter-code strip) | `aggregation_service`, `release_service`, `similarity_service` (`DeterministicFakeSimilarityProvider` default), `export_service`, `feedback_service` (`TemplateFeedbackProvider` default) | `exams_gradebook` |
| `lockdown` | `decisions` (SEB hash verification) | `service`, `repository` | `exams_lockdown`, `require_seb_admission` |

## 5. Background work and realtime (all inside the exam API process, started only when `EXAMS_ENABLED`)

| Loop (daemon thread in `exam_main.lifespan`) | Interval | Idempotent / restart-safe | Note |
|---|---|---|---|
| `_outbox_drain_loop` → `drain_outbox_once` with `LoggingNoopHandler` + `GradingTriggerOutboxHandler` | 2 s | yes (claim/lease/attempt cap `EXAM_OUTBOX_MAX_ATTEMPTS`) | The Firestore-backed outbox mirrors the legacy `SearchIndexQueue` claim pattern; reuse, do not add a second queue |
| `_judge_sweep_loop` → `JudgeService.sweep_pending_once` | 2 s | yes | completes FINAL_GRADE / abandoned public runs; retries unsent / failed sealed submissions with backoff and hands exhausted ones to an operator (Prompt 8) |
| `_proctoring_stale_incident_sweep_loop` | 15 s | yes | closes departures with no end event (per state; the run's grace; ended attempts closed without counting) |
| `_evidence_retention_sweep_loop` → `RetentionSweeper` | 900 s | yes | marks lapsed upload requests missing; deletes bytes + record together unless held; not started in production without a bucket |
| `_kt_ingestion_loop` → `KtIngestionOutboxHandler` (kind `kt_ingest`, one job at a time, lease ≥ 120 s) | idle 2 s | yes (status-checked, create-only versions, stale-attempt jobs ignored) | scan + parse uploaded sources in a killable child process (Prompt 4, ADR 0020); the main outbox loop excludes this kind so grading and run commands never wait behind a document |
| `_finalization_sweep_loop` → `ExamFinalizationSweeper.sweep_active_runs_once` (+ `run_schedule_automation_once` when `EXAM_SCHEDULE_AUTOMATION_ENABLED`) | `EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS` (15 s) | yes (each seal re-decides in its own transaction) | seals attempts past their own deadline with receipt + `attempt_finalized` outbox row + audit (Prompt 3, ADR 0019); was absent at the audit (TMR-02) |

Realtime: WebSocket with single-use TTL ticket (`live_tickets`), per-run monotonically increasing `seq`, 15 s heartbeat; the socket never accepts
commands. Frontend `useLiveSocket` reconciles via REST on every event and falls back to 10 s polling after 5 failed reconnects. The connection
manager is **per process** (single-instance caveat, ADR 0012 §4).

## 6. Firestore collections (all private; `firestore.rules` default-deny catch-all; Admin SDK only)

| Domain | Collections |
|---|---|
| Exams | `exam_definitions` (+`revisions`), `exam_runs` (+`attempts` → +`drafts`), `exam_command_receipts` (`{run}__{command}__{key}`), `exam_outbox` |
| Institution | `institutions`, `institution_memberships` (doc id `{institution_id}::{NORMALIZED_IDENTITY_KEY}`), `institution_roster_imports`, `institution_otp_challenges` |
| KT | `kt_uploads` (+`extracted_versions` (+`anchor_chunks`, Prompt 4)), `kt_approved_sources`; object store prefix `kt-quarantine/` in the evidence bucket |
| Generation | `exam_blueprints`, `generation_requests`, `generation_candidates`, `generation_malformed_candidates`, `exam_variant_assignments` |
| Judge | `judge_submissions`, `judge_grading_results`, `judge_mcq_results`, `judge_short_answer_reviews` |
| Proctoring | `proctoring_states`, `proctoring_log` (append-only), `evidence_snapshots`, `evidence_upload_tickets`, `evidence_view_tickets`, `evidence_access_log`, `proctoring_appeals` |
| Live / lockdown | `live_tickets`, `exam_lockdown_profiles`, `exam_lockdown_admission_log` |
| Gradebook | `gradebook_releases`, `gradebook_defective_questions`, `gradebook_grade_appeals`, `gradebook_export_jobs`, `gradebook_export_tickets`, `gradebook_export_access_log`, `similarity_matches` |
| Authorization (Prompt 1) | `exam_staff_bindings` (doc id = SHA-256 of the normalized e-mail), `exam_authz_audit` (append-only) |
| Legacy (shared project) | `users` (client rules: self-read, create with `role == 'viewer'` + key allowlist, update restricted to `name/display_name/name_last_updated_at` with `role` unchanged), `interview_experiences`, `practice_lists` (+`questions`), `metadata` |

No composite indexes exist for exam collections (`firestore.indexes.json` lists only legacy collections); every exam query is single-field equality.

## 7. Frontend map (verified against the page sources / `examEndpoints.ts`)

| Page | Backend routes called |
|---|---|
| `/exams/login` | `auth/challenge`, `auth/verify`, `me` |
| `/exams/faculty` | `faculty/definitions` |
| `/exams/faculty/[definitionId]` | definitions, kt uploads (`SourceUploadPanel`: upload with progress, status, retry, replace, delete, warnings), kt sources (list/from-url/review), blueprints (create/list), generation-requests, candidates (list/review) |
| `…/runs/[runId]/lobby` | `runs/{id}`, run commands (publish/release), reserve-variants |
| `…/runs/[runId]/control` | `runs/{id}/attempts`, run commands (pause/resume/close/end), proctoring appeals, `live/ticket` + WebSocket |
| `…/runs/[runId]/candidates/[uid]` | attempts, `extend`, proctoring incidents/snapshots/resolve |
| `…/runs/[runId]/results` (+`appeals`, `similarity`) | gradebook rows/counts/release/defective/export/appeals/similarity |
| `…/runs/[runId]/lockdown` | lockdown profile/admission-log |
| `/exams/student` | `student/runs` |
| `/exams/student/[runId]/{preflight,waiting,exam,receipt,results,blocked}` | admit, state (+socket), question/next-question/drafts/submit/receipt/accommodations/judge public-run, gradebook results/feedback/appeals, proctoring incidents/appeals |
| Shared | `ExamShellHeader` (props only), `ExamProtectedRoute` (reads `/me` via `examAuth.tsx`; **UX gate only**), `CodeEditorPanel`, `useLiveSocket`, `useCameraCapture`, `useExamServiceWorker`, `proctoringObservations.ts` |

There is **no** faculty UI for institutions/roster/memberships (API-only today) and **no** UI for upload management beyond URL source registration.

## 8. Configuration and feature flags (`backend/app/core/config.py`, `backend/.env.example`)

`EXAMS_ENABLED` (false), `EXAM_NEW_ADMISSIONS_PAUSED` (false), `EXAM_FACULTY_EMAILS`, `EXAM_SUPER_ADMIN_EMAILS` (Prompt 1; empty), `EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL` (Prompt 1; dev only, false), `EXAM_DEFAULT_WARNING_THRESHOLD` (3),
`EXAM_DEFAULT_LATE_ADMISSION_GRACE_SECONDS` (120), `EXAM_FINALIZATION_BATCH_SIZE` (50), `EXAM_FINALIZATION_SWEEP_ENABLED` (Prompt 3; **true**), `EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS` (15), `EXAM_FINALIZATION_ENDED_RUN_WINDOW_HOURS` (24), `EXAM_SCHEDULE_AUTOMATION_ENABLED` (Prompt 3; false), `EXAM_NEGATIVE_MARKING_ENABLED` (Prompt 3; false), `EXAM_KT_MAX_ACTIVE_UPLOADS_PER_USER` (200) / `EXAM_KT_PARSE_MEMORY_MB` (1024) / `EXAM_KT_URL_ALLOWED_HOSTS` (empty) (Prompt 4), `EXAM_OUTBOX_LEASE_SECONDS` (60), `EXAM_OUTBOX_MAX_ATTEMPTS` (5),
`EXAM_OTP_HMAC_SECRET` / `_TTL_SECONDS` (300) / `_MAX_ATTEMPTS` (5) / `EXAM_OTP_DELIVERY_PROVIDER` (Prompt 2; empty), `EXAM_STAFF_REAUTH_MAX_AGE_SECONDS` (900),
`EXAM_MALWARE_SCANNER_ENDPOINT` (ignored), `EXAM_GENERATION_PROVIDER_*`, `EXAM_REVIEW_PROVIDER_*`, `EXAM_FEEDBACK_PROVIDER_*`, `EXAM_JUDGE_PROVIDER_*`,
`EXAM_LIVE_TICKET_TTL_SECONDS` (60), `EXAM_PROCTORING_GRACE_SECONDS` (2.0) / `_COOLDOWN_SECONDS` (5.0) / `_STALE_INCIDENT_SECONDS` (120),
`EXAM_EVIDENCE_*` (ticket TTLs, retention 90 d, `EXAM_EVIDENCE_STORAGE_BUCKET`), `EXAM_EXPORT_DOWNLOAD_TICKET_TTL_SECONDS` (300), `EXAM_JPLAG_*`.
Frontend: `NEXT_PUBLIC_EXAMS_ENABLED`, `NEXT_PUBLIC_EXAMS_API_BASE_URL`.

## 9. Seams that must be reused (do not re-implement)

| Concern | Existing seam |
|---|---|
| Identity verification | `get_current_user_uncached` → `get_current_exam_user` (one auth system; Firebase ID tokens) |
| Role / membership store | `institution_memberships` + `RoleScope`; `authorization_version` bump on revoke; token claims `exam_authz_version` / `exam_institution_id` |
| Staff recency | `require_recent_staff_reauth` (not MFA) |
| Error mapping | `exams_errors.raise_for_domain_error` / `raise_for_conflict_reason` |
| Idempotency / receipts | `exam_command_receipts`, `SubmitReceipt`, `decisions.py` pure functions |
| Durable work | `exam_outbox` + `drain_outbox_once` handlers |
| Rate limiting | `SlidingWindowLimiter` (`app.core.rate_limit`; Redis with in-memory fallback) |
| Realtime | ticketed WebSocket + REST reconcile/polling |
| UI | global CSS variables / `.card`, `.badge*`, `.btn-*`, `ThemeContext`, `States` components |
| Time on exam screens (Prompt 7) | `lib/serverClock.ts` + `useServerClock` (server-offset, monotonic); never `Date.now()` for a deadline |
| Confirming a state change (Prompt 7) | `components/exams/ConfirmDialog` + `lib/examConflicts.describeConflict`; reload state after success or rejection |

## 10. Planned-duplicate flags

None of the planned deltas (see [delta-plan.md](delta-plan.md)) introduces a second endpoint family, auth store, queue/worker, realtime channel or
design system. The two **new persisted concepts** introduced by Prompt 1 are the only additions with no existing home: staff-binding records and an
authorization audit log (documented in ADR 0017). The finalization sweeper (Phase 3) must be started from the existing `exam_main.lifespan` loop
pattern, not as a separate worker.
