# ADR 0015 — PWA shell, accessibility, and managed-lockdown admission

**Status:** Accepted

## Context

Prompt 10 bundles three mostly-independent concerns the master prompt groups
together: an installable static-shell PWA for `/exams/*`, accessibility
verification (manual + automated), and a genuinely separate strict-admission
profile for institutions that require a managed lockdown browser (Safe Exam
Browser — SEB). None of this touches grading, the gradebook, or proctoring
logic from Prompts 6–9.

Auditing the existing codebase for "fake lockdown" anti-patterns (contract:
"Do not disable browser shortcuts in ways that break accessibility while
pretending to achieve OS lockdown") found the codebase already clean of
keydown-blocking/right-click-disabling tricks — but surfaced two real,
pre-existing bugs, plus a third found later via the new PWA e2e tests
themselves:

1. `CodeEditorPanel.tsx` intercepted Tab to insert four spaces — a textbook
   WCAG 2.1.2 keyboard trap (a keyboard-only or screen-reader user could
   never leave the code editor). Removed entirely; Tab now always moves
   focus.
2. `next.config.ts`'s site-wide `Permissions-Policy: camera=()` silently
   blocked every `getUserMedia` call `useCameraCapture` (ADR 0013) makes on
   exam pages — every student's camera evidence capture would fail closed
   as "permission denied" in any browser enforcing Permissions-Policy,
   with nothing to indicate the real cause was this header. Fixed with a
   path-scoped override (`camera=(self)` for `/exams/:path*` only).
3. `useExamServiceWorker`'s `controllerchange` handler reloaded the page on
   the very FIRST-EVER service-worker activation (no prior controller) —
   not just on a genuine update to an already-controlled page. Caught only
   by a REAL-browser Playwright test (`e2e/exams-pwa.spec.ts`); the jsdom
   unit tests never exercise a real `controllerchange` event with real
   navigation side effects, so this class of bug could only be found by
   testing against an actual browser. Fixed by recording whether the page
   already had a controller at mount and only reloading in that case.

A fourth, unrelated finding while running the automated accessibility scan:
two site-wide `<nav>` landmarks (desktop `Navbar`, `Footer`) had no unique
accessible name (`aria-label`) — an `axe-core` "landmark-unique" violation
affecting every page on the site, not just `/exams/*`. Fixed with
`aria-label="Main"`/`"Mobile"`/`"Footer"`.

A fifth, structural finding unrelated to this phase's primary scope: neither
`exams_student.py` nor `exams_me.py` declared `require_exams_enabled` at the
ROUTER level like every other exam router — both relied solely on the
feature-flag check nested inside `get_current_exam_user`. Since
`get_current_user_uncached` uses FastAPI's `HTTPBearer()` security
dependency, a caller with NO Authorization header at all gets HTTPBearer's
own 403 "Not authenticated" before the nested feature-flag check ever runs —
meaning a *disabled* module could still answer 403 instead of the documented
503 contract for unauthenticated callers. Fixed by adding the router-level
dependency to both files, matching the other nine exam routers.

Committed on `exams/phase-10-pwa-accessibility-lockdown`, stacked on top of
`exams/phase-9-gradebook-similarity-feedback` (current tip of the branch
stack; `main` remains untouched throughout).

## Decisions

### 1. Static-shell service worker, explicit allowlist, nothing else ever intercepted

`public/exams-sw.js` precaches exactly `exams-offline.html`,
`exams-manifest.webmanifest`, `logo.svg`, `favicon.svg` — nothing else. Its
`fetch` handler only ever calls `event.respondWith()` for (a) an exact match
against that precached list, or (b) a failed navigation within `/exams/*`
(falls back to the offline page). Every other request — every `/api/` call,
every other navigation — is never intercepted at all; the absence of
`respondWith()` means the browser handles it exactly as if no service worker
existed. There is no offline queueing, no background sync, no IndexedDB
staging of drafts/submissions: a failed request while offline simply fails.

### 2. Update suppression is the app's decision, not the worker's

The worker never calls `self.skipWaiting()` on install — a new version sits
in the browser's normal "waiting" state until told otherwise. `useExamServiceWorker`
(the one piece of client logic that decides) only posts `{type:
"SKIP_WAITING"}` to a waiting worker when it is NOT told `attemptActive:
true`. The actual exam-taking page (`app/exams/student/[runId]/exam/page.tsx`)
passes `attemptActive: attempt?.status === "in_progress"` — exactly mirroring
the existing `useProctoringObservations`/`useCameraCapture` gating pattern
already used there. Every other exam page (mounted via
`ExamServiceWorkerRegistrar` in `app/exams/layout.tsx`) calls the hook with
no argument, meaning "safe to update any time" — correct, since only the
live exam-taking screen can ever be mid-attempt.

### 3. `camera_waived` is an explicit, faculty-authored accommodation — never inferred

`AccommodationWindow` (Prompt 3's per-student accommodation model) gained
`camera_waived: bool = False`. A new scoped student endpoint (`GET
/api/exams/v1/student/runs/{run_id}/accommodations`) exposes only the
CALLER's own `extra_minutes`/`camera_waived` — never another student's.
`useCameraCapture` now accepts this flag and, when true, never calls
`getUserMedia` at all — it records one honest
`missing_reason=camera_waived_accommodation` snapshot entry per capture
moment instead, so faculty reviewing evidence see an explained, complete
record rather than unexplained silence (contract: "camera alternatives
approved by institution").

### 4. Managed lockdown: only the header-verification half of SEB's protocol is real

Confirmed directly from SEB's own developer documentation
(`safeexambrowser.org/developer/seb-config-key.html`, fetched during
implementation): the Config Key header (`X-SafeExamBrowser-ConfigKeyHash`)
is `SHA256(absolute_url_without_fragment + config_key)`, hex-encoded.
`app/domain/lockdown/decisions.py::compute_seb_hash` implements exactly this
and is tested against an independently-computed reference hash (not just
round-tripped against itself). The Browser Exam Key header is treated
identically (same url+key SHA256 formula per SEB's integration overview)
under the header name `X-SafeExamBrowser-RequestHash` — the conventional
name used by SEB integrations (e.g. Moodle's SEB quiz access rule), NOT
independently re-confirmed against SEB's own BEK specification PDF (that
document 404'd when fetched during implementation). Deliberately NOT
implemented: deriving a Config Key from a full `.seb` settings file (SEB's
plist→"SEB-JSON" canonicalization has exact alphabetical-sort/date/real/
whitespace rules with no official test vectors available here to validate
against) — the raw key is instead an opaque, institution-supplied secret,
copy-pasted from SEB's own Config Tool/Preferences "Exam" tab, the same
"safe default, real institutional input required" pattern as the Judge0/
generation-provider adapters.

### 5. Admission verification fails closed, never uses User-Agent, and is audited

`LockdownProfile.enabled=True` with NEITHER a Config Key nor any Browser
Exam Key configured denies every admission (`DENIED_NOT_CONFIGURED`) —
enabling strict mode is a two-step action (toggle it on, THEN paste a real
key) and the gap between those steps is never accidentally permissive.
`verify_seb_admission`'s signature has no user-agent-shaped parameter at all
— a structural guarantee, not just a documented promise (tested via
`inspect.signature`). Every admission check, pass or fail, is written to
`LockdownAdmissionLogEntry` (`app/domain/lockdown/repository.py`) — a faculty-
visible audit trail, not a silent decision. `require_seb_admission` (a new
FastAPI dependency) is composed into `admit`/`get_current_question`/
`next-question`/`save_draft`/`submit`/`finish` — deliberately a superset of
just "admission", since SEB sends its headers on every request and a
one-time-only check would let a student quit SEB immediately after being
admitted and continue in an ordinary browser for the rest of the attempt.
Proctoring observation reporting and the public-run judge routes are NOT
re-gated in this phase — a documented, bounded scope decision, not an
oversight.

### 6. "Mark untested platforms unsupported" is a declared, admin-attested list — not an automatic gate

SEB's classic header mechanism carries no reliable, verifiable
OS/client-version field this backend can check cryptographically. `piloted_platforms`
on `LockdownProfile` is therefore a plain declarative list an institution
admin fills in after actually piloting a platform — a transparency/process
mechanism (surfaced on the new faculty lockdown-settings page,
`docs/exams/accessibility-and-lockdown-checklist.md`'s support matrix), not a
runtime security control. The real security gate remains the Config Key/BEK
hash check (§4–5); this list exists purely so "untested" is a documented
fact, not a silent assumption.

### 7. Automated accessibility checks are real; manual ones are honestly unperformed

`@axe-core/playwright` runs against `/exams/login` (the one exam page
reachable without mocking a full institutional auth + live exam-API
session) and a keyboard-reachability check confirms the submit button is
never unreachable. `docs/exams/accessibility-and-lockdown-checklist.md`
lists, as unchecked boxes, every manual verification NOT performed in this
sandboxed environment (real screen readers, real zoom/reflow on device,
color contrast beyond what axe can evaluate headlessly, a real SEB pilot,
real PWA installation) — matching the master prompt's own instruction not
to claim automated/mock coverage proves something only a real device or
real provider can prove.

## Consequences

- **Positive:** 20 new backend tests (bringing the suite to 460/460
  passing) covering SEB hash verification (including a fail-closed
  unconfigured case and a structural no-user-agent-parameter guarantee),
  admission auditing, and the `camera_waived` accommodation lookup — plus
  2 regression tests for the router-level feature-flag gap found and
  fixed. Ruff/`compileall` clean. Frontend: 7 new Vitest files (49/49
  total) and 3 new Playwright e2e specs (5/5 e2e passing) — genuinely
  caught and fixed THREE real bugs in the process (keyboard trap, blocked
  camera permission, premature page reload on first SW activation) that
  no amount of code review alone would likely have surfaced. `tsc
  --noEmit`/`eslint`/production `next build` all clean.
- **Negative / follow-up required:** No real Safe Exam Browser
  installation exists in this environment — the Config Key/BEK
  verification math is correct per SEB's own documented algorithm and
  unit-tested against an independent reference hash, but has never been
  exercised against a real SEB binary's actual HTTP headers. No real
  screen reader, zoom/reflow device testing, or PWA installation flow was
  performed — `docs/exams/accessibility-and-lockdown-checklist.md` lists
  these explicitly as outstanding gates, not silently skipped. The
  `piloted_platforms` field is purely declarative (no cryptographic tie to
  what a student's device is actually running) — a process/transparency
  control, not a technical one. `require_seb_admission` re-checks on every
  content/answer route but NOT on proctoring-observation or public-run
  judge routes — a bounded, documented scope choice. The Browser Exam Key
  header NAME (`X-SafeExamBrowser-RequestHash`) is the conventional one
  used by existing SEB integrations, not independently re-verified against
  SEB's own specification PDF (unreachable during implementation).

## Migration / configuration steps

- New Firestore collections (`exam_lockdown_profiles`,
  `exam_lockdown_admission_log`) — no rules changes needed (default-deny
  catch-all).
- Additive model change: `AccommodationWindow` gained `camera_waived: bool
  = False` (every existing accommodation is unaffected).
- New route: `GET /api/exams/v1/student/runs/{run_id}/accommodations`.
- New faculty routes: `GET`/`POST /api/exams/v1/faculty/lockdown/runs/{run_id}/profile`,
  `GET /api/exams/v1/faculty/lockdown/runs/{run_id}/admission-log`.
- New frontend route: `/exams/faculty/[definitionId]/runs/[runId]/lockdown`
  (settings + admission log) and `/exams/student/[runId]/results` is
  unaffected by this phase.
- No new backend environment variables — lockdown configuration is
  per-run Firestore data set via the faculty API, not a global setting.
- `next.config.ts`: new path-scoped `Permissions-Policy` override for
  `/exams/:path*` (camera allowed, microphone/geolocation still denied).
- New static public assets: `exams-manifest.webmanifest`, `exams-sw.js`,
  `exams-offline.html` — served from `/public`, no build step required.
- New dev dependencies: `@axe-core/playwright`, `vitest-axe` (the latter
  installed but the actual automated checks in this phase used
  `@axe-core/playwright` at the e2e layer — `vitest-axe` is available for
  future component-level checks).
- No data migration.

## Next-phase prerequisites

- Prompt 11 (production qualification) inherits a documented, non-exhaustive
  support matrix (§ "Support matrix" in the checklist doc) that must be
  filled in with real device/browser/SEB pilot results before any real
  cohort relies on strict-mode admission or the PWA install flow.
- The three real bugs found and fixed in this phase (keyboard trap, blocked
  camera permission, premature SW reload) are a reminder that this
  codebase's own automated checks — not just manual review — should keep
  running on every future phase that touches shared chrome (Navbar/Footer),
  global headers (`next.config.ts`), or the code editor.
