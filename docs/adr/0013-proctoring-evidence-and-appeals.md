# ADR 0013 — Proctoring evidence, warning block and appeals

**Status:** Accepted

## Context

Prompts 3/4 already reserved the exact seams this phase needed:
`AttemptStatus.BLOCKED` existed as a state with legal transitions to/from
`IN_PROGRESS`/`EXPIRED`, and every draft-save/question-access/submit path
already rejected a `BLOCKED` attempt with `attempt_blocked`. This phase
builds the thing that actually SETS that status — a capability-aware
browser-observation ledger, a private evidence-snapshot pipeline, and a
faculty review/student-appeal workflow — without touching any of that
already-tested gating logic.

Two real gaps were found and closed while wiring this in (not stand-ins —
genuine pre-existing bugs/omissions from Prompts 3/4, caught by writing
Prompt 8's own tests): `decide_seal_on_expiry`/`ExamFinalizationSweeper`
only ever looked at `IN_PROGRESS` attempts, so a BLOCKED attempt whose
deadline passed would have stayed open forever; and `close_stale_open_incident`
(new in this phase) initially computed a stale incident's duration from its
own `last_activity_at` instead of the sweep's current time, silently never
counting single-observation departures no matter how long they'd been open.

Committed on `exams/phase-8-proctoring-evidence`, stacked on top of
`exams/phase-7-faculty-student-ui` (current tip of the branch stack; `main`
remains untouched throughout).

## Decisions

### 1. One correlation function owns grace, cooldown, and one-continuous-departure

`correlate_observation` (pure, `app/domain/proctoring/decisions.py`) is the
entire contract: informational kinds (`blur`, `focus`, `permission_denied`,
`camera_error`, `connection_degraded`) are logged but never counted,
regardless of timing — never inferred as misconduct by themselves (contract:
"Blur alone, permissions prompts and connection/camera failures are
technical/informational by default."). Departure kinds (`page_hidden`,
`fullscreen_exit`) open/extend/close a single logical incident; a departure
shorter than `grace_seconds` never counts; a new departure starting within
`cooldown_seconds` of the previous one closing REOPENS that same incident
(preserving its true original start time and whether it was already
counted) rather than starting a new one — this is the literal mechanism
behind "count one continuous departure once." `client_event_id` dedup
(a bounded 64-entry rolling window per attempt) absorbs bursts/replays/
offline-retry duplicates before any of the above even runs.

### 2. Stale-incident sweep — the tab-closed-entirely case

A student who closes the tab mid-departure never sends the "returned" event.
`close_stale_open_incident` + a new background sweep thread
(`_proctoring_stale_incident_sweep_loop`, 15s tick) force-close any incident
with no activity for `EXAM_PROCTORING_STALE_INCIDENT_SECONDS` — computing
its counted duration from the sweep's own "now", not the last-seen
timestamp, so a single stray departure event that's been open for an hour
correctly counts as an hour-long departure once discovered, not zero.

### 3. Threshold is per-run policy, never a global default

`ExamPolicy.warning_threshold` (already existed, default 3, Prompt 3) is
read fresh from the run on every observation and every sweep tick — the
module-level `EXAM_DEFAULT_WARNING_THRESHOLD` setting that already existed
in config.py was, on inspection, never actually wired to anything; this
phase deliberately does NOT wire it either, using the per-run policy value
instead (contract #5: "policy can customize it").

### 4. Blocking reuses the existing state machine exactly — no new transition

`ProctoringLedgerService.record_observation` calls the SAME
`require_attempt_transition`/`AttemptRepository.update_in_transaction` used
by every other exam mutation. Crossing the threshold attempts
`IN_PROGRESS -> BLOCKED` (already legal); if already `BLOCKED`, it just
keeps `warning_count` in sync without re-transitioning — idempotent,
never raises on a repeat departure after blocking.

### 5. Unblock is "allow to continue" or "uphold" — never straight to FINALIZED

`resolve_block` was initially written to jump `BLOCKED -> FINALIZED` on
"uphold" — illegal per the Prompt 3/4 state table (only `IN_PROGRESS` and
`EXPIRED` are reachable from `BLOCKED`). Fixed before commit: "uphold" makes
**no status change at all** — the block simply stands, audited via a new
`IncidentLogEntry`. Only "allow to continue" actively transitions
`BLOCKED -> IN_PROGRESS`. An upheld block that outlives the exam's deadline
is picked up by the ordinary finalization sweep (§6) exactly like any other
still-open attempt — never a separate code path.

### 6. BLOCKED attempts are seal-eligible too (exam expiry while blocked)

`decide_seal_on_expiry` now treats `BLOCKED` the same as `IN_PROGRESS` for
sealing purposes (`AttemptRepository.list_blocked`, new, feeds
`ExamFinalizationSweeper.sweep_once` alongside `list_in_progress`). A
review-blocked attempt is not exempt from its own deadline — the last
acknowledged draft or blank is still sealed, a receipt still exists, and
faculty review/appeal proceeds against that preserved, immutable outcome
afterward, never by leaving the attempt open indefinitely.

### 7. Evidence: scoped single-use tickets, hash-verified, honestly incomplete

`EvidenceService` mirrors the live-control ticket pattern (ADR 0012) twice
over: a single-use `UploadTicket` (scoped to exactly one snapshot + uid,
expires in `EXAM_EVIDENCE_UPLOAD_TICKET_TTL_SECONDS`) for the student's
upload, and a single-use `ViewTicket` (scoped to one faculty uid) for
viewing — every successful view is written to `EvidenceAccessLogEntry`.
The server independently recomputes the uploaded bytes' sha256 and rejects
a mismatch (`SnapshotHashMismatchError`) rather than trusting the client's
declared hash. A denied permission, unsupported camera API, or failed
capture/upload records an explicit `EvidenceSnapshot.status=MISSING` with a
`missing_reason` — a complete, honest, non-error outcome. Nothing in
`app.domain.exams.student_service` (draft save, submit, finish) ever
imports or calls into the proctoring module at all — submission acceptance
structurally cannot depend on evidence succeeding.

### 8. No continuous video, no face recognition, no identity claims

Camera capture (`useCameraCapture`, frontend) starts the camera, grabs
exactly one still frame to a canvas, and stops the track immediately —
check-in (once, on starting the exam), periodic (every 5 minutes), and a
best-effort submission-time capture that never blocks or delays the actual
submit call. No face-matching/liveness library is used or planned; every
faculty-facing surface (the incident list API response, the evidence
viewer) carries the disclaimer text directly: "Browser telemetry is
client-reported, not cryptographic proof of misconduct" / evidence is "a
manual review aid only — not automatic identity/liveness proof."

### 9. Appeals are a separate channel from faculty resolution

`AppealsService` lets a student file an appeal regardless of current
attempt state (even after the window closed) and never itself touches the
attempt — only a faculty call to `resolve_block` changes attempt status.
Re-resolving an appeal (contract: "review reversal") bumps `Appeal.version`
rather than editing the original decision away, mirroring the regrade-
versioning pattern from ADR 0011.

### 10. Retention: bounded sweep, storage + record deleted together

`RetentionSweeper` (new background thread, hourly tick) deletes each past-
retention `EvidenceSnapshot`'s stored bytes AND its Firestore record in the
same call — there are no separate "derivatives/exports" in this phase (no
thumbnails, no CSV export yet), so deleting the one stored object is a
complete retention action. `EXAM_EVIDENCE_RETENTION_DAYS` defaults to 90.

## Consequences

- **Positive:** 47 new backend tests (every scenario in the Prompt 8 list —
  burst/duplicate/dedup, grace, cooldown/flapping/reopen, threshold-exact
  blocking, stale-incident sweep, hash match/mismatch, oversized payload,
  expired/wrong-owner/replayed upload and view tickets, missing-evidence
  reasons, review reversal, retention expiry, and the blocked-attempt seal-
  on-expiry path) — full backend suite 363/363 passing, ruff clean,
  `compileall` clean. Frontend: capability-aware observation reporting with
  an offline retry queue, one-frame-at-a-time camera capture wired into the
  header/submit flow, a student-facing incident-history + appeal UI on the
  blocked page, and a faculty incident/evidence/appeals review UI on the
  candidate-detail and live-control-room pages — 31 frontend tests passing,
  `tsc --noEmit`/`eslint`/production `next build` all clean.
- **Negative / follow-up required:** `InMemoryEvidenceStorage` is the
  default (no `EXAM_EVIDENCE_STORAGE_BUCKET` configured) — uploaded evidence
  does not survive a process restart in that mode; `FirebaseStorageAdapter`
  is a genuine, complete implementation over `firebase_admin.storage` but
  has **not** been exercised against a real provisioned bucket in this
  environment. The proctoring ledger/evidence/appeals services are tested
  only against in-memory fakes (the same `Service.__new__` + fake-repo
  pattern as every prior phase), not a real Firestore/emulator run — the
  same documented limitation as ADRs 0008/0010/0012. The stale-incident and
  retention sweeps are single-process background threads (same class of
  scaling limitation as the outbox/judge sweeps in ADRs 0008/0011). No
  liveness/anti-spoofing check exists for camera captures by design (out of
  scope, explicitly excluded by the master prompt) — a still frame proves
  only that some image was captured through this flow, nothing about who or
  what is in it. Browser observation reporting is wired into the main exam
  screen only, not the waiting-room/preflight screens (a deliberate scoping
  choice — those screens don't gate content on proctoring signals). No CSV/
  bulk evidence export exists yet (Prompt 9's gradebook-export territory).

## Migration / configuration steps

- New settings, safe defaults: `EXAM_PROCTORING_GRACE_SECONDS` (2.0),
  `EXAM_PROCTORING_COOLDOWN_SECONDS` (5.0),
  `EXAM_PROCTORING_STALE_INCIDENT_SECONDS` (120.0),
  `EXAM_EVIDENCE_UPLOAD_TICKET_TTL_SECONDS` (120),
  `EXAM_EVIDENCE_VIEW_TICKET_TTL_SECONDS` (60),
  `EXAM_EVIDENCE_RETENTION_DAYS` (90), `EXAM_EVIDENCE_STORAGE_BUCKET`
  (unset — in-memory storage until configured).
- New Firestore collections (`proctoring_states`, `proctoring_log`,
  `evidence_snapshots`, `evidence_upload_tickets`, `evidence_view_tickets`,
  `evidence_access_log`, `proctoring_appeals`) — no rules changes needed
  (default-deny catch-all).
- Two new background threads (`_proctoring_stale_incident_sweep_loop`,
  `_evidence_retention_sweep_loop`), started alongside the existing outbox/
  judge-sweep threads, only when `EXAMS_ENABLED=true`.
- No new frontend env vars.
- No data migration. Prompt 3/4 files changed only additively: one new
  pure decision-function branch (`decide_seal_on_expiry` now also accepts
  `BLOCKED`) and one new repository method (`AttemptRepository.list_blocked`),
  both purely expanding an already-reserved seam.

## Next-phase prerequisites

- Prompt 9 (gradebook) has real `warning_count`/block-resolution history to
  surface as the "integrity disposition" separate from raw academic grade,
  and a real evidence-access-log to include in scoped export audit trails.
- Prompt 10 (PWA/lockdown) inherits a proctoring pipeline that already
  assumes best-effort, non-blocking evidence capture — a managed-lockdown
  profile can tighten capability requirements without changing this
  contract (submission still never depends on evidence success).
