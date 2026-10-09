# Hirelog Exams — repository baseline (v1.1 re-baseline, Prompt 0)

Date inspected: **2026-10-09**. Pack: `03-IMPLEMENTATION-PROMPTS (1).md` v1.1 ("incremental
implementation update" — the Exams module already exists; this is **not** a greenfield build).
This phase was **read-only for application code**. Findings are traced requirement-by-requirement in
[requirements-traceability.md](requirements-traceability.md); callers/wiring are in
[integration-map.md](integration-map.md); stable contracts in
[compatibility-and-migration.md](compatibility-and-migration.md); the confirmed-gap plan in
[delta-plan.md](delta-plan.md). Everything below describes the repository **before** Prompt 1 work.
The 2026-09-29 v1.0 baseline is preserved unchanged in
[Historical record](#historical-record--v10-prompt-01-inspection-2026-09-29) at the end of this file.

## 1. Repository state

| Item | Finding |
|---|---|
| Branch / HEAD | `exams/phase-11-production-qualification` @ `5b0ad3bbed19947e9c97b403d9594be920b8b0ee` (2026-10-04, "Coding questions: real IDE, judge singleton fix, and exam-integrity enforcement"); 18 commits; every commit is pushed to `origin/exams/phase-11-production-qualification` |
| Remote | `origin` = `https://github.com/saiabhinav001/hirelog.git` |
| Provenance (resolves the v1.0 open item) | Local `main` (`801696e`, "baseline snapshot") and `origin/main` (`0be13f6`, the commit named in the design pack) have the **identical tree** (`5eba7737c645bfcdb93d5c5e3e122ce426847437`), so the snapshot content equals the original GitHub commit. The **histories are unrelated** (`git merge-base` is empty): the exam branch cannot be opened as a PR into `origin/main` without `git rebase --onto origin/main main <branch>`. CI (`pull_request` / `push: main` only) has therefore most likely never run against exam work (not verifiable from this checkout). |
| Working tree | 3 uncommitted items, **left untouched**: `frontend/next.config.ts` (modified: `turbopack.root` pin), `backend/scripts/seed_demo_student.py` (untracked), `docs/exams/user-guide.md` (untracked) |
| Project instructions | No `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md` or `*.instructions.md`. De facto standard: [docs/engineering-standards.md](../engineering-standards.md) (snake_case payloads, token retrieval via `getClientAuthToken`, env-var rules, "lint/type-check/tests/build pass" PR checklist) |
| Design pack inputs | Only the prompt pack(s) exist on disk. `01-RESEARCH-REPORT.md` and `02-MASTER-TECHNICAL-DESIGN.md` were **not found anywhere** → design-section citations cannot be verified; requirements below are derived from the v1.1 prompt text itself |
| Toolchain | Local: Python 3.12.0 (`backend/.venv`), Node 22.19.0 / npm 10.9.3. CI pins Python 3.11 and Node 20 (`Dockerfile.exams` also 3.11). Not a failure; a reproducibility note |
| Local runtime config | A gitignored `backend/.env` exists (real Firebase project used for live testing since 2026-10-03; contents were **not** read). It sets `EXAMS_ENABLED=true`, which is the cause of the 11 test failures in §2 |
| Secrets | None in tracked files (qualification report §4 grep re-confirmed by this audit: no key material in `backend/app/**` or `frontend/src/**`). `.env.example` documents every exam variable with safe defaults |

## 2. Baseline checks (run 2026-10-09, read-only, nothing modified)

| Check | Command | Result |
|---|---|---|
| Backend lint | `ruff check .` (backend venv) | clean |
| Backend compile | `python -m compileall -q app` | clean |
| Backend tests | `pytest -q -p no:warnings` (JUnit XML in `%TEMP%`, deleted) | **490 tests: 479 pass, 11 fail** |
| …the 11 failures | all in `tests/test_exams_routes_smoke.py` (`*_is_503_when_exams_disabled`) | **Environmental, pre-existing**: they assert `settings.EXAMS_ENABLED is False` but the local gitignored `.env` sets it `true`. With `EXAMS_ENABLED=false` forced in the environment all 11 pass (490/490). Test-isolation defect, local-only; CI has no `.env` |
| Frontend lint | `npm run lint` | 0 errors, 1 warning (unused `eslint-disable` directive, `faculty/[definitionId]/page.tsx:59`) |
| Frontend types | `npm run type-check` | clean |
| Frontend unit tests | `npm run test:ci` (Vitest) | 13 files / 49 tests pass |
| Frontend audit | `npm audit --omit=dev` | **11 vulnerabilities: 2 critical (`next`, `websocket-driver`), 7 high, 2 moderate** (pre-existing; `next` still pinned `16.2.10`). CI's `npm audit --audit-level=high` step has no `|| true`, so it would fail today |
| **Not run** | `next build`, Playwright e2e, `bandit`/`pip-audit` (not installed locally), Firestore-emulator/rules tests (none exist: no `firebase.json`/emulator config in repo) | evidence gap, not a pass |

Test inventory: 46 backend test files / 453 test functions (490 collected cases), 13 Vitest files, 3 Playwright specs
(`smoke`, `exams-pwa`, `exams-accessibility`). Backend tests are pure-unit/fake-based; **no test exercises a real or emulated
Firestore transaction** (acknowledged in ADR 0008 §1).

## 3. What exists (summary; details in the other Prompt 0 documents)

- **Separate exam API** (`backend/app/exam_main.py`, `Dockerfile.exams`, `requirements-exams.txt`) with 12 route modules / ~91 routes under
  `/api/exams/v1/**`, behind `EXAMS_ENABLED` (default `false`) + router-level `require_exams_enabled`. Intentional (ADR 0008 §9) — to be preserved.
- **Domain packages** `exams` (definition/revision/run/attempt/draft state machine, decisions, outbox, finalization sweeper), `institution`
  (membership, roster import, OTP login, account link), `kt` (upload validation, SSRF-safe URL fetch, parsers, source registry), `generation`
  (blueprint, provider-neutral generation, validation, independent review, variants), `judge` (fake/stub provider, grading, regrade lineage),
  `proctoring` (append-only ledger, evidence tickets, appeals, retention), `live` (ticketed WebSocket), `gradebook` (aggregation, release
  versions, appeals, similarity, exports, feedback), `lockdown` (SEB admission gate).
- **Frontend**: 17 `/exams/**` pages, `ExamShellHeader`, `CodeEditorPanel` (CodeMirror 6), exam-scoped PWA shell (`exams-sw.js`), global
  light/dark `ThemeContext`.
- **Verified live (2026-10-03/04, real Firebase project, fake generation/judge providers)**: faculty create → source → blueprint → generate →
  approve → publish run → assign variants → release; student admit → answer → submit → receipt.
- **Collections** (all Admin-SDK-only; `firestore.rules` default-deny covers them): see [integration-map.md §6](integration-map.md).

## 4. Repository drift versus the v1.1 pack / earlier documents

| Pack / document assumption | Actual state | Impact |
|---|---|---|
| v1.1 Prompt 0 deliverables exist | Only the v1.0 baseline (this file's appendix) existed; `integration-map`, `requirements-traceability`, `compatibility-and-migration`, `delta-plan` did not | Created in this phase |
| Roles are `student`, `teacher`, `super_admin` | Code has `RoleScope` = `student/faculty/proctor/admin` (per institution) + legacy `viewer/contributor/placement_cell`; exam bootstrap is the `EXAM_FACULTY_EMAILS` env allowlist, which grants **admin-equivalent** authority; there is no `super_admin` anywhere | Mapping decided in ADR 0017 (reuse the existing role model; no second role store) |
| "Bootstrap the **four** provided super-admin emails" (Prompt 1) | Master prompt lists **three**; v1.0 listed a fourth that v1.1 removed; Prompt 1's wording is stale. None of the addresses is hard-coded in the repo | Allowlist is env-driven (`EXAM_SUPER_ADMIN_EMAILS`); operator supplies the final list |
| Courses / course-scoped capabilities | No course entity exists. The UI label "Assigned courses" is `GET /faculty/definitions` (definitions owned by `owner_uid`). `ExamRun`/`ExamDefinition` carry no institution | Course scope is `BLOCKED_BY_DECISION` (Prompt 2); Prompt 1 scopes by owner + institution |
| Prior reports (`production-qualification-report.md` §6) say no real Firebase project / `.env` exists | A real project and `.env` now exist locally (2026-10-03+) | Report §6 is historical |
| `adversarial-review-report.md` §1 High finding (revoked membership has no effect) "described, not implemented" | **Still open at HEAD**: `require_active_institution_membership` is defined but wired into **zero** routes | Fixed in Prompt 1 (see delta-plan) |
| Durable queue / "no in-process thread is the sole owner of a critical task" (v1.0 invariant 9) | Outbox drain, judge sweep, proctoring-stale and evidence-retention loops run as daemon threads inside the API process; the **finalization sweeper is never started** | v1.1 invariant 9 only requires reuse of the existing reliable queue; the missing sweeper is a defect (TMR-02) |
| CI covers exam work | `ci.yml` runs on PR/push-to-`main` only; branch history is unrelated to `origin/main` | Needs history rebase or a branch trigger before CI evidence exists |
| Firestore rules verified by emulator | No `firebase.json`/emulator/rules tests | Rules remain statically reviewed; Prompt 1 adds a static rules regression test only |

## 5. Pre-existing defects found by this audit (not fixed in Prompt 0)

Severity is the auditor's judgement; IDs point into [requirements-traceability.md](requirements-traceability.md).

| # | Severity | Defect | IDs |
|---|---|---|---|
| 1 | High | Revoked/stale institutional membership has no effect on an active session (open since the 2026-10-02 adversarial review) | AUTH-06 |
| 2 | High | Any faculty can read/modify **any** definition, run, blueprint, candidate (incl. answer keys), gradebook, appeal, export, proctoring incident and camera-evidence view ticket, issue live tickets for any run, create institutions, apply rosters to any institution and revoke any membership (no ownership/institution scoping) | AUTH-05, AUTH-09, LIV-02, GRD-07, ID-07 |
| 3 | High | Allowlisted-email authority (`EXAM_FACULTY_EMAILS`, admin-equivalent) trusts the token `email` claim with no `email_verified` check and no UID binding; `verify_id_token` has no `check_revoked` | AUTH-02, AUTH-04 |
| 4 | High (functional) | `ExamFinalizationSweeper` is never started → expired/abandoned attempts are never auto-sealed or graded | TMR-02, DOM-12 |
| 5 | Medium | Student timer uses the browser wall clock and has no ticking interval (display refreshes only on re-render); no time-remaining warnings | UI-01, UI-02 |
| 6 | Medium | Generation sends **placeholder** excerpts (`"[excerpt for anchor …]"`) and does not verify anchor ids against the extracted version | GEN-03, GEN-05 |
| 7 | Medium | KT uploads are parsed in the API thread pool and the original bytes are discarded (`storage_ref` is a dangling label); default `NullMalwareScanner` leaves every upload `SCAN_UNAVAILABLE` (never parsed); `EXAM_MALWARE_SCANNER_ENDPOINT` is ignored; URL sources get no extracted version | ING-02..ING-06 |
| 8 | Medium | Judge: `Judge0HttpAdapter.submit` raises `NotImplementedError`; only fake provider works; allowlist is python3.11/java17/cpp17 (unvalidated); retries-exhausted state undefined | JDG-01..JDG-07 |
| 9 | Low | 11 route-smoke tests depend on the developer's local `.env` | OPS-01 |
| 10 | Low | 11 npm advisories (2 critical in `next`/`websocket-driver`) — release blocker per qualification report | OPS-07 |

## 6. Known limits of this audit

- No Firestore emulator or real Firestore concurrency test was run (none exists in the repo); transactional behavior is evidenced only by pure
  decision tests plus ADR-recorded limitations.
- No real generation, judge, malware-scan, SSO/OTP-delivery or storage provider is configured; all provider findings are about adapter contracts
  and fakes, **not** real-provider behavior. Mocks do not prove sandbox security, production load or strict lockdown.
- Frontend behavior was audited statically (plus unit-test inventory); no browser session was driven in this phase.
- Statuses in the traceability matrix marked `UNVERIFIED` could not be confirmed from code or tests.

## 7. Addendum — outcome of the Prompt 1 implementation

Sections 1-6 describe the repository **before** Prompt 1 and are kept as the audit record. Defects #1-#3 above were addressed by Prompt 1
([ADR 0017](../adr/0017-exam-role-policy-and-resource-scope.md); status per requirement in
[requirements-traceability.md §1.1](requirements-traceability.md#11-status-after-the-prompt-1-implementation)). Defects #4-#10 are unchanged.

| # | Before | After Prompt 1 | Residual |
|---|---|---|---|
| 1 | Revoked/stale membership ignored on an active session | Enforced on every exam route (`get_current_exam_user`); student routes also require an ACTIVE student membership in the run's institution | A Firebase session revoked at the provider stays valid until expiry (`check_revoked` not enabled); an already-open live socket keeps streaming read-only events |
| 2 | Any faculty could act on any resource | Owner / ADMIN of the resource's institution / super-admin only, resolved through the ownership chain; institution creation super-admin only; roster and revoke scoped | Course-level scope waits on decision D-2; pending-review list is filtered after the global query |
| 3 | Allowlisted token e-mail trusted unverified and unbound | Verified e-mail only, bound to the first UID, conflicts denied and audited, fail closed | `check_revoked` (see #1); final super-admin list is operator configuration (D-1) |
| 3b (found during Prompt 1) | The allowlist was matched against `users/{uid}.email`, which a client may write when creating its own profile — a forged stored e-mail could have granted authority | Authority reads only the verified token e-mail; the stored value is display-only | `firestore.rules` still permits the `email` key on create (tightening needs emulator-backed rules tests, P11) |

Verification of the Prompt 1 work: backend suite 627 passed / 0 failed with `EXAMS_ENABLED=false` (490 before + 137 new), `ruff check .` clean, app
imports. Frontend was not changed and was not re-run. Not verified: Firestore emulator behavior, concurrent binding writers, a live browser session.

---

# Historical record — v1.0 Prompt 0/1 inspection (2026-09-29)

> Preserved verbatim for audit history. Superseded by the sections above wherever they differ (notably: the repository now *is* a git
> clone with history, and the v1.0 "new modules expected in later phases" list has since been built).

Date inspected: 2026-09-29. This workspace is a **plain file extraction, not a git
clone** — there was no `.git` directory and no way to confirm it matches commit
`0be13f6a208b924accb195308090938be0ebaa2c` from the design pack. A local repo was
initialized during this work (`git init`, baseline committed to `main`, all
subsequent work on branch `exams/phase-1-auth-security`) so history now exists
going forward, but the pre-existing history from GitHub is not available here.

## Drift from the implementation-pack assumptions

| Assumption in the prompt pack | Actual state found | Impact |
|---|---|---|
| A git repo at a known commit | No `.git` at all | No diff/blame against `0be13f6a208b924...`; treat all "baseline" claims here as file-content evidence, not commit-verified |
| `AGENTS.md` exists | Not present anywhere in the workspace or in the attached OneDrive folder | Closest equivalent is [`docs/engineering-standards.md`](../engineering-standards.md) (naming/casing/wiring rules) — treated as the de facto contributor instructions for this work |
| `01-RESEARCH-REPORT.md` / `02-MASTER-TECHNICAL-DESIGN.md` attached | Only `03-IMPLEMENTATION-PROMPTS.md` was available | Phase scope/acceptance criteria came from `03-...` only; section-level design citations (e.g. "design section 8") could not be verified against the actual design doc |
| Python 3.11 (CI pin, `scripts/bootstrap_windows.ps1` requires `py -3.11`) | Only Python 3.12.0 installed on this machine | Used 3.12 for baseline checks (satisfies `requirements.txt` markers `python_version < "3.13"`); flag Python 3.11 as a still-needed local setup item for anyone reproducing this exactly |
| Firebase project/credentials configured | No `FIREBASE_SERVICE_ACCOUNT_JSON`/`_PATH`, no `firebase.json`, no emulator config anywhere | Firestore-touching integration tests and "verify rule behavior in the emulator" could not be executed live in this environment — see Known Limitations below |

## Integration map (files touched or read for Prompt 0/1)

| Concern | File(s) |
|---|---|
| Auth dependency / role resolution | `backend/app/api/dependencies.py` |
| User profile schema | `backend/app/models/schemas.py` (`UserCreate`) |
| User profile routes | `backend/app/api/routes/users.py` |
| Firestore security rules | `firestore.rules` |
| Frontend auth context | `frontend/src/context/AuthContext.tsx` |
| Frontend E2E bypass | `frontend/src/lib/e2eAuth.ts`, `frontend/src/lib/authToken.ts` |
| Frontend route gating (client-side only) | `frontend/src/components/ProtectedRoute.tsx` |
| API client retries/failover | `frontend/src/lib/api.ts` (multi-base candidate list, exponential backoff + jitter, allowlisted `localStorage` origin) |
| Startup/lifespan background tasks | `backend/app/main.py` (`lifespan`), `backend/app/worker_main.py` |
| CI | `.github/workflows/ci.yml`, `.github/workflows/codeql.yml` |
| Deployment | `backend/Dockerfile`, `backend/Dockerfile.worker`, `backend/docker-compose.backend.yml`, `docs/backend-deployment.md` (Hugging Face Docker Space; optional split API/worker via `SEARCH_INDEX_WORKER_MODE=external` + Firestore-backed queue) |
| Existing tests | `backend/tests/*.py` (pure-unit style; no Firestore emulator harness present) |
| Anonymity invariant (untouched by this phase) | `backend/app/api/routes/experiences.py` (`_IDENTITY_FIELDS` guard), `docs/adr/0002-anonymity-invariant.md` |

New modules expected in later phases (not created yet): `backend/app/domain/exams/*`,
a second FastAPI entry point for the exam API, `backend/app/api/routes/exams_*.py`,
`frontend/src/app/exams/**`, private Firestore collections (e.g. `exam_*`,
`institutions`, `memberships`), and a separate evidence storage bucket.

## Confirmed finding: public profile role-escalation (fixed in this phase)

**Severity: Critical (OWASP A01:2021 — Broken Access Control / privilege escalation).**

Static evidence, pre-fix:

- `backend/app/models/schemas.py` — `UserCreate.role: Literal["viewer","contributor","placement_cell"] = "viewer"` accepted an arbitrary client-chosen role on the public profile create/update DTO.
- `backend/app/api/routes/users.py` — `create_or_update_user()` computed `"role": _highest_role(payload.role, str(user.get("role") or "viewer"))`, i.e. it **always kept the higher-priority value between the client-supplied role and the stored role**, using the Admin SDK (which bypasses `firestore.rules`).
- The frontend never legitimately sends anything but `role: "viewer"` (`AuthContext.tsx` signup flow), so the only exploit path was a **crafted direct API call** (e.g. `curl`/Postman with a real Firebase ID token from any normal signup) sending `{"name": "...", "role": "placement_cell"}`.
- Once persisted, `require_placement_cell` (used by `/api/dashboard/admin`, the moderation queue, visibility toggles, and search-runtime admin actions) reads the same Firestore `role` field and would grant real access — this was a genuine, working, server-side privilege escalation, not just a client-rendering gap.
- `firestore.rules` already blocked a *direct client Firestore write* from changing role, but that protection does not apply to the backend API path, which uses the Admin SDK.

Fix applied (branch `exams/phase-1-auth-security`):

1. `UserCreate` no longer has a `role` field at all — removed from the DTO, not just ignored.
2. `create_or_update_user()` never reads `payload.role`; the only role inputs are two **server-derived** values: the role `get_current_user` already resolved for this request (which applies the `PLACEMENT_CELL_EMAILS` allowlist), and the previously stored Firestore value. The "never regress on re-auth" behavior is preserved, but it now only compares two server-observed values, never client input.
3. `firestore.rules` — `users/{userId}` update rule now also restricts the **set of writable keys** to `name`, `display_name`, `name_last_updated_at` (in addition to the existing `role` unchanged check), and the create rule pins the writable key set too. This closes the "profile-field injection" half of the concern for any future field added to the user document without a matching rule update.
4. Frontend `e2eAuth.ts` — the `NEXT_PUBLIC_E2E_BYPASS_AUTH` test-only auth bypass now throws at module load if it is ever `true` in a `NODE_ENV=production` build, instead of relying purely on operational discipline not to set it.
5. `backend/app/api/dependencies.py` — split `get_current_user` (legacy, 5-minute cache, used by existing routes) from a new `get_current_user_uncached` (always re-verifies the Firebase token and re-reads Firestore role fresh). New exam authorization routes in later phases should depend on the uncached path so a role change/revocation is observed immediately, per contract #2 ("do not blindly copy... the five-minute role cache").

See [ADR 0007](../adr/0007-role-escalation-fix.md) for the full decision record.

## Known limitations of this verification pass

- **No Firebase emulator was run.** There is no `firebase.json`/emulator config in the repo and no Firebase CLI/Java runtime was installed in this environment. The `firestore.rules` change above was reviewed **statically** (rule text audited by hand against the create/update paths) but not executed against `@firebase/rules-unit-testing`. This is a real gap: rules syntax can still contain mistakes that only an emulator run would catch. **Action item for a follow-up:** add `firebase.json` + `firestore-debug` emulator config and a small Jest/Vitest rules-unit-test suite, then run it in CI.
- Backend regression tests use hand-written Firestore doc/collection fakes (see `backend/tests/test_role_escalation.py`), not a real Firestore instance — they prove the **route/schema logic** is correct, not that Firestore itself will enforce the same invariant end-to-end.
- No production deployment, credential rotation, or user data migration was performed. `PLACEMENT_CELL_EMAILS`-based promotion is unchanged and untouched.
- Python 3.11 vs 3.12 and the absence of git history remain open environment gaps (see Drift table above).

## Traceability (Prompt 1 slice)

| Requirement | Source | Implementation | Test |
|---|---|---|---|
| Public requests cannot grant placement_cell/faculty/proctor/admin | Master contract #2 | `schemas.py` (`UserCreate` has no `role`), `users.py` (`create_or_update_user`) | `test_role_escalation.py::test_new_viewer_cannot_grant_placement_cell`, `::test_viewer_role_unaffected_even_if_stored_doc_was_tampered` |
| Arbitrary authority/profile fields rejected | Prompt 1 | `UserCreate` schema (no extra authority fields exist to bind to) | `test_role_escalation.py::test_arbitrary_authority_fields_are_ignored` |
| Legitimate privileged user is not regressed | Prompt 1 | `create_or_update_user` role-merge uses only server-observed values | `test_role_escalation.py::test_legitimate_privileged_user_keeps_role_on_refresh` |
| Client-writable user fields restricted (rules + schema) | Prompt 1 | `firestore.rules` key allowlist; `schemas.py` | Static rule review (see Known limitations) |
| Production guard for test auth bypass | Prompt 1 | `frontend/src/lib/e2eAuth.ts` throws if bypass+production | Manual: normal `npm run build` (flag unset) verified green; forced-flag production build not executed (would require a dedicated CI job) |
| Identity verification separated from stale-cache authorization | Prompt 1 | `dependencies.py` — `get_current_user_uncached` | `test_auth_dependency.py::test_get_current_user_uncached_bypasses_stale_cache`, `::test_legacy_get_current_user_still_verifies_and_caches` |
| Legacy sign-in unaffected | Prompt 1 | No change to token verification, Firestore rules `create` path, or `_resolve_role` | Full existing suite green (33/33) |
| Anonymous experience identity preservation unaffected | Prompt 1 | No files under `experiences.py`/`nlp.py` were modified in this phase | Not re-tested directly; verified by diff scope (ADR 0002 guard untouched) |
