# ADR 0019 — Exam domain: publication snapshot, commands, finalization and schedule

**Status:** Accepted

## Context

Prompt 3 asks for the exam domain and its state machines: validated policy, a preview of
marks and counts before publication, immutable revisions with pinned runs, legal
transitions with expected-version checks, and grant/revoke, pause/resume, extend,
terminate and recovery commands that are scoped, reasoned, idempotent and receipted —
preserving accommodated deadlines when a cohort is closed. Prompt 0 classified the open
rows as DOM-03/04/07/09/11/12, TMR-02/03, SCH-01 and MRK-01
(`docs/exams/requirements-traceability.md` §3). The existing domain (ADR 0008) already
had the run state machine, receipts, the outbox and a pure finalization decision.

Reading the code against that list found defects that mattered more than the missing
endpoints:

1. **The finalization sweeper was never started**, so no attempt was ever auto-finalized.
2. **Even when run, a seal was incomplete**: it wrote no `submit_receipt` and emitted
   `attempt_expired`, a kind nothing consumes — grading is triggered only by
   `attempt_finalized` — so an expired attempt would never have been graded.
3. **`grant_time_extension` was a plain read-then-write**: concurrent extensions lost
   updates; an extension could land on an attempt the sweeper had just sealed (or on one
   already past its deadline); a client retry added the minutes twice; no actor was kept.
4. **`close` counted attempts already past their own deadline as "unresolved"**.
5. **A revision document could be overwritten** (`set`), and publishing trusted a
   client-supplied `slot_count` unrelated to the blueprint or its approved candidates.

## Decisions

### 1. Same pattern for every state change
Read fresh state **inside** the transaction, decide with a pure function in
`decisions.py`, then write the state change, the receipt, the outbox row (where there is a
consumer) and an audit event together. Duplicate retries are answered from the receipt;
nothing is applied twice. New commands reuse `exam_command_receipts` (keyed
`{run_id}__{command}:{uid}__{key}`) rather than adding a collection.

### 2. Audit goes to the existing shared log
Events are `exam_*` kinds in `exam_authz_audit` (ADR 0017's append-only collection), with
`actor_uid`, subject `uid`, `institution_id` and a `detail` map (reason, from/to status,
deadlines, receipt id). They are written in the same transaction as the change, so a change
without its audit record cannot commit. Receipts gain optional `actor_uid` and `reason`;
unset values are not written, so legacy receipts and callers are unaffected.

### 3. The finalization sweeper is started, and a seal is a real finalization
`ExamFinalizationSweeper` runs from `exam_main.lifespan` (like the other loops). A seal
re-reads and re-decides inside its own transaction and writes the status, a
`submit_receipt` over the last acknowledged drafts, `finalization_reason`/`finalized_by`,
an `attempt_finalized` outbox row (the existing grading trigger; `attempt_expired` is kept
for observers) and the audit event. Several instances may run it: the loser of any race
finds the attempt already sealed, or still inside an extended deadline, and does nothing.
Scope: released/paused/closed runs plus runs ended within
`EXAM_FINALIZATION_ENDED_RUN_WINDOW_HOURS`, found with single-field queries (no new
composite index). It is **on by default** (`EXAM_FINALIZATION_SWEEP_ENABLED`) — a deliberate
exception to "flags off by default", because the off state is the defect. The flag is a
rollback lever; the exam API logs a warning when it is false.

### 4. Time extension is serialised with expiry
`decide_time_extension` allows an extension only for a live attempt (IN_PROGRESS/BLOCKED)
whose deadline has **not** passed, on a run that has not ended, within a cumulative cap of
24 h. After the deadline — even before the sweeper has sealed it — an extension is rejected
(`deadline_passed`): otherwise the outcome would depend on which writer ran first. Because
both writers decide inside transactions against fresh state, exactly one of "extended" or
"sealed" wins. An optional idempotency key makes a client retry harmless; without one the
legacy behavior (each call adds minutes) is preserved.

### 5. New commands are narrow
- `terminate`: seals one live attempt now (same EXPIRED + receipt + grading trigger as an
  expiry); a reason is required; never reopens.
- `recover`: re-enqueues the grading trigger for an already-sealed attempt; never reopens
  or edits drafts/receipts (the downstream grading is idempotent).
- `access/grant` / `access/revoke`: add/remove one student from `enrolled_uids` (grant checks
  institution membership via the existing scope helper). Revoke is refused once any attempt
  exists — terminate instead — so a revocation cannot strand or erase work. They do not bump
  `run.version`, so an enrollment edit never invalidates a faculty member's in-flight pause/close.
- `finalize-expired`: one bounded sweep for a run, the same code as the background loop.
- Per-student pause/resume is **not** implemented (decision D-12): freezing one student's
  clock needs a deadline-shift rule and collides with the proctoring `BLOCKED` state.

### 6. Closing a cohort stops admissions only
This was already true of the state machine (admission requires RELEASED; submit/draft work
in CLOSED) and is now regression-tested end to end. `close` counts as unresolved only
attempts still inside their own deadline, so an accommodated student keeps their time and
the faculty member must acknowledge it with `force`; attempts already expired are the
sweeper's job.

### 7. Publication is derived and frozen
`GET …/publish-preview` and `publish` with `blueprint_id` compute total marks, counts per
item type, difficulty distribution, languages and target minutes from the blueprint and its
**approved** candidates. Blockers (a slot without an approved candidate, non-contiguous or
duplicate slot indexes, more than 500 slots, non-zero negative marks while the flag is off)
refuse the publish with `422 publication_blocked` and write nothing; warnings (short pool,
coding slot without languages, unrecognised difficulty, targets above the duration) do not.
The revision freezes the slot specs, approved candidate ids and source references.
Revisions are written with create-only semantics; a run keeps its `revision_id`, so later
approvals/rejections or a newer revision never affect it. The legacy `slot_count` publish
path is unchanged. A retried publish with the same key returns the same revision.
The candidate list is read just before the publish transaction, so a candidate changed in
that window is frozen as it was read — the revision is still internally consistent.

### 8. Policy and schedule
`ExamPolicy` gains optional `timezone` (IANA, **display only**), `scheduled_start`/
`scheduled_end` (tz-aware, normalised to UTC, end after start) and `variant_policy`
(`unique` default / `balanced_reuse`, honoured by reserve-variants). Enforcement is always
UTC. With `EXAM_SCHEDULE_AUTOMATION_ENABLED` (default off) the sweeper releases a PUBLISHED
run when its window opens and closes RELEASED/PAUSED runs when it ends, through the ordinary
`execute` path with deterministic keys (`schedule-release`, `schedule-close`), the run's
current version and the system actor — so a faculty member acting at the same time, or two
instances, cannot double-apply. It never opens a window that has already ended, and `close`
never cuts off an admitted student.

### 9. Negative marking is schema only (decision D-8)
Blueprint slots gain `negative_marks` (≥ 0, ≤ marks, default 0), previewed and frozen into
the revision. Grading does **not** apply it. To make "validated but ignored" impossible,
non-zero values are rejected at blueprint creation and block publication unless
`EXAM_NEGATIVE_MARKING_ENABLED` is set (default off).

## Consequences

- Expired attempts are now sealed, receipted, audited and graded without anyone clicking.
  Operators see new background reads: the sweeper reads every IN_PROGRESS/BLOCKED attempt of
  each active run every interval (`EXAM_FINALIZATION_SWEEP_INTERVAL_SECONDS`, default 15 s).
  A composite index on (`status`, `deadline`) would narrow it; it was not added because the
  query would fail until the index is deployed.
- Behavior changes visible to teachers: an extension after the deadline now fails (409);
  `close` no longer asks for `force` when only expired attempts remain.
- All new persisted fields are optional and omitted when unset; old documents and clients
  keep working (compat §8). Rolling back to the previous build leaves the extra fields in
  place; `ExamPolicy` is `extra="forbid"`, so a *previous* build cannot read a run whose
  policy carries the new fields (compat §4) — do not roll back after such runs exist.

## Not verified

Everything above is unit- and HTTP-level against an in-memory Firestore with optimistic
transactions and thread jitter (`backend/tests/exam_inmemory.py`). Real Firestore
transaction retry/contention behavior, query cost at scale, and live websocket fan-out from
the sweeper (best effort, same process only) have not been exercised; that is Phase 11's
emulator/staging work. No frontend changed.
