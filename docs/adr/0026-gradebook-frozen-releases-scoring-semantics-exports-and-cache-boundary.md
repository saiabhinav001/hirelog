# ADR 0026 — Gradebook: frozen releases with lineage, exact scoring semantics, expiring formula-safe exports, and an exam-scoped cache boundary

**Status:** Accepted

## Context

Prompt 10 asks for a scoped gradebook, released results, regrade lineage, accessibility and exam-scoped PWA boundaries: exact grade-count and scoring
semantics; original submissions and result versions preserved, a correction creating a new version with reason and audit; similarity as reviewer evidence only;
authenticated, scoped, expiring exports protected against formula injection; a PWA that caches only the static shell, with authenticated pages and data network-only
and `no-store` at the service worker, framework, CDN and API layers; no offline final submission; service-worker updates that never interrupt an active attempt.

The traceability audit rated GRD-02..06 verified and GRD-01, PWA-01, A11Y-01 partial. Exercising the routes over HTTP with the real repositories, and the
service worker and hook in a mocked browser, found that several "verified" rows did not hold:

1. **Students saw the live gradebook, not a release.** `GET /student/gradebook/runs/{run}/results` only checked that *some* release existed and then computed the row from current data, so a regrade, a re-review
   or a defective-question resolution reached students instantly, unreleased and unaudited, and "version" described nothing. A release could also be made while attempts were running or answers ungraded.
2. **Multiple select was never graded correctly.** The exam UI sends a list; the grader compared `str(list)` with `"A,C"`, so every multiple-select answer was wrong.
3. **Counts were wrong or non-deterministic.** A student who never started counted as having been *presented* question 1; with several defective resolutions for one question the one applied depended on store order;
   a short-answer score could exceed the maximum; the integrity label said "blocked, upheld" for any counted incident, even one far below the block threshold.
4. **CSV injection was only partly handled** (first character `= + - @`; not TAB / CR / LF, look-alikes or leading whitespace), and **exports never expired**: the content stayed in storage (or process memory) indefinitely.
5. **Grade disputes** could be filed before release, for any string as a question, without limit, and concurrently; a short-answer re-review overwrote the earlier score in place.
6. **Update suppression did not work.** The exam layout mounts one `useExamServiceWorker()` (no attempt knowledge) and the exam page a second one with `attemptActive`; each instance kept its own flag, so the layout instance released the
   waiting worker and the controller change reloaded the page in the middle of an attempt. A page refreshed mid-attempt had the same window while the attempt was still loading.
7. **Next.js had no `no-store` for `/exams/**`.**

## Decisions

### 1. A release freezes what students see
`release_results` copies every enrolled student's row into `gradebook_release_rows/{snapshot_id}__{uid}` (a fresh snapshot id per release, never rewritten), then creates the release record `{run}-v{n}` with `create_if_absent`
(a concurrent release retries with the next number). Students are served the latest release's frozen row, without internal provenance. Version 2+ requires a reason; an incomplete release (run open, live attempts, pending answers)
requires acknowledgement and records the counts; every release is audited. Releases without a `snapshot_id` (made earlier) are served live as before. Alternative rejected: freezing only a version number and recomputing — that is the defect.

### 2. Scoring semantics are written down and enforced
All-or-nothing exact-set grading for choice questions; no partial credit and no negative marking (D-8 open); short-answer scores bounded to `0..marks`; a re-review keeps its history; the latest defective resolution wins; `presented` requires a started attempt;
the integrity label follows the block threshold and the reviewer's decision. See [../exams/scoring-and-gradebook-semantics.md](../exams/scoring-and-gradebook-semantics.md).

### 3. Exports expire and are formula-safe
`ExportJob.expires_at` (`EXAM_EXPORT_RETENTION_HOURS`, default 24) and `expired_at`. A sweep (also running without a bucket, because the content then sits in memory) deletes the bytes first and records it second; tickets are refused for an expired job, and a ticket
issued earlier cannot download after expiry (`export_expired`). `sanitize_csv_cell` covers the OWASP set, LF, full-width look-alikes and leading whitespace / zero-width characters.

### 4. Grade disputes are bounded
Released result, a question in the student's own released row, one open dispute per question, at most 10 per student, an id derived from the prior count so simultaneous submissions collide on one document. Resolution is audited. Not done: an append-only
decision history for disputes like proctoring appeals (reversal still bumps `version` and overwrites the notes).

### 5. One attempt lock for the whole service-worker lifecycle
The hook keeps a module-level count of components holding an attempt open. While it is above zero no waiting worker is released and no controller-change reload happens; the last release lets a waiting update through. The exam page holds while the
attempt is unknown (loading), `in_progress` or `blocked`; the blocked page holds. The worker itself is unchanged (it never calls `skipWaiting` on its own and caches only four static files).

### 6. Cache boundary, layer by layer
Service worker: never intercepts API or page requests except the offline fallback on a failed navigation; never writes a runtime cache (tested against the shipped file). Framework: `Cache-Control: private, no-store` for `/exams/:path*` in `next.config.ts` (verified on a production
build; the dev server rewrites it). API: `no-store` on every exam response (existing). CDN: no repository-visible configuration; the framework header is what a CDN honours.

## Consequences

- Students no longer see anything that was not released; teachers see changes immediately and must release a corrected version with a reason.
- Multiple-select answers that were previously all zero are graded correctly **for attempts finalized from now on**; already-stored wrong results are not rewritten (regrade through the existing regrade routes if needed).
- Rollback: the new fields are additive (`snapshot_id`, `row_count`, `pending_question_count`, `live_attempt_count`, `acknowledged_incomplete`, `release_version`, `expires_at`, `expired_at`, `history`) and one new private collection (`gradebook_release_rows`, covered by the default-deny rule); old code ignores them and old releases keep working. Reverting
  the code returns students to the live view.
- A release now costs one write per enrolled student.

## Not done / residual

Real browser, screen reader, zoom, device and managed-client evidence (jsdom axe cannot check contrast, reflow or reading order); a CDN in front of the app; a lockdown profile (a PWA alone is not lockdown — a managed client would be a separate, separately tested profile);
D-8; an append-only dispute history; export content in process memory when no bucket is configured (single instance only); orphaned snapshot rows when two releases collide.
