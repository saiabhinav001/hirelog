# ADR 0014 — Gradebook, similarity review, and learning feedback

**Status:** Accepted

## Context

Prompts 3–8 already produce every raw ingredient this phase needed:
`Attempt`/`Draft` (Prompt 3/4), `GenerationCandidate`/`VariantAssignmentRepository`
(Prompt 5), `GradingResult`/`McqGradeResult`/`ShortAnswerReview` (Prompt 6),
and `ProctoringState`/`Appeal` (Prompt 8). Nothing in this phase re-grades a
submission from scratch — it reads those already-committed results across
domains and assembles them into a per-student, per-question gradebook,
plus three adjacent workflows the prompt bundles with it: a JPlag-style
similarity review, scoped CSV/JSON export, and deterministic learning
feedback.

One real gap was found and closed before any gradebook code could be
written: `McqGradeResult` (Prompt 6) had no `id` or `version` field, and
`McqResultRepository.save()` silently **overwrote** the single doc for a
given `(run_id, uid, question_id)` on every call — there was no way to
regrade an MCQ question without destroying the original automatic result,
breaking the "auditable lineage, never overwrite" contract this phase
depends on. Fixed additively: `McqGradeResult` gained `id`/`version`/
`graded_by`/`regrade_reason` (mirroring `GradingResult`/`ShortAnswerReview`
exactly), `McqResultRepository.save` became `.create`/`.get`/
`.latest_for_question`/`.list_for_attempt` (each version is its own
document), and `JudgeService.regrade_mcq` was added.

Committed on `exams/phase-9-gradebook-similarity-feedback`, stacked on top
of `exams/phase-8-proctoring-evidence` (current tip of the branch stack;
`main` remains untouched throughout).

## Decisions

### 1. Counts are defined exactly as named, computed by one pure function

`compute_gradebook_counts` (`app/domain/gradebook/decisions.py`) produces
`assigned`/`presented`/`finalized`/`nonblank_attempted`/`fully_solved`/
`partially_solved`/`blank`/`pending` — the exact eight terms the prompt
names, nothing invented, nothing merged. Two required interpretation
choices, both documented in code comments and here:
- **`presented`** = `slot_index <= attempt.current_slot_index` OR a draft
  exists for that question — forward-only progression (Prompt 7) makes the
  slot-index check alone reliable; the draft-existence check is a defensive
  fallback for edge cases, not the primary signal.
- **`finalized`** is attempt-level (`attempt.status` in
  `RESOLVED_ATTEMPT_STATUSES`), cross-referenced per question — a question
  isn't itself "finalized", the student's overall attempt is.

### 2. Four-state outcome classification, one pure function, every item type

`classify_question_outcome` maps `(has_nonblank_answer, grading_available,
weighted_score, max_score)` to exactly one of `blank`/`pending`/
`partially_solved`/`fully_solved`. `grading_available` means "a completed
grading record exists": a `GradingResult` for coding, an `McqGradeResult`
for MCQ, a `ShortAnswerReview` with `status=REVIEWED` for short answer. Its
absence — a submission stuck in `INFRA_ERROR` retry, an unreviewed short
answer — is always `pending`, **never a silent zero** (contract: "never a
silent zero score"). A nonblank answer scored exactly 0 is `partially_solved`
(0 is the low end of the partial range), not a separate "wrong" category —
the prompt names only four content outcomes, so an incorrect-but-answered
MCQ has nowhere else to go.

### 3. Per-student-per-question resolution walks the SAME seams Prompt 5/6/7 already reserved

`GradebookAggregationService.build_student_row` reconstructs
`question_id = f"{revision.id}:slot:{index}"` for every slot (exactly as
`student_service.get_current_question` does), resolves the assigned variant
via the existing `VariantAssignmentRepository`, and reads whichever
judge-domain result type matches the candidate's `item_type`. No new
student-facing content-delivery logic was added — this phase only reads
what Prompts 5–7 already deliver and grade.

### 4. Regrades are already-versioned; the gradebook always reads the latest

`GradingResultRepository.list_latest_for_attempt`,
`McqResultRepository.list_for_attempt`, and
`ShortAnswerReviewRepository.list_latest_for_attempt` (all new, mirroring
each other) fetch every version for an attempt and keep only the
highest-`version` doc per question — one query per result type per student
instead of one query per question. A regrade (via the pre-existing coding/
short-answer regrade endpoints, or the new `JudgeService.regrade_mcq`)
therefore flows into the gradebook automatically on next read; the
original version is never deleted or edited.

### 5. Defective-question resolution is a read-time transform, not N edits

`DefectiveQuestionResolution` (`full_credit_all` | `exclude_from_total`) is
one auditable record per question, applied by
`apply_defective_question_resolution` **at aggregation read time** — it
never touches any student's underlying `GradingResult`/`McqGradeResult`/
`ShortAnswerReview`. `exclude_from_total` zeroes both `weighted_score` and
`max_score` for that question (true exclusion from both sides of the
fraction, not a zero score); `full_credit_all` sets `weighted_score` to
`max_score`. One resolution record, uniformly applied to every assigned
student — never a per-student patch.

### 6. Grade appeals are a new, separate model from proctoring's `Appeal`

`GradeAppeal` disputes a specific question's score; Prompt 8's `Appeal`
disputes a BLOCKED-attempt decision — different subject, different
lifecycle, deliberately not unified. `GradebookReleaseService.resolve_grade_appeal`
mirrors `AppealsService.resolve_appeal` exactly: resolving a `PENDING`
appeal for the first time does not bump `version`; reversing an
already-resolved appeal does. Resolution is bookkeeping only — same as
proctoring's `resolve_block`, it never itself changes a grade. If the
appeal is granted, faculty separately triggers the appropriate regrade
(existing coding/short-answer endpoints, or the new MCQ regrade endpoint)
using the same explicit-and-separate pattern already established in ADR
0013 for block resolution vs. appeal resolution.

### 7. Release is its own version sequence, independent of any one grade's version

`ReleaseRecord.version` (`decide_next_release_version`) is a run-wide
release counter — v1 the first release, v2+ for every subsequent
re-release (typically after a regrade/defective-question fix). Students
can only ever see the CURRENT gradebook read, gated by "does at least one
release exist for this run" (`ResultsNotReleasedError`, 403) — there is no
per-question "released" flag and no way to see a stale frozen snapshot of
a past version; a corrected regrade is visible to students the moment it's
computed, as long as a release already exists.

### 8. Similarity: real Protocol, honest fake, honest untested real adapter

`SimilarityProvider.compare(submissions)` mirrors the exact honesty tier as
`JudgeProvider`/`Judge0HttpAdapter` (ADR 0011): `DeterministicFakeSimilarityProvider`
never performs real comparison (every result explicitly scripted by the
caller — a "smart" fake that approximated real similarity would
misrepresent what's actually validated here); `JPlagCliAdapter` raises
`NotImplementedError` until an operator configures a real `EXAM_JPLAG_JAR_PATH`
**and** separately validates the parsed-output format against a real JPlag
jar (no JPlag/Java installation exists in this environment — this setting
alone does not create or validate one). `SimilarityService.run_for_question`
groups by `(question_id, language)`, strips instructor starter code via
`strip_starter_code` (exact-substring removal, conservative by design) before
ever handing text to the provider, skips non-coding items and lone
submissions, and persists matches via `replace_for_question` (re-running
supersedes prior matches for that question entirely). Matches are exposed
only on faculty-only routes with an explicit disclaimer string on every
response: never an automatic misconduct finding, never a score penalty.
`GenerationCandidate.starter_code` (new, optional, empty-dict default) is
the field this exclusion reads — no existing authoring path populates it
yet, so the exclusion is a fully-built, fully-tested mechanism that is
currently a no-op in practice until Prompt 5's authoring flow or the code
editor starts setting it.

### 9. Export: job-status state machine today, real queue-ready contract tomorrow

`ExportJob` (`PENDING -> RUNNING -> SUCCEEDED/FAILED`) is a real state
machine, but "async" in this codebase means "a route calls `create_job`
then `run_job` back-to-back" — there is no task-queue infrastructure
anywhere in this repository (same honest framing as the outbox drain loop
and finalization sweeper). Swapping in a real queue later only changes WHO
calls `run_job` and WHEN; the contract (job doc, storage ref, row count,
error) does not change. Every CSV cell passes through `sanitize_csv_cell`:
a leading `=`, `+`, `-`, or `@` is prefixed with `'`, neutralizing Excel/
LibreOffice/Google Sheets formula injection while leaving the visible value
unchanged (OWASP CSV-injection guidance) — verified end-to-end through
`csv.DictWriter`, not just at the sanitizer-function level. Downloads reuse
the exact single-use, short-TTL, transaction-consumed ticket pattern as
evidence view tickets (ADR 0013), with every successful download recorded
to `ExportAccessLogEntry`. Export blob storage reuses proctoring's
`EvidenceStorage` Protocol/adapters directly (the interface has nothing
evidence-specific about it) under a different key prefix
(`exam-exports/...` vs. `exam-evidence/...`) — no new storage abstraction.

### 10. Learning feedback is template-based, not a live model call, and post-release only

`build_learning_feedback` is a pure function: fully-solved questions become
strengths; partially-solved/blank questions become gaps, each carrying a
`FailureCategory`-keyed hint (e.g. `time_limit_exceeded` → "consider a more
efficient algorithm") and the question's own `supporting_anchors` (the
SAME faculty-approved KT citations already attached to that candidate at
generation time, Prompt 5 — never a different/broader search over KT
sources); pending questions are silently excluded (grading isn't final, so
there's nothing honest to say yet). `FeedbackService.build_feedback_for_student`
refuses to run at all (`ResultsNotReleasedError`) until a release exists
for the run — feedback can never leak a result students aren't allowed to
see yet. No LLM call, no network request, no model — fully deterministic
and fully unit-testable without mocking (contract: "Optional AI signals
remain disabled until locally evaluated.").

### 11. Legacy search indexing never reads exam data (verified, not assumed)

Grepped `app/services/**` and `app/main.py` for any reference to
`app.domain.*` or any exam/judge/proctoring/generation/kt/gradebook
collection name — zero matches. The exam module and the legacy
search-indexing pipeline are structurally disjoint (separate FastAPI apps,
separate entry points, no shared import), not merely disjoint by
convention.

## Consequences

- **Positive:** 76 new backend tests (bringing the suite to 439/439
  passing) covering every named scenario: weighted coding/MCQ/short-answer
  cases, pending-infra-failure (never a silent zero), MCQ and coding
  regrades (aggregation reads the latest version), similarity matches on
  near-identical submissions, starter-code exclusion, CSV formula-injection
  neutralization end-to-end, the unauthorized-faculty-route gate
  (pre-existing `require_exam_faculty` dependency, now also guarding every
  new gradebook/similarity/export route), and unreleased-result access
  (`ResultsNotReleasedError` on both the student results route and the
  feedback route). Ruff/`compileall` clean. Frontend: a real faculty
  gradebook table (paginated rows, per-question counts, release/defective-
  question/export controls), a dedicated grade-appeals review page, a
  dedicated similarity-review page, and a student results page (score
  breakdown, per-question outcomes, grade-appeal submission, deterministic
  feedback) — 5 new frontend tests (36/36 total), `tsc --noEmit`/`eslint`/
  production `next build` all clean.
- **Negative / follow-up required:** `JPlagCliAdapter` is genuinely
  unimplemented (raises `NotImplementedError`) — no real code-similarity
  detection runs until an operator installs JPlag/Java and configures
  `EXAM_JPLAG_JAR_PATH`; until then, faculty similarity checks return no
  matches, never fabricated ones. `GenerationCandidate.starter_code`
  exclusion is mechanically complete but practically a no-op today (nothing
  populates it). The integrity-disposition heuristic
  (`_integrity_disposition`) cannot distinguish "faculty resolved a block
  via allow-to-continue with no student appeal ever filed" from "block
  still counted and never separately cleared" — both conservatively show as
  `blocked_upheld` rather than `blocked_resolved_continued`, because
  `resolve_block` (ADR 0013) records its resolution as free text, not a
  structured field; documented rather than silently guessed at. The
  gradebook/export/similarity services are tested only against in-memory
  fakes (the same `Service.__new__` + fake-repo pattern as every prior
  phase), not a real Firestore/emulator run — the same documented
  limitation as ADRs 0008/0010/0012/0013. Export "async" is a same-request
  job today, not a real background queue — a large run's export runs
  synchronously inside the HTTP request that creates it. No coding-specific
  "manual grade override" exists (a coding appeal that's granted still
  requires an actual re-grade against corrected tests/rubric, via the
  pre-existing regrade endpoint — there is deliberately no "just set the
  score to X" path for coding, unlike MCQ/short-answer).

## Migration / configuration steps

- New settings, safe defaults: `EXAM_EXPORT_DOWNLOAD_TICKET_TTL_SECONDS`
  (300), `EXAM_JPLAG_JAR_PATH` (unset — falls back to the deterministic
  fake similarity provider), `EXAM_JPLAG_JAVA_EXECUTABLE` (`java`). Export
  storage reuses `EXAM_EVIDENCE_STORAGE_BUCKET` (no new bucket setting).
- New Firestore collections (`gradebook_releases`,
  `gradebook_defective_questions`, `gradebook_grade_appeals`,
  `similarity_matches`, `gradebook_export_jobs`, `gradebook_export_tickets`,
  `gradebook_export_access_log`) — no rules changes needed (default-deny
  catch-all).
- Additive model changes only: `McqGradeResult` gained `id`/`version`/
  `graded_by`/`regrade_reason`; `GenerationCandidate` gained `starter_code`
  (defaults to `{}` — every existing candidate is unaffected).
- No new background threads — export/similarity work runs synchronously
  within the request that triggers it (see Decision 9).
- No new frontend env vars.
- No data migration.

## Next-phase prerequisites

- Prompt 10 (PWA/lockdown) inherits a fully closed grading→gradebook→
  release→feedback loop; a managed-lockdown profile changes proctoring
  signal strength (ADR 0013), not anything in this phase's read path.
- A real JPlag installation and a real Firestore/emulator validation run
  remain the two biggest "honestly incomplete" items carried forward from
  this phase, consistent with every adapter/transactional-shell caveat in
  ADRs 0008–0013.
