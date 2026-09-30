# ADR 0012 — Faculty/student interfaces and live control

**Status:** Accepted

## Context

Prompts 1–6 built a complete backend contract (auth, institution identity,
exam domain/state machine, question delivery, KT/generation, coding judge)
with no UI beyond a placeholder login page. This phase builds the actual
faculty and student screens on Hirelog's existing design tokens/navigation,
plus the live-control layer (authenticated WebSocket admission, ordering/
dedup, reconciliation, reconnect/fallback) needed for a released exam to
feel "live" — a faculty command or a student's own status change should
reach the other side within seconds, without ever trusting the socket as
authoritative.

Two real gaps were discovered and closed while wiring the UI to the real
backend contract (not stand-ins — see "Decisions" below): `PublicQuestionRef
.public_content` was never actually populated (Prompt 4 reserved the field,
Prompt 5 never filled it), and nothing ever advanced `Attempt
.current_slot_index` (the field existed, but no action moved it). Both are
now real, tested behavior, not UI-side workarounds.

Committed on `exams/phase-7-faculty-student-ui`, stacked on top of
`exams/phase-6-judge-grading` (current tip of the branch stack; `main`
remains untouched throughout).

## Decisions

### 1. WebSocket ticket admission — poll-free, but still push-based

`POST /api/exams/v1/live/ticket` is a normal authenticated REST call
(Bearer token) that mints a single-use, 60-second ticket bound to
`(run_id, uid, role)`; the WS handshake (`GET /api/exams/v1/live/ws?ticket=
...&run_id=...`) presents only that ticket as a query param, because
browsers cannot set an `Authorization` header on a WebSocket upgrade. The
ticket is verified and consumed inside one Firestore transaction (read +
mark-consumed together) — a replayed ticket can never open a second
connection. Every reconnect attempt mints a brand-new ticket; none are ever
reused (contract: "authenticated WebSocket ticket admission").

### 2. The socket is read-only and advisory — REST keeps every guard

The socket accepts no client-sent commands at all (the server-side receive
loop only exists to detect disconnects promptly; any received frame is
discarded). Every `LiveEvent` it pushes carries only a `kind`/`state_version`
/summary `payload` — the frontend's `useLiveSocket` hook treats EVERY
accepted event (in-order or gapped alike) as "go refetch the real state over
REST", never as data to apply directly. This is what "socket events never
override server guards" means concretely: there is no code path anywhere
that mutates UI-trusted state from a socket payload.

### 3. Per-run sequencing, not per-connection; targeted vs. broadcast scope

`LiveConnectionManager` keeps one monotonic `seq` counter per `run_id` (not
per socket) so a reconnecting client can tell a gap (missed events) from a
duplicate (replayed event) by comparing against the last `seq` it saw.
`LiveEvent.target_uid=None` reaches every connection for that run (a faculty
command); a set `target_uid` reaches the faculty room *and only that one
student's own connection(s)* — other students never observe another
student's targeted event (warning count, time extension, slot progress).

### 4. Single-process fan-out — a documented, not hidden, scaling limit

`LiveConnectionManager` is in-process memory, the same class of limitation
already accepted for the outbox drain loop and in-memory rate limiter: a
second server instance has its own registry and never sees another
instance's connections. A student and the faculty watching them must be
routed to the same instance for live updates to arrive; horizontal scaling
needs a shared pub/sub backend (e.g. Redis), not implemented here. The REST
fallback (below) is what keeps the *feature* correct even without that —
only the *latency* of faculty visibility degrades on a multi-instance
deployment without a shared backend.

### 5. Reconnect: jittered exponential backoff, then REST polling fallback

`useLiveSocket` (frontend) reconnects with jittered backoff (1s → 30s cap)
and mints a fresh ticket every attempt. After 5 consecutive failures it
starts a REST polling fallback (`onReconcile` on a timer) *in addition to*
continuing backoff attempts in the background — recovering silently back to
"connected" the moment a socket attempt succeeds. `connectionHealth` is
always one of `connecting|connected|reconnecting|polling|offline`, shown
honestly in the header — never a fabricated "all good" state.

### 6. `public_content` allowlist — one function, every item type

`app/domain/generation/public_content.py::build_public_content` is now the
single place deciding what of a `GenerationCandidate` a student's current-
question response includes: prompt/constraints/examples always; `options`
for choice items; for coding, only the reference-solution **language keys**
(never the solution source) plus the candidate's own `public_tests`. Hidden
test groups, the private expected answer, explanation, and marking rubric
are never read by this function at all — not filtered out, structurally
absent from its inputs. `get_current_question` now calls this (an additive
change to the existing Prompt 4 route), closing the gap where the field
existed but was always empty.

### 7. Forward-only slot advance — the server enforces "no back button"

`decide_advance_slot` (new, pure, in `decisions.py` alongside the rest of
Prompt 3/4's decision functions) is the actual mechanism behind "Keyboard
Back/Next cannot access locked or rewrite submitted content": there is no
server action that ever decrements `current_slot_index`, and the last slot
has no "next" at all (the student submits instead). `POST .../next-question`
is transactional, so a double-click/two-tab race advances by exactly one
slot, never two. A student's browser Back button can still navigate to a
previously-rendered page in history, but re-fetching `GET .../question`
always returns the (unchanged, forward-only) current slot — there is
nothing "back" to actually reach.

### 8. Draft-version recovery endpoints — closing the "student refresh" gap

Final submit requires `expected_draft_versions` to exactly match every
question's current server version. A refreshed tab has no memory of that
map, so two small additive endpoints were added: `GET .../drafts` (all of
*this student's own* question→version pairs for the run) and `GET
.../drafts/{question_id}` (that one draft's version + answer, to re-hydrate
the editor). Both reuse `DraftRepository` methods that already existed
(`all_versions`, `get`) — no new repository logic, only new routes.

### 9. No rich code editor — the accessible textarea IS the fallback

No Monaco/CodeMirror dependency was added. `CodeEditorPanel` is a plain,
fully keyboard/screen-reader-operable `<textarea>` (monospace, tab-key
indent handling, explicit `aria-label`) lazy-loaded via `next/dynamic`
(contract: "lazy-load camera/editor on exam routes" — the editor bundle
ships to no other route). This IS the fallback path, not a fallback FROM
something richer — adding an unvetted heavy editor dependency in a security/
accessibility-sensitive exam context was judged a bigger risk than a plain
textarea's lesser affordances. A richer editor is a reasonable follow-up,
scoped separately.

### 10. Header never fabricates a capability it cannot observe

Camera thumbnail/capture-age and "delivery profile" (managed-lockdown vs.
standard browser) are not implemented until Prompts 8 and 10 respectively.
`ExamShellHeader` shows an explicit "Camera not yet configured" / "Unknown"
state and a plain "Standard browser" label rather than inventing a reading —
directly per contract: "No fabricated background-app or Wi-Fi-strength
indicators. Unknown capabilities say unknown."

### 11. Faculty candidate grid: real pagination, not virtualization

`AttemptRepository.list_page_for_run` is a real cursor-paginated query
(`order_by("uid")`, `start_after`, one extra row fetched to detect a next
page). The control-room table is a plain HTML table with First/Next page
buttons — no virtualization library (e.g. `react-window`) was added, since
none was already a dependency and introducing one is a scope decision, not
a one-line fix. For the grid sizes this phase targets (bounded by the same
25–200 row pages the backend already paginates), a plain table remains
responsive; virtualization is a reasonable follow-up if faculty routinely
run runs large enough to need it.

## Consequences

- **Positive:** 4 new backend domain modules (`app/domain/live/*`) with 27
  new backend tests (ticket lifecycle, connection-manager sequencing/
  scoping/queue-saturation, event-builder allowlists, `decide_advance_slot`,
  `build_public_content` hidden-data-leakage guards) — full backend suite
  316/316 passing, ruff clean, `compileall` clean. A real, working set of
  ~19 frontent routes across faculty and student roles, all using existing
  design tokens (`card`/`btn-primary`/`badge`/`input-field` utility classes)
  and the existing global Navbar/Footer — no unrelated page was restyled.
  29 frontend tests (live-socket ordering/dedup/reconnect/gap-detection,
  header's honest-unknown states, the accessible code editor, and a submit-
  confirmation irreversibility smoke test) all passing; `tsc --noEmit`,
  `eslint`, and a full production `next build` all clean.
- **Negative / follow-up required:** The full multi-actor scenarios named in
  the prompt — two students with distinct variants under one teacher,
  faculty reconnect/conflict, network loss, stale events, and lost submit
  response, all observed together in real time — are **not** exercised
  end-to-end here. They are covered at the unit level (connection-manager
  scoping/sequencing proves the *isolation* two students need; the
  live-socket hook's tests prove dedup/gap/reconnect *logic*), but a real
  two-browser, real-Firestore, real-WebSocket staging run has not been
  performed — consistent with every prior phase's "transactional shell
  untested against real Firestore" limitation (ADR 0008 §1), now extended to
  "WebSocket fan-out untested against a real multi-connection deployment."
  `LiveConnectionManager` is single-process-only (§4) — a production
  deployment with more than one instance needs a shared pub/sub backend
  before live updates are reliable across instances; nothing currently
  warns an operator if this is misconfigured beyond this ADR. "Lost submit
  response" has no automatic client-side retry/reconciliation yet — a
  failed submit shows an error and leaves drafts intact, but the student
  must manually retry or check their receipt page rather than the client
  doing it automatically. No virtualization on the candidate grid (§11).
  No rich code editor (§9). Faculty "question/test review" creates
  single-slot blueprints only through this UI (the backend supports
  multi-slot blueprints; the form just doesn't expose it yet). Results is a
  status-count summary, not the full gradebook (Prompt 9's job, per the
  prompt pack's own phase boundaries).

## Migration / configuration steps

- New setting, safe default: `EXAM_LIVE_TICKET_TTL_SECONDS=60`.
- New Firestore collection (`live_tickets`) — no rules changes needed
  (default-deny catch-all).
- New background thread is NOT added by this phase (the WS connections are
  request-scoped `asyncio` tasks, not a polling thread) — only the existing
  outbox-drain and judge-sweep threads continue as before.
- No new frontend env var — the WebSocket origin is derived from the
  existing `NEXT_PUBLIC_EXAMS_API_BASE_URL` by swapping `http(s)` for
  `ws(s)`, keeping the "pinned to one configured origin" contract intact.
  `next.config.ts`'s CSP `connect-src` was extended with the derived `ws://`
  /`wss://` origin alongside the existing `https://` one.
- No data migration. Prompt 3/4's files changed only additively: a new pure
  decision function and one new service method in `decisions.py`/
  `student_service.py`, new routes in already-existing route files, and one
  new field-population line in `get_current_question`.

## Next-phase prerequisites

- Prompt 8 (proctoring evidence) has a real header slot to wire a camera
  thumbnail/capture-age into, and a real `attempt_status_changed` live event
  already reaching the faculty candidate grid to extend with incident
  signals.
- Prompt 9 (gradebook) has a real results route to replace the status-count
  placeholder, and the judge/MCQ/short-answer grading endpoints from
  Prompt 6 already reachable from a faculty-authenticated session.
- Prompt 10 (PWA/lockdown) has a real "delivery profile" header slot to wire
  managed-lockdown detection into, replacing the current honest "Standard
  browser" placeholder.
