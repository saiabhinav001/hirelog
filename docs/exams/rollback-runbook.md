# Exams Rollback Runbook

Three distinct rollback levers exist for the exams module, from narrowest to
broadest blast radius. Prefer the narrowest one that actually addresses the
problem — **never delete exam data to roll back** (Firestore documents for
definitions/revisions/runs/attempts/drafts/receipts/outbox entries are never
deleted by any rollback procedure in this system; see
[backup-dr.md](../runbooks/backup-dr.md)).

## 1. Per-run pause (narrowest — a single run's content/engine is suspect)

Faculty-issued command (Prompt 3), already exists:

```http
POST /api/exams/v1/faculty/runs/{run_id}/commands/pause
{"expected_version": <current version>, "idempotency_key": "<unique>"}
```

Transitions that one run to `RunStatus.PAUSED`. In-progress attempts on that
run stop advancing (the student-facing routes gate on the run's status);
other runs and the module as a whole are completely unaffected. Resume with
the analogous `release` command once the issue is resolved (this is the
"explicitly pause affected runs" half of contract #12).

Use when: a specific run's content is defective, a proctoring/judge issue is
isolated to one run, or you need to freeze one run while investigating.

## 2. New-admissions-pause (global, preserves in-progress students)

New this phase (Prompt 11, ADR 0016). Set:

```bash
EXAM_NEW_ADMISSIONS_PAUSED=true
```

and redeploy/restart the exam API process (reads from `Settings` at process
start via `pydantic-settings`'s env-file loading — not a live-reloadable
Firestore flag, so this requires a redeploy or restart to take effect, same
as `EXAMS_ENABLED` itself).

Effect: `ExamStudentService.admit()` blocks every **brand-new**
NOT_STARTED -> ADMITTED transition, across **every** run, with HTTP 403
`{"code": "new_admissions_paused"}`. Students who were already admitted or
in-progress **before** the flag was set are completely unaffected — calling
`admit()` again (e.g. a waiting-room page refresh) still returns their
existing attempt via the pre-existing idempotent-return path. This is the
"disable new admissions while preserving safe completion" half of contract
#12, and the gap this phase's code change closed (see
[production-qualification-report.md §2](production-qualification-report.md)).

Use when: you need to stop a potential incident from growing (e.g. a
suspected exploit in the admission path, a capacity problem, a provider
outage you don't want new students walking into) while every student already
mid-exam finishes safely and normally.

Reverting: set `EXAM_NEW_ADMISSIONS_PAUSED=false` and redeploy/restart.
Students who were blocked from admitting can retry once reverted.

## 3. Module-wide disable (broadest — the exam engine itself is unsafe)

```bash
EXAMS_ENABLED=false
```

Every `/api/exams/v1/*` route (including already-admitted students trying to
continue) returns 503. This is the blunt instrument — it does **not**
preserve safe completion for in-progress students, so prefer lever 2 above
unless the engine itself (not just new admissions) is unsafe to keep serving.

Use when: a defect affects the state machine, grading, or data integrity
itself — something that would make it unsafe to let anyone, including
already-admitted students, keep interacting with the system.

Reverting: set `EXAMS_ENABLED=true` and redeploy/restart. Students who were
mid-exam when this was flipped resume exactly where their server-persisted
`Attempt`/`Draft` state left off — nothing is lost (Firestore is the
authoritative store; see backup-dr.md).

## What rollback never does

- **Never deletes** any exam Firestore document (definitions, revisions,
  runs, attempts, drafts, command receipts, outbox entries, gradebook
  records, evidence metadata). Rollback is about traffic/admission control,
  not data destruction.
- **Never bypasses** the transactional outbox or idempotency guarantees —
  reverting a pause does not replay or duplicate anything; students simply
  resume making requests against their existing, unchanged server state.
- **Never auto-extends deadlines.** A paused run's or paused-admissions
  student's deadline is a separate, faculty-controlled accommodation
  (`manual_extension_minutes`) — rollback does not implicitly grant extra
  time; decide that explicitly per the incident.

## Decision guide

| Symptom | Lever |
|---|---|
| One run's content/questions are wrong | 1. Per-run pause |
| Suspected incident in the admission path, capacity problem, or an upstream provider (judge/generation/malware-scan) outage — want to stop new students walking in while protecting everyone already in | 2. `EXAM_NEW_ADMISSIONS_PAUSED=true` |
| State-machine/grading/data-integrity defect — unsafe for ANYONE to keep using the engine | 3. `EXAMS_ENABLED=false` |
