# Hirelog Exams — repository baseline (Prompt 0 inspection)

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
