# ADR 0022 — Student attempts: released-slot gating, idempotent drafts and receipt-backed submit

**Status:** Accepted

## Context

Prompt 6 asks that a student fetch only currently released slots of their own active attempt; that draft saves are versioned and
idempotent; that submit is a separate irreversible command with scoped idempotency keys and a durable receipt (an identical retry
returns the same receipt, a changed payload under a reused key conflicts, a lost response is recoverable by receipt lookup without
allowing late work); that concurrent submit / timeout / teacher-close produce one serialized outcome and finalization seals the last
acknowledged draft or blank exactly once; and that nothing authenticated is cached. Prompt 0 rated ATT-01/03/04/05/07 as verified and
ATT-02/06/08 as open (`requirements-traceability.md` §6).

Reading the code against the list, and exercising it over HTTP for the first time (the student routes had no HTTP-level tests; the
"verified" rows rested on pure decision tests), found that three of the "verified" rows were not true:

1. **A retry after a committed submit did not return the receipt.** The service leaves the attempt `SUBMITTED` (grading moves it to
   `FINALIZED` later), but `decide_submit` only answered from the receipt for `FINALIZED`. A student whose submit response was lost and
   who retried got `attempt_not_in_progress`. The unit test that "proved" idempotency had constructed a `FINALIZED` attempt.
2. **A reused key with a different payload was not detected**: the receipt stored no fingerprint of the request.
3. **A draft save was not idempotent**: after a lost response the retry carried the old `expected_version` and was answered with a
   conflict, so the client could not tell whether its own write had landed.

and that the open rows were wider than listed:

4. **Draft routes accepted any question id.** A guessed or future id created a draft document (unbounded count and size), and the
   grading handler then crashed on a draft id that was not a slot id.
5. **The public test run accepted any question id**: a future slot's public tests could be run before release, a malformed id returned
   a 500, and it worked before admission and after sealing.
6. **The seal read draft versions outside its transaction** in submit/finish, the sweeper and faculty terminate, so a draft saved
   between the read and the commit (including the first save of a slot) could be missing from the receipt.
7. There was no way to list released slots (navigator), no mark-for-review flag, and `Attempt.epoch` is set to 1 at admission and never
   changed by anything.

## Decisions

### 1. A slot is shown or written only if it was already released
`resolve_released_slot` maps a client-supplied question id to a slot index: it must be exactly `{revision_id}:slot:{n}` with a
canonical number (no sign, leading zero, or non-ASCII digit) and `n` no greater than the attempt's current slot. A foreign or malformed id is
`question_not_in_run` (404); a well-formed id beyond the current slot is `slot_not_released` (403). It is applied to draft saves, draft
reads, question reads and the judge's public run, and nothing is created for a refused id. Release still only ever advances (the existing
forward-only `next-question`), so "released" means `0..current`.

### 2. Reopening earlier slots is a flag
Writing an earlier released slot's draft was already possible and stays possible (an in-flight autosave must not be lost when the student
moves on). **Reading** an earlier question is new: `GET …/questions/{n}` always serves the current slot, serves earlier ones only when
`EXAM_STUDENT_REVISIT_RELEASED_SLOTS` is on (default off → `403 slot_revisit_disabled`), and never serves a later one.
`GET …/slots` returns navigator data for released slots only (ids, answered, marked, version, locked count) and no content. The
product choice (decision D-3: sequential release with free review of released slots versus strictly forward-only) is left to the
institution; the flag makes the default the existing behavior.

### 3. Draft saves are idempotent when the client says so
A save may carry `client_save_id`. The draft remembers the id and a hash of the latest save; the same id with the same content is
acknowledged again with the same version (`replayed: true`) and no new write, the same id with different content is
`idempotency_key_reused`, and a different id on a stale version is still `draft_version_conflict`. A replay never revives work: it is
evaluated after the state, deadline and epoch checks, so a sealed or late attempt still answers with its own conflict. Saves also accept an
optional `client_epoch` (checked when present), an optional `marked_for_review` (omitted = unchanged), and an answer is capped at
`EXAM_DRAFT_MAX_BYTES` (413).

### 4. Submit is answered from its receipt in every resolved state
`decide_submit` now treats `SUBMITTED`, `EXPIRED` and `FINALIZED` alike: the same key returns the stored receipt even after the
deadline (recovering a lost response without permitting late work); another key is a conflict named for the state
(`already_submitted`, `attempt_expired`, `already_finalized`). The receipt stores a hash of the request (`submit` with these expected
versions, or `finish`); the same key with a different hash is `idempotency_key_reused`. Receipts written before this change have no
hash and are matched by key only.

### 5. The seal reads drafts inside its transaction
`DraftRepository.all_versions_in_transaction` streams the draft collection as part of the caller's transaction and is used by
submit/finish, the finalization sweeper and faculty terminate. Firestore transactions are serializable, so a draft written (or first
created) after that read makes the commit fail and the callback re-run with the new state; the receipt therefore lists exactly the
versions that exist at the instant of sealing, and because a draft save also reads the attempt, no write can land after the seal.
Concurrent submit / terminate / expiry are serialized by the attempt document; exactly one writes the receipt and the grading outbox row.
The local fake (`exam_inmemory.py`) now models query reads (the whole result set must be unchanged at commit). **A real Firestore run of
this interleaving has not been done** (the Prompt 11 staging gate).

### 6. Judge public run and grading
The public run refuses a question id that is not a released slot of the caller's run, and (for starting a run) an attempt that cannot
be worked on any more; polling a result needs only a released slot. The grading handler skips a draft id that is not a slot id instead of
raising.

## Not changed on purpose

- **Epoch.** `Attempt.epoch` stays a fence that only a server-side recovery action could advance; nothing does yet, so it is a constant
  and "stale epoch" is enforced against a value that never moves. A two-tab takeover policy (last tab wins, or concurrent tabs with
  versioned drafts as now) is a product decision; concurrent tabs are already safe because drafts are versioned.
- Run-level pause keeps refusing draft saves and submit (existing behavior).
- Offline: still none. The service worker never intercepts `/api/` and answers only a fixed static list; no answer is stored in the browser.

## Behavior changes

| Before | Now |
|---|---|
| A retry after a committed submit returned `attempt_not_in_progress` | Same key → the same receipt; other key → `already_submitted` |
| Same key, different payload after a seal → ignored/conflict by accident | `idempotency_key_reused` |
| Draft save for any string id succeeded | Only released slots of this run; `404 question_not_in_run` / `403 slot_not_released` |
| Reading a draft of an unknown id returned an empty draft | `404` / `403` as above |
| Public run accepted any id, any time | Released slot of this run; not after sealing |
| Draft save retried after a lost response → conflict | Same `client_save_id` → acknowledged |
| Seal read drafts outside its transaction | Inside it |

## Residual risks

- Real-Firestore behavior of the transactional query read, contention at a synchronized submit burst, and index needs are unverified.
- Grading reads the current draft for each slot at grading time rather than a sealed copy; safe only because no draft can be written after
  the seal (tested, but a future write path would break it).
- The draft count per attempt is now bounded by the slot count; the 100 KB per-answer cap is a default, not a measured limit.
- `epoch` is not a working fence (above).

## Test evidence

Backend `EXAMS_ENABLED=false`: 1271 passed (1222 before), `ruff check .` clean. New `test_exams_student_attempts_p6.py` (HTTP +
in-memory Firestore with optimistic transactions): double click, two tabs, stale autosave, response loss and late retry, reused key,
stale epoch, guessed / malformed / future ids, direct calls by non-enrolled or non-admitted students, thread races (two saves,
submit vs terminate, submit vs sweeper, finish vs first save), a deterministic read-then-commit interleaving, a sweeper that crashes
mid-seal, expiry sealing a draft or blank once, no-store on every student route, and a marker-based leak check across every student
response and live event payload. Frontend: `tsc`/`eslint` clean; the exam page retries a failed autosave once with the same save id.
