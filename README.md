# HireLog — Campus Placement Interview Archive

A production-grade platform where students share placement interview experiences. The system uses NLP to auto-extract questions, classify topics, generate summaries, and power semantic search — so the next batch can prepare smarter.

**Live:** Frontend on Vercel · Backend API on Render · Data in Firebase Firestore

---

## Architecture

```
┌──────────────┐       HTTPS        ┌───────────────────┐
│   Vercel     │ ──────────────────> │   Render          │
│   Next.js    │   API calls        │   FastAPI + FAISS  │
│   Frontend   │ <────────────────── │   (Python 3.11)   │
└──────┬───────┘                    └────────┬──────────┘
       │                                     │
       │  Firebase Auth (JWT)                │  Firebase Admin SDK
       │  Firestore (client reads)           │  Firestore (read/write)
       ▼                                     ▼
┌──────────────────────────────────────────────────────────┐
│                   Firebase (Google Cloud)                │
│   • Firestore — source of truth for all data            │
│   • Auth — Email/Password + Google sign-in              │
└──────────────────────────────────────────────────────────┘
```

### Key Components

| Layer | Stack | Deployed To |
|-------|-------|------------|
| Frontend | Next.js 16, React 19, Tailwind CSS 4, TypeScript | Vercel |
| Backend API | FastAPI, uvicorn, Python 3.11 | Render Web Service |
| NLP Pipeline | spaCy (en_core_web_sm), sentence-transformers (all-MiniLM-L6-v2) | Render (same service) |
| Vector Search | FAISS (faiss-cpu) with persistent disk | Render Persistent Disk |
| Database | Firestore (NoSQL) | Firebase / Google Cloud |
| Auth | Firebase Auth (Email/Password + Google) | Firebase |

### Core Capabilities

| Capability | Description |
|---|---|
| **AI-powered ingestion** | Cleans raw text, extracts interview questions, classifies topics, generates summaries and vector embeddings automatically |
| **Semantic search** | Intent-based discovery — finds relevant results even when wording differs from the query |
| **Placement Cell analytics** | Topic frequency, difficulty distribution, company-wise breakdowns, repeated questions, interview progression flows |
| **Practice lists** | Save extracted questions to personal lists, track revision status |
| **Compounding knowledge base** | Every contribution is permanent. Data grows richer year after year |
| **Anonymous contributions** | Submit experiences anonymously; identity hidden publicly but preserved for moderation |
| **Tiered dashboard** | Stats load instantly from cache; charts and analytics load lazily |

### Data Flow

1. **Submission** → User posts experience → FastAPI validates and saves to Firestore immediately (< 200ms)
2. **Background NLP** → Separate thread: extract questions, classify topics, generate summary, compute FAISS embedding → writes enrichment back to Firestore
3. **Search** → Semantic (FAISS cosine similarity) or keyword (Firestore query) with filters
4. **Dashboard** → Pre-computed stats cached in Firestore metadata collection, refreshed after each submission

---

## Local Development Setup

### Prerequisites

- Python 3.11 (required for spaCy + FAISS compatibility)
- Node.js 18+
- Firebase project with Firestore + Auth enabled
- Firebase service account key JSON file

### 1. Clone

```bash
git clone https://github.com/saiabhinav001/hirelog.git
cd hirelog
```

### 2. Backend

```bash
cd backend

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\Activate.ps1
# Activate (macOS/Linux)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# Configure environment
cp .env.example .env
# Edit .env — set FIREBASE_SERVICE_ACCOUNT_PATH and FIREBASE_PROJECT_ID

# Initialise FAISS index (first run only)
python scripts/init_faiss.py

# Start dev server
uvicorn app.main:app --reload --port 8000
```

### 3. Frontend

```bash
cd frontend

npm install

# Configure environment
cp .env.example .env.local
# Edit .env.local — set NEXT_PUBLIC_API_BASE_URL and Firebase config

npm run dev
```

Open http://localhost:3000

---

## Production Deployment

### Backend → Render

#### Option A: Blueprint (recommended)

1. Push this repo to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint**
3. Connect the GitHub repo — Render reads `render.yaml` and creates the service + persistent disk automatically
4. Set the secret environment variables in the Render dashboard:
   - `FIREBASE_PROJECT_ID` — your Firebase project ID
   - `FIREBASE_SERVICE_ACCOUNT_JSON` — single-line JSON of your service account key (see below)
   - `ALLOWED_ORIGINS` — your Vercel frontend URL (e.g. `https://hirelog.vercel.app`)

#### Option B: Manual

1. **New Web Service** → connect GitHub repo
2. **Root Directory:** `backend`
3. **Runtime:** Python 3
4. **Build Command:** `pip install -r requirements.txt && python -m spacy download en_core_web_sm`
5. **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
6. **Add Persistent Disk:** Mount path `/data/faiss`, size 1 GB
7. Set environment variables (same as Option A above, plus):
   - `PYTHON_VERSION` = `3.11.9`
   - `ENV` = `production`
   - `FAISS_INDEX_PATH` = `/data/faiss/index.faiss`
   - `FAISS_MAPPING_PATH` = `/data/faiss/mapping.json`

#### Encoding the Firebase Service Account Key

```bash
# macOS / Linux
cat serviceAccountKey.json | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)))"

# Windows (PowerShell)
Get-Content serviceAccountKey.json -Raw | python -c "import sys,json; print(json.dumps(json.load(sys.stdin)))"
```

Copy the single-line JSON output and paste it as `FIREBASE_SERVICE_ACCOUNT_JSON` in Render.

### Frontend → Vercel

1. Go to [Vercel](https://vercel.com) → **New Project** → import the GitHub repo
2. **Root Directory:** `frontend`
3. **Framework Preset:** Next.js (auto-detected)
4. Set environment variables:
   - `NEXT_PUBLIC_API_BASE_URL` — your Render backend URL (e.g. `https://hirelog-api.onrender.com`)
   - `NEXT_PUBLIC_FIREBASE_API_KEY`
   - `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`
   - `NEXT_PUBLIC_FIREBASE_PROJECT_ID`
   - `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET`
   - `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID`
   - `NEXT_PUBLIC_FIREBASE_APP_ID`
5. Deploy

### Firebase Setup

1. Create a Firebase project at [console.firebase.google.com](https://console.firebase.google.com)
2. Enable **Firestore Database** (production mode)
3. Enable **Authentication** → Email/Password + Google sign-in
4. Generate a service account key: **Project Settings → Service accounts → Generate new private key**
5. Deploy Firestore security rules:
   ```bash
   firebase deploy --only firestore:rules
   ```

### Firestore Security Rules

The frontend uses the backend API for all writes, so client writes can be locked down. The backend uses the Firebase Admin SDK (which bypasses rules). See `firestore.rules` in the repo root.

### Firestore Indexes

Firestore will prompt you for missing indexes the first time a query runs. Common composite indexes:
- `interview_experiences`: `company + year`
- `interview_experiences`: `role + difficulty`
- `interview_experiences`: `topics (array) + difficulty`
- `interview_experiences`: `created_by + created_at`

Add any additional composite indexes prompted by the Firebase console.

---

## Environment Variables Reference

### Backend (Render / `.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ENV` | Yes | `development` | `development` or `production` |
| `FIREBASE_PROJECT_ID` | Yes | — | Firebase project ID |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Prod | — | Single-line JSON of service account key |
| `FIREBASE_SERVICE_ACCOUNT_PATH` | Dev | — | Path to service account JSON file |
| `ALLOWED_ORIGINS` | Yes | `localhost:3000,3001,3002` | Comma-separated CORS origins |
| `FAISS_INDEX_PATH` | Prod | `data/faiss/index.faiss` | `/data/faiss/index.faiss` on Render |
| `FAISS_MAPPING_PATH` | Prod | `data/faiss/mapping.json` | `/data/faiss/mapping.json` on Render |
| `EMBEDDING_MODEL` | No | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model name |
| `EMBEDDING_DIM` | No | `384` | Must match the model output dimension |
| `MAX_SEARCH_RESULTS` | No | `20` | Max results per search query |
| `DASHBOARD_SAMPLE_LIMIT` | No | `500` | Max documents for dashboard stats |

### Frontend (Vercel / `.env.local`)

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXT_PUBLIC_API_BASE_URL` | Yes | Backend API URL |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Yes | Firebase Web API key |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Yes | e.g. `project-id.firebaseapp.com` |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Yes | Firebase project ID |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Yes | e.g. `project-id.appspot.com` |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Yes | Firebase sender ID |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Yes | Firebase app ID |

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Service status |
| GET | `/health` | No | Detailed health (FAISS vector count, env) |
| POST | `/api/users/register` | Yes | Register new user |
| GET | `/api/users/me` | Yes | Get current user profile |
| PATCH | `/api/users/me/name` | Yes | Update display name |
| POST | `/api/experiences` | Yes | Submit new experience |
| GET | `/api/experiences/mine` | Yes | Get user's contributions |
| DELETE | `/api/experiences/{id}` | Yes | Soft-delete experience |
| POST | `/api/experiences/{id}/restore` | Yes | Restore soft-deleted |
| PATCH | `/api/experiences/{id}` | Yes | Update experience metadata |
| PATCH | `/api/experiences/{id}/questions` | Yes | Add questions to experience |
| GET | `/api/search` | No | Search (semantic/keyword) |
| GET | `/api/dashboard/stats` | Yes | Dashboard summary stats |
| GET | `/api/dashboard/charts` | Yes | Charts and analytics |
| GET | `/api/dashboard/questions` | Yes | Frequent questions |
| GET | `/api/dashboard/flows` | Yes | Interview progression flows |
| GET | `/api/practice` | Yes | Practice lists |
| POST | `/api/practice` | Yes | Create practice list |

---

## Project Structure

```
hirelog/
├── render.yaml                    # Render Blueprint (IaC)
├── firestore.rules                # Firestore security rules
├── backend/
│   ├── requirements.txt
│   ├── .env.example
│   ├── app/
│   │   ├── main.py                # FastAPI app + lifespan
│   │   ├── core/
│   │   │   ├── config.py          # Pydantic settings
│   │   │   └── firebase.py        # Firebase Admin SDK init
│   │   ├── api/
│   │   │   ├── dependencies.py    # Auth middleware
│   │   │   └── routes/
│   │   │       ├── users.py       # User registration & profile
│   │   │       ├── experiences.py # Experience CRUD + background NLP
│   │   │       ├── search.py      # Semantic + keyword search
│   │   │       ├── dashboard.py   # Analytics dashboard
│   │   │       └── practice.py    # Practice lists
│   │   ├── models/
│   │   │   └── schemas.py         # Pydantic request/response models
│   │   ├── services/
│   │   │   ├── nlp.py             # spaCy + sentence-transformers pipeline
│   │   │   ├── faiss_store.py     # Thread-safe FAISS vector store
│   │   │   └── seed_data.py       # Initial seed data
│   │   └── utils/
│   │       └── serialization.py   # Firestore → JSON helpers
│   ├── data/faiss/                # Local FAISS index (gitignored)
│   └── scripts/
│       ├── init_faiss.py          # Initialize empty FAISS index
│       ├── init_firebase.py       # Bootstrap Firestore collections
│       └── seed_data.py           # Seed sample data
└── frontend/
    ├── .env.example
    ├── package.json
    ├── next.config.ts             # Production headers & caching
    ├── src/
    │   ├── app/                   # Next.js App Router pages
    │   ├── components/            # Shared UI components
    │   ├── context/               # Auth, Theme, Toast providers
    │   └── lib/
    │       ├── api.ts             # API client with retry logic
    │       ├── firebase.ts        # Firebase client SDK init
    │       └── types.ts           # TypeScript interfaces
    └── public/
```

---

## Demo Flow

1. **Sign up** via Email/Password or Google
2. **Dashboard** — see aggregated analytics across all contributions
3. **Submit** — share an interview experience (NLP enrichment runs in background)
4. **Search** — find relevant experiences by topic, company, difficulty, or semantic query
5. **Practice** — save experiences into named lists for targeted revision
6. **Profile** — view your contributions, soft-delete, restore, edit metadata

---

## Resetting After a New Firebase Project

1. Download a new Service Account key JSON
2. Update `backend/.env` with the new path/JSON and project ID
3. Update `frontend/.env.local` with the new Firebase client keys
4. (Optional) Delete `backend/data/faiss/` for a fresh semantic index
5. Re-run `python scripts/init_firebase.py` and `python scripts/seed_data.py`

---

## Security

- Firestore is the source of truth
- Backend verifies Firebase ID tokens for protected endpoints
- Client SDK has read-only access; all writes go through the authenticated API
- Firestore security rules enforce per-user access control
- Anonymous submissions preserve real UID for moderation

---

## License

MIT
