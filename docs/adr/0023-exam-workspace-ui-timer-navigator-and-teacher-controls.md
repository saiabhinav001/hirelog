# ADR 0023 — Exam workspace UI: server-anchored timer, navigator, teacher controls and short links

**Status:** Accepted

## Context

Prompt 7 asks for a timer that keeps counting down from the server's deadline and not the browser's wall clock, time-left warnings that
are announced once and are accessible, a numbered navigator with answered / unanswered / marked / locked counts, previous and next
across released questions, mark-for-review, readable light and dark themes, and the teacher controls that were still missing (schedule
and scoring fields, a short join link, per-student access and attempt controls, conflict feedback, and an honest "how fresh is this").
Reading the existing screens found:

1. **The timer did not tick.** `ExamShellHeader` computed `deadline − Date.now()` at render; nothing re-rendered it, and a student whose
   device clock was wrong saw a wrong time left. There was no server-time sample at all.
2. **No time warnings, no previous question, no navigator, no mark-for-review control.** The P6 backend already offers the data
   (`GET …/slots`, `GET …/questions/{n}`, `marked_for_review`, `client_save_id`); nothing used it.
3. **Teacher actions that exist on the server had no screen**: per-student extend/terminate/recover, grant/revoke access. Run-level
   commands showed raw server errors, and "Snapshot as of" was the browser's fetch time.
4. **Contrast.** The shared dark theme's muted text (`#71717a` on `#111113`) is about 3.9:1, below the 4.5:1 WCAG AA minimum.

## Decisions

### 1. The clock is the server's
Student state, the released-slot list, the faculty run and the faculty attempt listing carry `server_time`. The browser keeps one
offset, `server_time − performance.now()` (monotonic, so a device clock change mid-exam does not move it), refreshed on every sample.
Time left is `deadline − (performance.now() + offset)`, re-rendered once a second. The deadline stays the server's: the screen only
displays it, and nothing the browser computes can extend or end an attempt. When the display reaches zero the workspace locks editing
and says so, polls the server for the sealed state and goes to the receipt; **it never submits by itself** - the sweeper seals the
attempt, as before. Accuracy is limited to one network round trip.

### 2. Warnings are policy data
`ExamPolicy.time_warning_minutes` (default `[10]`, up to eight values of 1–1440, sorted descending) is returned on the student state.
The workspace announces the tightest (smallest) threshold crossed, once, in a polite live region (not `alert`, so it does not interrupt typing);
time-up is the only `alert`. A student who joins late, already past a threshold, hears the nearest one once and not every missed one.

### 3. Navigation: view index is separate from release index
The navigator lists released slots from the server (`…/slots`) plus locked placeholders and shows the four counts. Previous/Next move a
**view** index over released slots only; Next on the current slot is the one action that releases the following question and says it
cannot be undone. A pending autosave is flushed against the question being left before the view changes. Whether earlier slots can be
re-opened is still the institution's choice (`EXAM_STUDENT_REVISIT_RELEASED_SLOTS`, default off, decision D-3): with it off, Previous is
disabled and no earlier question is requested. Mark-for-review is a toggle saved with the draft (`marked_for_review`).

### 4. Teacher actions: confirm, name, reason, idempotent, then re-read
A shared `ConfirmDialog` (focus starts on Cancel, Escape cancels, Tab stays inside, consequence stated, optional/required audited reason)
fronts every state change: run commands, per-student extend / end attempt / recover, grant / revoke access, short-link regenerate /
revoke. The idempotency key for a per-student action is generated when the dialog opens, so a retry cannot apply twice. After **every**
attempt - success or rejection - the page reloads authoritative state; `describeConflict` turns server reasons (`version_conflict`,
`deadline_passed`, `attempt_exists`, …) into plain sentences shown in one live region instead of replacing the page with a crash screen.
Freshness shows the server's time at the last refresh and a ticking "refreshed Ns ago"; with the live feed not connected and data older
than 30 s the grid says it may be out of date.

### 5. Short links are a locator, not a credential
Behind `EXAM_SHORT_LINKS_ENABLED` (default **off**). One active code per run, 8 characters from an unambiguous alphabet (random, not
sequential), collision-checked, revocable; regenerate revokes the old one in the same transaction; each change is audited with the
actor. The faculty routes need run scope and a run that still admits students. `GET /student/short-links/{code}` needs a signed-in
student **enrolled** in that run and answers the same `404` for unknown, revoked, malformed, not-enrolled and feature-off, so it cannot
probe for runs; it is rate-limited per user and per IP, and it returns only the run id - admission, content and submission keep their
own checks. The join page then continues to the existing readiness check.

### 6. Contrast is fixed where it is exercised
A `.dark .exam-scope { --text-muted: #a1a1aa }` override, applied by the exams layout, raises muted text to about 7:1 on the card surface
without touching the rest of the product. A test computes the ratios from `globals.css` (and fails if the override is removed).

## Consequences

- New optional response fields only (`server_time`, `time_warning_minutes`); no data migration. Old clients ignore them.
- Residual risks: the timer, navigator and dialogs are verified with jsdom and fake timers, **not in a real browser or with a screen
  reader**; automated contrast covers theme tokens, not every rendered pair; `epoch` is still a constant 1; the roster/institution
  teacher screens are not built; nothing ran against real Firestore.
- Open decisions: D-3 (revisit policy) and D-9 (warning thresholds beyond the 10-minute default) remain with the institution; both are
  data/flags today.
