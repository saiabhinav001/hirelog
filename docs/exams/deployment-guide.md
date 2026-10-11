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
```

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

The request path is separate: a 90-student burst (one question, three draft saves each, a synchronized submit) cost about 4,300 reads and 800 writes in total here, including one read of the legacy `users/{uid}` document per request (`get_current_user_uncached`), which also creates that document for a first-time user. A real exam has more questions and saves, so measure it before relying on the free plan.