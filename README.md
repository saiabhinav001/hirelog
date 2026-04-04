# HireLog Placement Archive

HireLog is a full-stack platform for collecting interview experiences and turning them into searchable institutional knowledge.

This repository is development-first and operationally focused.

## Current Operating Model

- Frontend is already deployed.
- Backend is deployed and updated independently.
- Frontend points to backend through NEXT_PUBLIC_API_BASE_URL.

## Repository Structure

```text
.
|- backend/      FastAPI services, search pipeline, scripts, tests
|- frontend/     Next.js app, UI, contexts, tests
|- docs/         deployment, runbooks, standards
```

## Core Capabilities

1. Experience submission with structured storage.
2. NLP enrichment (questions, topics, summary, embeddings).
3. Intelligent search (vector + lexical + rerank).
4. Dashboard analytics and moderation endpoints.
5. Practice lists and question tracking.

## Local Development

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

## Environment Configuration

### Backend

Use [backend/.env.example](backend/.env.example).

Minimum required values:
- FIREBASE_PROJECT_ID
- FIREBASE_SERVICE_ACCOUNT_PATH or FIREBASE_SERVICE_ACCOUNT_JSON
- ALLOWED_ORIGINS

### Frontend

Use [frontend/.env.example](frontend/.env.example).

Required values:
- NEXT_PUBLIC_API_BASE_URL
- NEXT_PUBLIC_FIREBASE_API_KEY
- NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
- NEXT_PUBLIC_FIREBASE_PROJECT_ID
- NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET
- NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID
- NEXT_PUBLIC_FIREBASE_APP_ID

## Backend Deployment (Hugging Face Docker)

Frontend is already deployed, so backend is deployed independently to a Hugging Face Docker Space.

Full procedure:
- [docs/backend-deployment.md](docs/backend-deployment.md)

Quick summary:
1. Set HF token, owner, and space name.
2. Set FIREBASE_PROJECT_ID, FIREBASE_SERVICE_ACCOUNT_JSON, and ALLOWED_ORIGINS.
3. Run [backend/scripts/deploy_hf_space.py](backend/scripts/deploy_hf_space.py).
4. Verify /health/live and /health/ready.
5. Point frontend NEXT_PUBLIC_API_BASE_URL to the hf.space URL.

## Optional Split API + Worker Deployment

Use:
- [backend/Dockerfile](backend/Dockerfile)
- [backend/Dockerfile.worker](backend/Dockerfile.worker)
- [backend/docker-compose.backend.yml](backend/docker-compose.backend.yml)

Operational details:
- [docs/backend-cutover-runbook.md](docs/backend-cutover-runbook.md)

## Quality Gates

### Backend

```bash
cd backend
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m compileall app
```

### Frontend

```bash
cd frontend
npm run lint
npm run type-check
npm run test:ci
npm run build
npm run test:e2e
```

## API Endpoints (High Level)

Public:
- GET /
- GET /health
- GET /health/live
- GET /health/ready
- GET /health/deep
- GET /api/search
- GET /api/search/facets

Authenticated:
- /api/users/*
- /api/experiences/*
- /api/practice-lists/*
- /api/dashboard/*

## Documentation Index

- [docs/backend-deployment.md](docs/backend-deployment.md)
- [docs/backend-cutover-runbook.md](docs/backend-cutover-runbook.md)
- [docs/engineering-standards.md](docs/engineering-standards.md)

## Security Notes

- Keep service-account secrets out of version control.
- Firestore client rules are defined in [firestore.rules](firestore.rules).
- Backend uses Firebase Admin SDK for server-side operations.
