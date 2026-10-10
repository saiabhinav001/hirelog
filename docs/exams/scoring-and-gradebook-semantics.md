# Scoring and gradebook semantics

Exact definitions used by the gradebook, the exports and the student results page. Code: `backend/app/domain/gradebook/` and
`backend/app/domain/judge/grading.py`. Tests: `test_gradebook_*`, `test_judge_choice_grading_p10.py`, `test_gradebook_routes_p10.py`.
Open policy choices are listed at the end (decision D-8).

## 1. Marks per question

| Item type | How a question is scored | Maximum |
|---|---|---|
| Single choice | All or nothing: the selected key must equal the answer key | the slot's marks |
| Multiple select | All or nothing: the selected **set** of keys must equal the answer-key set. Order and repeats are irrelevant; keys are case-sensitive; one missing or one extra option scores 0 | the slot's marks |
| Short answer | A human reviewer enters a score, `0 <= score <= marks` (refused with `score_out_of_range` otherwise). Until reviewed the question is **pending**, never zero | the slot's marks |
| Coding | Sum of the weights of passed tests (weighted test groups); a group with an infrastructure error is excluded from both sides until it is re-run. No result yet is **pending**, never zero | sum of the weights |

- **No partial credit** for choice questions and **no negative marking**: a wrong answer scores 0. `negative_marks` on a blueprint slot is stored but **not applied** (D-8).
- A blank answer scores 0 and is reported as **blank**, not as a failure.
- An answer key that is missing or empty is never correct.
- The original automatic result is never edited. A regrade is a new version (`version + 1`, `graded_by`, `regrade_reason`); a re-reviewed short answer keeps the earlier score, notes, reviewer and time in `history`.

## 2. Outcome of one question for one student

Exactly one of:

| Outcome | Meaning |
|---|---|
| `blank` | No answer was saved for the question |
| `pending` | An answer exists but its grading is not finished (no grading record, an unreviewed short answer) |
| `fully_solved` | Graded, and score >= maximum (a question whose maximum is 0 also counts as fully solved) |
| `partially_solved` | Graded and score < maximum — **including a score of 0 for an answered question** (answered but not full marks) |

## 3. Counts per question (`/faculty/gradebook/runs/{run}/counts`)

Over every student **enrolled in the run who has a variant assigned for that question** (the same students are in every column):

| Count | Definition |
|---|---|
| `assigned` | Students with a variant for the question |
| `presented` | Students who reached the question: the attempt has started and the question is at or before their furthest released question, or they saved a draft for it. A student who never started is **not** presented |
| `finalized` | Students whose attempt is submitted, expired or finalized (an attempt-level fact repeated on every question) |
| `nonblank_attempted` | Students whose outcome is not `blank` |
| `fully_solved`, `partially_solved`, `blank`, `pending` | Students in that outcome. They always add up to `assigned` |

## 4. Totals for one student

`raw_score` is the sum of the question scores and `raw_max_score` the sum of the question maxima, over the questions the student was assigned. Pending
questions contribute 0 to the score and their maximum to the maximum, so a total shown while grading is unfinished is a **lower bound**.
The total is never reduced for integrity reasons: **integrity disposition is separate from the grade** (`clean`, `blocked_pending_review`, `blocked_upheld`, `blocked_resolved_continued`):
`blocked_upheld` means the counted incidents reached the run's block threshold, `blocked_resolved_continued` that a reviewer allowed the attempt to continue (or an appeal was overturned).
Fewer counted incidents than the threshold is `clean` here (the incidents remain in the ledger and in `warning_count`).
Similarity matches and any AI-authorship signal are reviewer evidence only: they never change a score, and no student route returns them.

## 5. Defective questions

A teacher resolves a question once for everyone, with a reason: `full_credit_all` (every assigned student gets the maximum, including blank answers, and the outcome becomes `fully_solved`)
or `exclude_from_total` (score and maximum both become 0). The original grading records are untouched. If several resolutions exist for a question the **latest by time** applies; all stay on record.

## 6. Releases

- A release freezes, for every enrolled student, the row they will see. Students see **only the latest release's frozen row**, never the live gradebook: a regrade, a re-review or a defective-question resolution
  changes the teacher's view at once and reaches students only when a new version is released.
- Version 1 needs no reason. **Every later version needs a reason** (`release_reason_required`) and is audited (`exam_results_released`).
- Releasing while the run has not ended, attempts are in progress or blocked, or answers are pending needs an explicit acknowledgement (`release_incomplete`); the release records how many were pending and live.
- A student enrolled after a release has no row in it and sees "not released" until the next release.
- A student's view omits internal grading provenance (submission hash, runtime image, hidden test-suite version).
- Releases made before frozen rows existed (no `snapshot_id`) are still served live, as before.

## 7. Grade disputes

Only against a released result, only for a question in the student's own released row, one open dispute per question, at most 10 per student per run (`grade_appeal_not_allowed` otherwise).
Resolving a dispute changes no grade by itself: a granted dispute is followed by an explicit regrade and a new release.

## 8. Exports

CSV or JSON, produced for a run the caller manages, single-use download tickets bound to the caller, access logged. The content is deleted after `EXAM_EXPORT_RETENTION_HOURS` (default 24); the job record stays.
Every CSV cell is neutralised against spreadsheet formulas: a value that starts with `= + - @`, tab, CR, LF, a full-width look-alike, or one of those after leading spaces / zero-width characters, is prefixed with `'`.

## Open decisions (D-8)

Partial credit for multiple select, negative marking, rounding, and the institution's official definitions of "attempted" and "solved" are not decided; the rules above are what the code does today.
