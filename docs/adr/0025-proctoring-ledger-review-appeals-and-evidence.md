# ADR 0025 — Proctoring: a ledger that never mis-blocks, audited review, append-only appeals, evidence that is real or honestly missing

**Status:** Accepted

## Context

Prompt 9 asks for an institution-approved proctoring profile and an append-only incident ledger; browser signals treated as
client-reported observations (deduplicated, with grace and cooldown, technical failures informational); a transactional block that
stops answer writes and question delivery server-side while preserving drafts, with an audited review / recovery / appeal route
and a ledger that is never reset; private evidence with short-lived tickets, access audit and retention across originals and
derivatives; submission that never depends on evidence; and no face recognition, biometrics or automatic misconduct decisions.

The traceability audit rated PRC-02..06 and 09 `EXISTS_AND_VERIFIED` (pure decision tests and in-memory fakes with pass-through
transactions), PRC-01 `PARTIALLY_EXISTS` (D-7) and PRC-08 verified "by construction". Exercising the ledger over HTTP, with real
optimistic transactions and concurrent callers, for the first time found that several of those claims did not hold:

1. **A late observation could crash, and a threshold could race a submit.** `record_observation` read the attempt only when the count
   changed. Once an attempt was submitted or expired, a counted departure raised an illegal `submitted → blocked` transition (a 5xx
   to the student) and kept changing the ledger after the attempt had ended.
2. **A reviewer's "allow to continue" was undone by the next trivial event.** Blocking was `count >= threshold` on *every* closed
   incident, counted or not, so a one-second blip right after a review blocked the student again.
3. **The stale-incident sweep could be starved by one state.** For a student who left a tab open and submitted from elsewhere, the sweep
   raised on the illegal transition and aborted, over and over, before reaching any other state.
4. **A review left no authorization audit.** `resolve_block` wrote only a ledger entry that looked like a browser `page_visible`
   observation, the reviewer was named only inside free text, the reason was optional, and a student could read that free text.
5. **Appeals could be set back to *pending*, overwrote the earlier decision in place** (the test titled "without losing history" did not
   check history), could be filed without limit, and were not audited.
6. **Evidence was weaker than the traceability said.** In production without a bucket evidence went to process memory while records said
   `verified`; the "image" was whatever the client labelled; the viewer sniffed; a request whose upload never came stayed `requested`
   forever; retention scanned an arbitrary first 100 documents (so due evidence could never be reached) and deleted evidence that a
   blocked attempt or a pending appeal needed; a view ticket could be issued for a snapshot with no bytes; issuing and refused access were
   not audited; snapshot requests were unbounded.
7. **No profile.** Grace and cooldown were global environment values.
8. The student and teacher screens turned any conflict into a full-page crash, and the camera hook uploaded to `/upload/undefined` when
   the request was refused.

## Decisions

### 1. The ledger counts only live attempts, and blocks only on a newly counted incident
The attempt is read first, inside the same transaction as the decision. If it is not `in_progress` or `blocked`, the event is recorded as a
server entry (`source="server"`, never counted, deterministic id so a replay rewrites it) and nothing else changes; a submit or expiry
that commits first therefore makes the event a late observation, and one that commits later finds the attempt blocked (the transaction
retries on conflict). Blocking requires that an incident was *just counted*, the count has reached the threshold, and it is above the
attempt's `review_floor`. Dedupe, grace and cooldown are unchanged.

### 2. A review keeps the ledger and says who and why
`resolve_block` (reason required, 3+ characters) writes, in one transaction with the state change, a ledger entry with `source="reviewer"`
and `actor_uid`, and an `exam_block_resolved` audit event. "Allow to continue" resumes the attempt on its existing deadline, never
touches a count, and sets `review_floor = count + allowance`. Nothing deletes or edits ledger entries; the repositories have no such
method, and a test pins that. Students see the *decision* ("allowed to continue", "upheld") but not the reviewer's identity or notes.

### 3. A per-run proctoring profile, defaults unchanged
`ExamPolicy.proctoring_profile` (optional): `grace_seconds`, `cooldown_seconds` (0-60), `allowance_after_review` (0-10),
`evidence_retention_days` (7-365), `set_by` (stamped by the server). Unset fields use the deployment defaults, so a run without a profile
behaves as before. The teacher sets it when scheduling a session; who within an institution may do so, and whether an institution must
approve it first, is decision D-7 and is **not** built: `set_by` records provenance only.

### 4. Appeals are append-only and bounded
Filing returns the student's existing pending appeal (one per attempt, decided in a transaction); a decision is `upheld` or `overturned`
only; every decision is appended to `decisions` with the reviewer and reason and audited, so a reversal bumps `version` and keeps what came
before, and two reviewers deciding together lose neither decision. Filing is rate-limited per student. An appeal never changes the
attempt: the block is lifted only by a review, and the UI says so on both sides.

### 5. Evidence is real, private, audited, and honestly missing when it is not
- In production, without `EXAM_EVIDENCE_STORAGE_BUCKET` the snapshot-request, upload, view-ticket and view routes answer
  `503 evidence_storage_unavailable` (recording a snapshot as missing still works) and the retention loop does not start; the in-memory
  store is for development only. A storage failure during upload marks the snapshot
  `missing / storage_unavailable`.
- The bytes decide the type (JPEG, PNG or WebP by signature); anything else is refused and recorded as `missing / not_an_image`. Viewing
  uses the verified type with `nosniff` and `no-store`.
- A view ticket is issued only for verified evidence, is single use and bound to the reviewer, and issuing, viewing and refused attempts
  on a real ticket are all written to the access log (`issued` / `viewed` / `denied`).
- A request still `requested` after its ticket lapsed becomes `missing / upload_not_completed`.
- Snapshot requests and missing-records are rate-limited per attempt.
- Retention scans oldest-first in pages, uses the run's own window, **holds** evidence while the attempt is blocked or an appeal is
  pending, deletes bytes before the record, and one failure does not stop the rest. A snapshot has no derivatives (no thumbnails or exports)
  in this codebase; if one is added it must be deleted in the same place.
- Submission does not import or call the evidence module, and a test proves a submit succeeds while evidence storage is down.

### 6. What this does not do
No face recognition, biometric matching, liveness scoring or automatic misconduct decision exists (a test scans the proctoring package
for them). Signals come from the student's browser and prove nothing on their own; the screens say so.

## Consequences

- New optional fields only: `ExamPolicy.proctoring_profile`, `ProctoringState.review_floor`, `IncidentLogEntry.source/actor_uid`,
  `Appeal.decisions`, `EvidenceAccessLogEntry.outcome`. No new collection and no data migration.
- `POST …/resolve` now requires a reason and `POST …/appeals/{id}/resolve` a reason and a decided status (422 otherwise).
- Rollback: the previous code rejects a policy that contains `proctoring_profile` (it forbids unknown fields), so list runs that set it
  before rolling back below this change. Older code reads the other new fields by ignoring them and would show `issued` / `denied` access
  entries as views.
- Residual risks: signals and their timestamps are reported by the browser and can be wrong or forged, so counts are review aids only; the
  dedupe window is the last 64 event ids, so a replay of an older event is not recognised; nothing here was exercised against a real
  bucket, IAM policy, browser or camera; the camera hook change is not unit-tested; limiters are per instance; access-log entries and
  consumed tickets are not purged; D-7 (who approves a profile, evidence and log retention periods) is open.
