# HireLog

[![CI](https://github.com/saiabhinav001/hirelog/actions/workflows/ci.yml/badge.svg)](https://github.com/saiabhinav001/hirelog/actions/workflows/ci.yml)
[![CodeQL](https://github.com/saiabhinav001/hirelog/actions/workflows/codeql.yml/badge.svg)](https://github.com/saiabhinav001/hirelog/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![Next.js 16](https://img.shields.io/badge/Next.js-16-black.svg)](https://nextjs.org/)

> A full-stack platform that turns scattered interview experiences into searchable, AI-enriched institutional knowledge — with hybrid semantic + lexical search, background NLP enrichment, and a measured search-quality gate.

---

## Overview

Interview preparation knowledge is usually trapped in private notes, group chats, and memory. It's inconsistent, hard to search, and lost the moment a student graduates.

**HireLog** fixes that. Students submit their interview experiences; the platform automatically extracts questions, tags topics, generates summaries, and embeds them for semantic search. Anyone preparing can then search across the whole archive by **meaning**, not just keywords — "how do I design a scalable feed?" surfaces the right experiences even when the wording differs.

It solves three concrete problems:

- **Discoverability** — hybrid retrieval (vector + keyword) finds relevant experiences across paraphrase and exact terms.
- **Structure** — an NLP pipeline converts raw text into questions, topics, summaries, and embeddings.
- **Trust & privacy** — contributions can be anonymous, and that anonymity is enforced as a hard invariant that background processing can never violate.

---

## Features

- **AI-assisted interview prep** — submitted experiences are enriched into structured questions, topics, and summaries automatically.
- **Hybrid search** — semantic retrieval (FAISS + MiniLM embeddings) fused with BM25 lexical scoring via Reciprocal Rank Fusion, then re-ranked by a cross-encoder. Degrades gracefully to keyword-only if the model or index is unavailable.
- **NLP enrichment pipeline** — spaCy + sentence-transformers extract questions, classify topics, summarize, and generate embeddings in background threads that never block the request.
- **Analytics dashboard** — tiered, cached aggregates (top companies/topics, difficulty mix, frequent questions, interview progressions) plus a placement-cell moderation view.
- **Practice workflow** — personal practice lists with per-question status tracking (unvisited / practicing / revised) and topic distribution.
- **Authentication & authorization** — Firebase Auth on the client; verified ID tokens and role gating (`placement_cell`) on the server.
- **Anonymity guarantee** — an explicit invariant prevents any enrichment path from ever writing identity fields onto anonymous contributions.
- **Production hardening** — distributed rate limiting, multi-tier caching, structured logging, self-healing vector index, and a search-quality gate in CI.

---

## Architecture

```
          ┌──────────────┐
          │   Frontend   │  Next.js · React · Firebase Auth
          └──────┬───────┘
                 │  Bearer ID token
                 ▼
          ┌──────────────┐
          │   Backend    │  FastAPI · rate limit · request_id
          └──────┬───────┘
                 ▼
          ┌──────────────┐
          │  Firestore   │  system of record (experiences, users, lists)
          └──────┬───────┘
                 ▼
          ┌──────────────┐
          │ NLP Pipeline │  spaCy + sentence-transformers (background)
          └──────┬───────┘
                 ▼
          ┌──────────────┐
          │ FAISS/Search │  vector + BM25 → RRF → cross-encoder rerank
          └──────┬───────┘
                 ▼
          ┌──────────────┐
          │  Dashboard   │  cached aggregates + moderation
          └──────────────┘
```

Firestore is the single source of truth; FAISS, Redis, and Typesense are derived and rebuildable from it. On startup the backend reconciles derived state (rebuilds the vector index if the disk was lost, warms caches, repairs stats, sweeps stuck NLP jobs).

See **[docs/architecture.md](docs/architecture.md)** for the detailed request flow and **[docs/adr/](docs/adr/README.md)** for the decisions behind each subsystem.

---

## Tech Stack

| Layer | Technologies |
|---|---|
| **Frontend** | Next.js 16, React 19, TypeScript, Tailwind CSS v4, Firebase JS SDK, lucide-react |
| **Backend** | FastAPI, Uvicorn, Python 3.11, Pydantic v2 |
| **Auth & Data** | Firebase Authentication, Cloud Firestore (Firebase Admin SDK) |
| **AI / Search** | sentence-transformers (MiniLM), spaCy, FAISS, cross-encoder reranker, ONNX Runtime |
| **Optional infra** | Redis (cache + rate-limit windows), Typesense (alternate lexical engine) |
| **Testing & QA** | pytest, Vitest, Testing Library, Playwright, k6, ruff, ESLint, bandit, CodeQL |
| **CI/CD** | GitHub Actions |

Only technologies actually present in the repository are listed.

---

## Screenshots

> Screenshots are not yet included in the repository. To add them, drop images into `docs/screenshots/` and replace the rows below. Key views worth capturing:

| View | What it shows |
|---|---|
| Landing / Search | Hybrid search box with results and facets |
| Experience detail | Extracted questions, topics, and summary |
| Dashboard | Aggregated analytics and interview progressions |
| Practice lists | Per-question status tracking |
| Admin / Moderation | Placement-cell queue and quality metrics |

---

## Search Evaluation

Search ranking is easy to regress silently, so relevance is **measured and gated in CI**, not assumed. The harness in [`backend/eval/`](backend/eval/) ranks a labelled golden query set with the same BM25 scorer used in production and reports three standard IR metrics:

- **Recall@K** — did we retrieve the relevant experiences within the top *K*? Measures coverage.
- **MRR (Mean Reciprocal Rank)** — how high was the first relevant result? Measures how quickly a user finds a good answer.
- **nDCG@K** — are the most relevant results ranked highest? Measures ordering quality with position discounting.

The CI job fails the build if any metric drops below its threshold. Run it locally:

```bash
cd backend
PYTHONPATH=. python -m eval.run_eval
```

Methodology and how to extend it to semantic/hybrid evaluation: **[docs/adr/0006-search-evaluation.md](docs/adr/0006-search-evaluation.md)**.

---

## Reliability

| Area | Implementation |
|---|---|
| **Authentication** | Firebase ID tokens verified server-side on every protected route. |
| **Authorization** | Role gating (`placement_cell`) enforced in the backend; the UI never controls access. |
| **Caching** | Tiered dashboard cache (memory → Firestore → recompute) with atomic, lock-guarded updates; two-tier search cache (Redis → memory). |
| **Rate limiting** | Sliding-window limiter, Redis-backed and shared across instances when configured, with a thread-safe in-memory fallback. |
| **Logging** | Structured JSON logs in production with a `request_id` correlated across every line of a request. |
| **Resilience** | Search degrades to keyword-only under model failure (circuit breaker); the FAISS index self-heals from Firestore on startup. |
| **Testing** | Backend unit + concurrency regression tests (pytest); frontend unit tests (Vitest) and e2e smoke (Playwright). |
| **CI pipeline** | GitHub Actions: lint, type-check, tests, build, search-quality gate, dependency audit, and CodeQL SAST. |

Capabilities are described as implemented — nothing here is aspirational. Known limits and their revisit triggers are documented in the [ADRs](docs/adr/README.md).

---

## Local Setup

**Prerequisites:** Python 3.11, Node.js 20+, and a Firebase project (Auth + Firestore).

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate       # macOS/Linux
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env               # then fill in Firebase values
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local         # then fill in Firebase + API base
npm run dev                        # http://localhost:3000
```

### Quality gates

```bash
# Backend
cd backend
python -m ruff check .
PYTHONPATH=. python -m pytest
PYTHONPATH=. python -m eval.run_eval

# Frontend
cd frontend
npm run lint
npm run type-check
npm run test:ci
npm run build
```

---

## Deployment

The frontend deploys to any Next.js host (e.g. Vercel); the backend runs as a Docker container (a Hugging Face Docker Space is the reference target).

### Required environment variables

**Backend** (see [backend/.env.example](backend/.env.example)):

- `FIREBASE_PROJECT_ID`
- `FIREBASE_SERVICE_ACCOUNT_JSON` (production) **or** `FIREBASE_SERVICE_ACCOUNT_PATH` (local)
- `ALLOWED_ORIGINS`

**Frontend** (see [frontend/.env.example](frontend/.env.example)):

- `NEXT_PUBLIC_API_BASE_URL`
- `NEXT_PUBLIC_FIREBASE_API_KEY`, `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`, `NEXT_PUBLIC_FIREBASE_PROJECT_ID`, `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET`, `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID`, `NEXT_PUBLIC_FIREBASE_APP_ID`

### Steps & production notes

1. Configure the environment variables above.
2. Build and deploy the backend container ([backend/Dockerfile](backend/Dockerfile)); point the frontend's `NEXT_PUBLIC_API_BASE_URL` at it. Full procedure: [docs/backend-deployment.md](docs/backend-deployment.md).
3. **Persistent storage:** the FAISS index lives on local disk. Mount a persistent volume and set `FAISS_DIR` to it for durable semantic search. On ephemeral disks the index is automatically rebuilt from Firestore on startup (correct, but costs a re-embedding pass).
4. **Optional Redis:** set `SEARCH_REDIS_URL` to share the search cache and rate-limit windows across instances.
5. Verify `/health/ready` and run a search before cutover.

Operational runbooks: [backup & disaster recovery](docs/runbooks/backup-dr.md) · [load testing](docs/load-testing.md).

---

## Project Structure

```text
.
├── backend/                 FastAPI service
│   ├── app/
│   │   ├── api/routes/       experiences · search · dashboard · practice · users
│   │   ├── core/            config · firebase · rate limiting · logging
│   │   ├── services/        FAISS store & reconcile · search core · NLP · caches
│   │   └── main.py          app, middleware, startup reconciliation
│   ├── eval/                search-quality harness (recall@k · MRR · nDCG)
│   ├── scripts/             deployment & index utilities
│   └── tests/               unit + concurrency regression tests
├── frontend/                Next.js app (app router, contexts, components)
├── docs/
│   ├── adr/                 architecture decision records
│   ├── runbooks/           backup & disaster recovery
│   ├── architecture.md     system diagram & request flow
│   └── load-testing.md     k6 SLOs
├── loadtest/               k6 load test
└── .github/workflows/      CI + CodeQL
```

---

## Future Improvements

Realistic next steps, deferred deliberately (see the [ADRs](docs/adr/README.md) for rationale):

- **ANN vector index** (IVF/HNSW) once the corpus outgrows exact FlatIP search.
- **Semantic/hybrid evaluation** in CI, extending the current lexical quality gate.
- **Concurrent vector search** by replacing the single FAISS index lock with a read/write lock or snapshot-swap.
- **Distributed tracing & metrics export** (OpenTelemetry) for deeper production observability.
- **Redis circuit breaker** so rate-limit checks skip Redis during an outage instead of retrying.

---

## License

Released under the [MIT License](LICENSE).
