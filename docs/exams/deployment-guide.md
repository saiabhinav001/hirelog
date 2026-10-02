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
