# ADR 0008 — Exam domain state machine and bounded final-submit transaction

**Status:** Accepted

## Context

Prompts 3 and 4 ask for the core exam domain: run/attempt state machines, a
validated policy schema, a transactional outbox with an idempotent worker
interface, and student-facing draft/submit/finish/receipt endpoints built on
a bounded final-submit transaction. Both prompts explicitly depend on
concepts (institution/active membership, session epoch, faculty authority)
that either don't exist yet (Prompt 2, not yet implemented) or were only
partially addressed (exam-specific authority, in Prompt 1).

This ADR records the bounded stand-ins used to make Prompts 3/4 buildable and
testable now, and the exact seams where Prompt 2/5/6/7/8 plug in later
without changing the code built here.

## Decisions

### 1. Functional core / imperative shell

All legality, idempotency, conflict, and deadline logic lives in pure,
Firestore-free functions (`state_machine.py`, `decisions.py`) operating on
plain Pydantic models. The Firestore-touching layer (`repository.py`,
`commands.py`, `student_service.py`, `finalization.py`) is a thin shell that
reads fresh state, calls the same pure function, and writes the result —
inside a real `google.cloud.firestore` transaction, so contention retries
always re-decide against current server state. This let every scenario in
the Prompt 3/4 test list (duplicate/stale commands, stale epoch, draft
conflicts, deadline races, accommodation preservation, bounded batches) be
exhaustively unit-tested (66 new tests) with zero mocking.

**Known limitation:** the thin shell itself — the actual Firestore
transaction wiring, and specifically two non-transactional reads inside
transactional callbacks (the "unresolved attempts" count in
`ExamCommandService.execute` and the draft-version read in
`ExamStudentService._submit_or_finish`) — is **not** validated against a real
Firestore/emulator in this environment (none was available; see
[docs/exams/baseline.md](../exams/baseline.md)). Per the master prompt's own
instruction, this must be validated in real staging Firestore before being
treated as production-correct. A concurrent draft-save landing between the
draft-version read and the submit transaction's commit is a real race this
phase does not fully close by construction — only a live transaction run can
prove the exact interleaving is safe.

### 2. Enrollment snapshot instead of Prompt 2's institution/roster repository

`ExamRun.enrolled_uids` is a plain, immutable list of Firebase UIDs captured
at run-creation time. "Active membership" reduces to `uid in
run.enrolled_uids`. This stands in for Prompt 2's institution/membership
repository, which does not exist yet. When Prompt 2 lands, only the *source*
of this snapshot changes (a roster query instead of a caller-supplied list);
the run/attempt state machine, deadlines, and submit transaction are
unaffected.

### 3. Exam faculty authority via email allowlist, not the legacy role field

`EXAM_FACULTY_EMAILS` (server config, mirroring the existing
`PLACEMENT_CELL_EMAILS` pattern) resolves exam faculty authority, checked in
`app.api.exams_dependencies.require_exam_faculty`. This is deliberately
**not** stored on `users/{uid}.role` — exam authority never lives in a
client-editable user record (contract #2). It is also deliberately **not**
reusing `placement_cell` (a different, pre-existing authority domain).
Prompt 2 replaces this with real institution-scoped faculty membership
without touching the command/domain layer.

### 4. Identity verification, not the 5-minute legacy cache

All exam routes depend on `get_current_user_uncached` (added in Prompt 1),
which re-verifies the Firebase ID token and re-reads the Firestore role on
every call — never the legacy 5-minute cache. This directly satisfies
contract #2 ("do not blindly copy... the five-minute role cache").

**Known implementation detail:** for student routes, FastAPI resolves
`Depends(security)` (the `HTTPBearer` sub-dependency of
`get_current_user_uncached`) before `require_exams_enabled`, so an
unauthenticated request against a disabled module returns 401/403 rather
than 503. Faculty routes gate `require_exams_enabled` at the router level
(before per-route auth), so they return 503 even unauthenticated. Both are
fail-closed; only the specific status code differs.

### 5. Question content is scoped to access control only, not real content

`PublicQuestionRef` (the allowlisted student-facing DTO) carries
`question_id`/`slot_index`/`total_slots`/an empty `public_content` dict. The
real question bank (prompt text, options, code-editor spec) is Prompt 5's
job. This phase's job — and the only thing tested — is that the CURRENT
slot's id is deterministically derived (`{revision_id}:slot:{index}`), never
prefetched ahead, and never returned once the attempt/run state says it
shouldn't be (`is_content_reachable`). Prompt 5 only needs to populate
`public_content`; it does not touch access control.

### 6. Run status `PAUSED`/`CLOSED` do not, by themselves, cut off an
   already-admitted, still-within-deadline attempt

`is_content_reachable`, `decide_submit`, and `decide_save_draft` gate on the
**attempt's own** status/deadline, not blindly on `run.status`. A `PAUSED`
run still lets an in-progress attempt view its current question (assumption:
pause freezes new admissions/commands, not an already-running attempt's
access) but blocks new draft saves/submits (`run_paused` conflict) — pause is
treated as "freeze mutations, not reads." A `CLOSED` run only hard-blocks via
the bounded finalization sweep once an individual attempt's own deadline has
actually passed; an accommodated student with a later deadline keeps working
until an operator runs `end`. This directly implements "closing a cohort must
not accidentally cut off an accommodated student."

### 7. Bounded finalization, never one enormous transaction

`ExamFinalizationSweeper.sweep_once` reads the (bounded) set of in-progress
attempts for a run, decides a capped batch (`decide_finalization_batch`), and
seals each selected attempt in its **own** small transaction
(`_seal_one`), independently re-checking deadline/state fresh. A crash
mid-sweep loses at most one partially-applied item, not the whole run, and a
delayed sweep never seals early (every write re-checks deadline itself).

### 8. Transactional outbox with at-least-once, idempotent delivery

Every state-changing transaction (command execution, submit finalization,
expiry sealing) writes an `OutboxEntry` in the same transaction. A separate
poll-based drain loop (`outbox.py`, mirroring the existing
`SearchIndexQueue` Firestore-claim pattern for consistency) claims and
processes entries; delivery is at-least-once, so `OutboxHandler.handle` must
be idempotent. This phase ships only a logging no-op handler — Prompt 6
(grading trigger) and Prompt 7 (live-control notify) register real handlers
later without changing the drain loop.

### 9. Separate exam API entry point, slim dependency set

`app/exam_main.py` does not import `app.main`, `app.core.health_checks`, or
any legacy service module — it deliberately avoids pulling in
spaCy/sentence-transformers/FAISS/torch. `backend/requirements-exams.txt` and
`backend/Dockerfile.exams` give it its own minimal, independently-deployable
image (fastapi/uvicorn/firebase-admin/pydantic only). `EXAMS_ENABLED`
defaults to `False`; every mutating exam route responds 503 until explicitly
turned on (contract #12 — safe by construction, not by operational
discipline).

## Consequences

- **Positive:** the hard concurrency/idempotency logic (66 new tests, all
  passing) is fully covered without any Firestore mocking; the module is
  structurally incapable of executing legacy startup work or leaking
  authority into client-editable records; shipping it disabled is provably
  safe.
- **Negative / follow-up required:** no live Firestore/emulator validation
  of the transactional shell (see limitation in §1); Prompt 2 must replace
  the enrollment-snapshot and faculty-email stand-ins; question content
  delivery is inert until Prompt 5; no faculty/student UI exists yet
  (Prompt 7); the outbox has no real consumer yet (Prompts 6/7).

## Migration / configuration steps

- New settings (all optional, safe defaults): `EXAMS_ENABLED` (default
  `false`), `EXAM_FACULTY_EMAILS` (default empty — no one has faculty
  authority until configured), `EXAM_DEFAULT_WARNING_THRESHOLD` (3),
  `EXAM_DEFAULT_LATE_ADMISSION_GRACE_SECONDS` (120),
  `EXAM_FINALIZATION_BATCH_SIZE` (50), `EXAM_OUTBOX_LEASE_SECONDS` (60),
  `EXAM_OUTBOX_MAX_ATTEMPTS` (5).
- New Firestore collections (`exam_definitions`, `exam_runs` (+ `attempts`/
  `drafts` subcollections), `exam_command_receipts`, `exam_outbox`) require
  no rules changes — the existing default-deny catch-all already covers
  them; a documentation-only comment was added to `firestore.rules`.
- No data migration. No existing collection/document shape changed.
- To deploy the exam API standalone: `backend/Dockerfile.exams` +
  `backend/requirements-exams.txt`, entry point `app.exam_main:app`,
  same Firebase env vars as the legacy backend.
