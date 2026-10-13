# Exam API Deployment Guide

The exam API (`app.exam_main:app`) is an **independently deployable** FastAPI
process — separate from the legacy API (`app.main:app`) and legacy worker
(`app.worker_main:app`). It shares the same Firestore project but has its own
slim dependency set (no spaCy/sentence-transformers/FAISS/torch — see
`backend/requirements-exams.txt`) and its own container image
(`backend/Dockerfile.exams`). `EXAMS_ENABLED` defaults to `false`, so running
this process changes nothing for the legacy product until explicitly turned
on.

This guide covers deploying it; see [rollback-runbook.md](rollback-runbook.md)
for rolling back a live deployment and
[production-qualification-report.md](production-qualification-report.md) for
what has and has not been verified before a real release.

## Prerequisites

- A Firestore project already serving the legacy API (same project — the
  exam collections live alongside the legacy ones, all covered by the
  default-deny catch-all in `firestore.rules`).
- A container registry/host capable of running `backend/Dockerfile.exams`
  (any Docker-compatible host; `docker-compose.backend.yml` shows all three
  processes — `api`, `worker`, `exams` — running side by side for local/
  staging use).
- Decide which of the institutional provider gaps in
  [production-qualification-report.md §9](production-qualification-report.md)
  you are shipping with the safe default (fake/no-op) versus configuring for
  real, before enabling `EXAMS_ENABLED` for a real cohort.

## Step 1: Required environment variables

At minimum, beyond the legacy API's existing Firebase variables
(`FIREBASE_SERVICE_ACCOUNT_JSON`/`_PATH`, `FIREBASE_PROJECT_ID`):

```bash
EXAMS_ENABLED=true
EXAM_FACULTY_EMAILS=                 # or rely on real institution membership (ADR 0010)
EXAM_OTP_HMAC_SECRET=                # REQUIRED for any multi-instance deployment — see ADR 0010
EXAM_SUPER_ADMIN_EMAILS=             # platform administrators; keep it a secret on a hosted service
```

Students sign in with an e-mailed code. A host that blocks SMTP (Hugging Face Spaces allow outbound 80/443/8080 only)
needs the HTTPS relay in [otp-email-relay.md](otp-email-relay.md) (`EXAM_OTP_DELIVERY_PROVIDER=webhook`); without a
delivery provider, student OTP sign-in answers 503 and only staff who sign in with Google can use the exam pages.

Everything else in `backend/.env.example`'s "Exams module" section has a safe
default and can be left unset for an initial deployment; each unset/default
value is a real functional gap documented in the qualification report's §9
(malware scanner, generation provider, judge provider, evidence storage
bucket, JPlag, SEB keys) — not a configuration you can forget and expect to
work silently.

The new rollback lever (`EXAM_NEW_ADMISSIONS_PAUSED`, default `false`) does
not need to be set for a normal deployment — see
[rollback-runbook.md](rollback-runbook.md) for when to flip it.

## Step 2: Build and run

```bash
cd backend
docker compose -f docker-compose.backend.yml up --build exams
```

Or standalone:

```bash
docker build -f Dockerfile.exams -t hirelog-exam-api .
docker run -p 8020:8020 --env-file .env hirelog-exam-api
```

## Step 3: Verify health

```bash
curl -fsS http://<host>:8020/health/ready
```

Same health-endpoint contract as the legacy API
(`/health/live`, `/health/ready`, `/health/deep`) — gate any traffic-shifting
automation on these exactly as `docs/backend-cutover-runbook.md` already does
for the legacy API. The exam API's `/health/ready` reports Firestore
connectivity; it does not (and should not) report on institutional provider
configuration (those are expected to be unset at first deploy).

## Step 4: Connect the frontend

Set `NEXT_PUBLIC_EXAMS_ENABLED=true` and point the frontend's exam API client
at the deployed exam API base URL (mirrors the legacy multi-base-candidate
pattern in `frontend/src/lib/api.ts` — see that file for the exact client
wiring). Leave this `false` until you are ready for real users to see the
`/exams/*` routes.

## Step 5: Production smoke test checklist

Before enabling for a real cohort:

- [ ] `GET /health/ready` returns 200.
- [ ] With `EXAMS_ENABLED=false` (pre-cutover default), every `/api/exams/v1/*`
      route returns 503, including unauthenticated requests (regression
      covered by `backend/tests/test_exams_routes_smoke.py`).
- [ ] A real faculty account (via `EXAM_FACULTY_EMAILS` or real institution
      membership) can create a definition, publish a revision, and create/
      release a run.
- [ ] A real enrolled student can admit, reach the first question, save a
      draft, and submit.
- [ ] Re-run `backend/scripts/exam_load_test.py` against the real deployed
      instance (not the emulator) with real auth tokens before scheduling a
      real exam at scale — see
      [production-qualification-report.md §3](production-qualification-report.md)
      for why the emulator-based numbers in this repo are not sufficient on
      their own.

## Rollback

See [rollback-runbook.md](rollback-runbook.md). In short: prefer
`EXAM_NEW_ADMISSIONS_PAUSED=true` (preserves in-progress students) or a
per-run `pause` command (Prompt 3) over the blunt `EXAMS_ENABLED=false`
unless the exam engine itself is unsafe to keep serving at all.

## Troubleshooting

### `/health/ready` is slow or times out

Check the exam API's Firestore connectivity directly — it shares the same
credential-loading code path as the legacy API
(`backend/app/core/firebase.py`); see `docs/backend-deployment.md`'s
troubleshooting section for the same-shaped legacy issues (CORS, 401 after
auth, slow first response on cold start).

### A route returns 503 even with `EXAMS_ENABLED=true`

Every exam router declares `dependencies=[Depends(require_exams_enabled)]` at
the router level (regression-tested in `test_exams_routes_smoke.py`) — a 503
with the flag on means the deployed process is reading a different
`EXAMS_ENABLED` value than you expect (check env var propagation into the
actual running container, not just the `.env` file on the build host).

## Alternative: serve the exam API from the main HireLog backend

Use this when a second service is not available (for example a Hugging Face Space that already runs `app.main:app` for search). Nothing changes unless `EXAMS_ENABLED=true`.

1. On the main backend host set the exam variables from Step 1 (`EXAMS_ENABLED=true`, `EXAM_SUPER_ADMIN_EMAILS`, `EXAM_OTP_HMAC_SECRET` and the rest) and `ALLOWED_ORIGINS` for the frontend. Install `requirements.txt`, which now includes the four exam-only packages.
2. `app.main` then mounts the exam app after its own routes (every existing route still matches first), starts the exam workers (outbox, finalization, judge sweep, proctoring and retention sweeps) in its own startup, and exempts `/api/exams/` from the legacy 80-writes-a-minute limiter, which would otherwise throttle autosave. The exam app keeps its own error handling and `Cache-Control: no-store`.
3. Point the frontend at the same host: `NEXT_PUBLIC_EXAMS_API_BASE_URL=https://<main-backend-host>` (`NEXT_PUBLIC_EXAMS_ENABLED=true`), then rebuild the frontend.
4. Verify `GET /health/live` (the main app's) and `GET /api/exams/v1/me` (should answer 401 or 403 without a token).

Limits: the exam workers and live-event registry run inside the one process that serves search, so a restart or a cold start of that service also restarts them, and a second instance does not share live events. Do not rely on this for a real exam run until the release-pack gates are met.

## Running on the free (Spark) Firestore plan

Every worker poll costs at least one Firestore read even when nothing is waiting, and the free plan allows 50,000 reads a day for the whole project; a project shared with the legacy app shares that quota with it. Measured by counting Firestore requests while the exam API ran against the local emulator, with rates extrapolated from windows of 1 to 5 minutes (an estimate of the request count, not a bill):

| | Fixed polling (2 s outbox and judge, 15 s proctoring and finalization) | Idle-adaptive polling |
|---|---|---|
| Exam workers, nothing happening | about 148,000 reads a day | about 4,700 reads a day |
| 90 students mid-exam, workers only | about 43,000 reads an hour (the sweeper read every live attempt's drafts every 15 s) | about 3,000 reads an hour |

The outbox, judge, proctoring and finalization workers now poll at their minimum interval only while they find work, and back off while idle (`EXAM_OUTBOX_POLL_*`, `EXAM_JUDGE_SWEEP_*`, `EXAM_PROCTORING_SWEEP_*` and `EXAM_FINALIZATION_SWEEP_*` in `.env.example`; the defaults keep the old minimums, so exam-time latency is unchanged). Work done in the same process wakes them at once: admission, submit, run commands, terminate and recover, KT uploads, judge runs and proctoring observations. The finalization sweeper sleeps until the earliest known attempt deadline (never longer than `EXAM_FINALIZATION_SWEEP_MAX_INTERVAL_SECONDS`) and reads drafts only for attempts already past their deadline, which is the only case in which it can seal one. Work created by a different process cannot wake these loops, so with several instances a job can wait up to the maximum interval; the defaults suit one instance.

The request path is separate; see the next section.

### Read economy of the request path

Firestore bills one read per document returned (a query that matches nothing costs one) and one write per document written. Measured by counting the requests of one exam API process against the local emulator while 30 students each worked through 6 questions with 8 saves per question (48 saves a student, a heavier typist than the client's throttled autosave produces) and one faculty console reloaded the candidate grid every second:

| | Reads per student | Writes per student |
|---|---|---|
| Before | about 1,160 | about 59 |
| Server-side caching and guards | about 223 | about 58 |
| Plus the client updating its navigator from the save response | about 175 | about 58 |

With tab hidden/visible observations (6 a student) and the live-socket reconcile modelled as well the figure was about 198 reads and 71 writes a student.

Run at full size (90 students, 10 questions, 12 saves a question, so 10,800 saves, one faculty console, the live-socket reconcile modelled, no tab observations) every student submitted with a distinct receipt and no request failed or was refused. The totals were about 32,600 reads and 12,200 writes (363 and 135 a student), against the free plan's 50,000 and 20,000 a day. Saves account for about two thirds of the reads and nine tenths of the writes. The floor is 2 reads and 1 write per accepted save, because a save decides inside a transaction on the attempt and the draft it replaces, so fewer saves per student cost proportionally less: the client's autosave throttle is the lever. One exam of that size uses about two thirds of a day's free quota (the day ends at midnight Pacific time), so a second exam the same day, or other traffic on a shared project, can use it up.

What produces the saving, in order of size:

- Per-request guards no longer re-read stable documents. The run, definition, revision, lockdown configuration, memberships, staff bindings and each student's draft summaries are cached in process (`app/core/read_cache.py`, `EXAM_*_CACHE_SECONDS`). A write made by the process drops the entry at once, so the TTL only bounds staleness for a write made by another process (relevant only with more than one instance). A save, a submit and a command always decide on a fresh transactional read.
- Identity is the verified token alone: the exam API no longer reads or creates the legacy `users/{uid}` document per request.
- The navigator list is no longer fetched after every save: the save response carries `answered` and the client updates its own list. Autosave waits for a pause of 3 s and at most 12 s while typing (`NEXT_PUBLIC_EXAM_AUTOSAVE_DEBOUNCE_MS`, `NEXT_PUBLIC_EXAM_AUTOSAVE_MAX_WAIT_MS`); moving to another question, submitting and hiding the tab still save immediately.
- The faculty candidate grid is kept in memory and patched one row at a time when an attempt changes, with a full re-read at most every `EXAM_ATTEMPT_VIEW_RESYNC_SECONDS`.
- The finalization sweep sleeps while a run has no live attempt (`EXAM_FINALIZATION_DORMANT_RUN_SECONDS`) and is woken by an admission; the judge sweep stops polling a student's test run that never finished (`EXAM_JUDGE_PUBLIC_RUN_STALE_SECONDS`).

Idle cost with exams enabled and nobody using them is now about 2 reads a minute (about 2,900 a day), lower than the idle-adaptive figure in the table above because the idle caps were raised to 10 minutes and the finalization sweep sleeps while no run has a live attempt.

Limits of the measurement: it counts requests against the emulator, not bills; it does not include the Firebase Auth or hosting traffic; the live socket was modelled as a ticket request plus one extra reconcile rather than a real WebSocket; and the project quota is shared with every other client of the same project, which this process cannot see or control.

### Keeping exam data in its own free Firebase project

Free quotas are per project, so a project shared with the live site means exams and the site spend the same daily budget, and when the site uses it up every exam read also fails with `429 Quota exceeded`. Google enforces "read operations per project per day"; on the one day observed this ran about 1.9 times the document reads shown in the Firebase console's Usage tab, so check Google Cloud console > APIs & Services > Cloud Firestore API > Quotas rather than that tab.

Set these on the backend to keep all exam data in a second (also free) project. Sign-in stays on the main project, and the second project needs only Firestore (no Auth, no Storage, no composite indexes):

| Variable | Value |
|---|---|
| `EXAM_FIREBASE_PROJECT_ID` | the new project's id |
| `EXAM_FIREBASE_SERVICE_ACCOUNT_JSON` (secret) or `EXAM_FIREBASE_SERVICE_ACCOUNT_PATH` | a service-account key for the new project, as a one-line JSON string or a file path |

With `EXAM_FIREBASE_PROJECT_ID` empty (the default) nothing changes. When it is set, every exam module and worker uses the exam project, and the exam identity dependency stops reading and creating `users/{uid}` in the main project, so an exam request never touches the live site's database. Existing exam data is not moved if you switch later. A project id equal to `FIREBASE_PROJECT_ID` is treated as "not separate", and a missing key fails at the first exam request with a clear error.

Checked on local emulators with two projects: a 90-student burst (three draft saves each, synchronized submit) returned 200 for every request with 90 distinct receipts, all exam collections existed only in the exam project, and no student profile document appeared in the main project.