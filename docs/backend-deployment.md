# Backend Deployment Guide

This guide is for the current target state where the frontend is already deployed and only the backend must be deployed or updated.

## Deployment Goal

- Keep the deployed frontend unchanged.
- Deploy backend API on Hugging Face Docker Space.
- Connect frontend to backend base URL.
- Validate health, auth, search, and write flows.

## Prerequisites

1. Hugging Face account and access token with write permission for Spaces.
2. Frontend production URL for CORS, for example: https://your-frontend.vercel.app
3. Firebase values:
   - FIREBASE_PROJECT_ID
   - FIREBASE_SERVICE_ACCOUNT_JSON (single-line JSON)

## Step 1: Prepare Configuration

Backend runtime variables required in the Space:

- ENV=production
- FIREBASE_PROJECT_ID=<your-project-id>
- FIREBASE_SERVICE_ACCOUNT_JSON=<single-line-json>
- ALLOWED_ORIGINS=https://your-frontend.vercel.app

Recommended production defaults:

- SEARCH_ENGINE=faiss
- SEARCH_ENABLE_WARMUP=true
- SEARCH_INDEX_WORKER_MODE=embedded
- SEARCH_INDEX_QUEUE_BACKEND=memory

Reference template:
- backend/.env.example

## Step 2: Deploy to Hugging Face Docker Space

Use the automation script in this repository:

- backend/scripts/deploy_hf_space.py

From backend directory:

```bash
set HF_TOKEN=<your_hf_token>
set HF_OWNER=<hf_username_or_org>
set HF_SPACE_NAME=hirelog-backend
set HF_PRIVATE=true
set FIREBASE_PROJECT_ID=<your_project_id>
set ALLOWED_ORIGINS=https://your-frontend.vercel.app
set FIREBASE_SERVICE_ACCOUNT_JSON=<single-line-json>

.venv\Scripts\python.exe scripts\deploy_hf_space.py
```

What the script does:

1. Creates or reuses a Docker Space.
2. Uploads backend runtime files (Dockerfile, requirements, app, data).
3. Configures Space variables and secret values.
4. Waits for runtime stage transitions.

## Step 3: Verify Backend Health

### Public Space

Run:

1. GET /health/live
2. GET /health/ready
3. GET /health/deep

### Private Space

Use an Authorization header with your HF token when calling the hf.space URL.

Expected results:

- /health/live returns service alive.
- /health/ready returns ready=true.
- /health/deep does not report overall_status=error.

## Step 4: Connect Frontend to Backend

In frontend deployment settings:

- NEXT_PUBLIC_API_BASE_URL=https://<owner>-<space-name>.hf.space

No frontend code changes are required.

## Step 5: Production Smoke Test Checklist

1. Login works.
2. Submit experience succeeds.
3. Search returns results.
4. Dashboard loads stats.
5. Practice list operations work.

## Optional: External Worker Deployment

Only required when you want queue processing split from API.

Use:

- backend/Dockerfile.worker

Set:

- SEARCH_INDEX_WORKER_MODE=external
- SEARCH_INDEX_QUEUE_BACKEND=firestore
- SEARCH_INDEX_FIRESTORE_COLLECTION=search_index_tasks

## Rollback

If a release fails:

1. Revert Space files to previous commit/image.
2. Re-apply previous variable/secret set.
3. Re-run health checks.

For staged traffic cutover/rollback automation:

- docs/backend-cutover-runbook.md

## Troubleshooting

### CORS failures

- Ensure ALLOWED_ORIGINS exactly matches your frontend origin.

### API returns 401 after successful frontend auth

- Verify FIREBASE_PROJECT_ID and FIREBASE_SERVICE_ACCOUNT_JSON belong to the same Firebase project.

### Slow first responses

- Keep SEARCH_ENABLE_WARMUP=true.
- Wait for /health/ready before routing production traffic.

### Private Space health checks return 404 without token

- For private spaces, call health endpoints with Authorization: Bearer <HF token>.
