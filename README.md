# The Placement Archive

**An AI-powered placement intelligence system for CBIT that converts unstructured interview memories into structured, searchable, and analyzable institutional knowledge.**

Every placement season, graduating students accumulate invaluable interview experience — questions asked, rounds faced, topics tested — that is typically lost after the batch leaves. The Placement Archive captures this knowledge permanently, enriches it with NLP, and makes it discoverable through semantic search and analytics, so every future batch starts better prepared than the last.

## Core Capabilities

| Capability | What it does | Why it matters |
|---|---|---|
| **AI-powered ingestion** | Cleans raw text, extracts interview questions, classifies topics, generates summaries and vector embeddings automatically. | Eliminates manual curation; raw memories become structured intelligence. |
| **Semantic search** | Intent-based discovery over real interview experiences — finds relevant results even when wording differs from the query. | Students search by *meaning*, not keywords: "questions about concurrency in Java" surfaces OS, threading, and multithreading experiences. |
| **Placement Cell analytics** | Topic frequency, difficulty distribution, company-wise breakdowns, repeated questions, and interview progression flows. | A decision-support dashboard for the Placement Cell to identify trends, gaps, and high-impact preparation areas. |
| **Practice lists** | Save extracted questions to personal lists, track revision status (unvisited → practicing → revised). | Bridges insight to action — search becomes preparation. |
| **Compounding knowledge base** | Every contribution is permanent. Data grows richer year after year as seniors contribute back. | Unlike ephemeral WhatsApp groups, the archive is cumulative — 5 years of data is exponentially more valuable than 1. |
| **Anonymous contributions** | Contributors can submit experiences anonymously. Identity is hidden publicly but preserved for moderation. | Encourages honest, detailed accounts without social pressure. |

## System Architecture

```
┌─────────────────┐       ┌──────────────────────────────────────────────┐
│   Next.js UI    │──────▶│             FastAPI Backend                  │
│  (App Router)   │◀──────│                                              │
│  Tailwind CSS   │       │  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  Firebase Auth  │       │  │  NLP /   │  │  FAISS   │  │ Firestore │  │
│  Client SDK     │       │  │  spaCy   │  │  Vector  │  │  (Source  │  │
│                 │       │  │ Pipeline │  │  Index   │  │  of Truth)│  │
│                 │       │  └──────────┘  └──────────┘  └───────────┘  │
└─────────────────┘       └──────────────────────────────────────────────┘
```

- **Frontend:** Next.js (App Router), Tailwind CSS, Firebase Client SDK.
- **Backend:** FastAPI, Firebase Admin SDK, Sentence Transformers, spaCy, FAISS.
- **Database:** Firestore (source of truth).
- **Search:** FAISS semantic index with vector-to-document mapping + keyword fallback.
- **Auth:** Firebase Authentication with Email/Password + Google. Role-aware access (`viewer` → `contributor` on first submission).

## Repository Layout
- `frontend/` — Next.js app (UI, auth, search, practice lists)
- `backend/` — FastAPI service (NLP pipeline, search, analytics, data layer)

## Prerequisites
- Node.js 18+
- Python 3.10–3.12 (recommended: 3.11). spaCy + FAISS wheels are not available yet for 3.13+ on Windows.
- Firebase project with Firestore + Authentication enabled

## Firebase Setup
1. Create a Firebase project.
2. Enable Authentication providers: Email/Password + Google.
3. Create a Firestore database in production or test mode.
4. Generate a Service Account key JSON from Firebase Console.
5. Update both `backend/.env` and `frontend/.env.local` if you switch to a new Firebase project.

## Firestore Rules (Recommended)
The frontend uses the backend API for all writes, so client writes can be locked down. The
backend uses the Firebase Admin SDK (which bypasses rules), so role upgrades and experience
creation are handled server-side. Paste the following rules into Firebase Console → Firestore → Rules:

```text
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    function signedIn() {
      return request.auth != null;
    }

    match /users/{userId} {
      allow read: if signedIn() && request.auth.uid == userId;
      allow create: if signedIn() && request.auth.uid == userId;
      // Role upgrades are managed server-side via the backend admin SDK.
      // Client writes only touch name/email; role changes are rejected client-side.
      allow update: if signedIn() && request.auth.uid == userId;
    }

    match /interview_experiences/{docId} {
      allow read: if signedIn();
      // Contributions are created through the backend API (admin SDK),
      // which auto-upgrades viewers to contributors on first submit.
      allow create: if signedIn()
        && request.resource.data.created_by == request.auth.uid;
      allow update, delete: if signedIn()
        && resource.data.created_by == request.auth.uid;
    }

    match /practice_lists/{listId} {
      allow read, write: if signedIn() 
        && request.auth.uid == resource.data.user_id;
      allow create: if signedIn() 
        && request.resource.data.user_id == request.auth.uid;
      
      match /questions/{questionId} {
        allow read, write: if signedIn() 
          && get(/databases/$(database)/documents/practice_lists/$(listId)).data.user_id == request.auth.uid;
      }
    }

    match /metadata/{docId} {
      allow read: if signedIn();
      allow write: if false;
    }

    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```

## Firestore Indexes
Firestore will prompt you for missing indexes the first time a query runs. For this app, the most
common composite indexes are:
- `interview_experiences`: `company + year`
- `interview_experiences`: `role + difficulty`
- `interview_experiences`: `topics (array) + difficulty`
Add any additional composite indexes prompted by the Firebase console.

## Backend Setup
Recommended (Windows, clean setup with no dependency warnings):
```bash
cd backend
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_windows.ps1
```

Manual setup (Windows, force Python 3.11):
```bash
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\activate
$env:PIP_DISABLE_PIP_VERSION_CHECK=1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Create `backend/.env` using `backend/.env.example`:
```bash
FIREBASE_SERVICE_ACCOUNT_PATH=path/to/serviceAccountKey.json
FIREBASE_PROJECT_ID=your-firebase-project-id
ALLOWED_ORIGINS=http://localhost:3000
```

Initialize Firebase + FAISS (optional, handled on startup):
```bash
python scripts/init_firebase.py
python scripts/init_faiss.py
```

Seed demo data (auto-runs on first startup, idempotent):
```bash
python scripts/seed_data.py
```

## Resetting After a New Firebase Project
If you delete or recreate your Firebase project:
1. Download a new Service Account key JSON.
2. Update `backend/.env` with the new `FIREBASE_SERVICE_ACCOUNT_PATH` and `FIREBASE_PROJECT_ID`.
3. Update `frontend/.env.local` with the new Firebase client keys.
4. (Optional) Clear the FAISS index at `backend/data/faiss/` if you want a fresh semantic index.
5. Re-run `python scripts/init_firebase.py` and `python scripts/seed_data.py`.

Run the API:
```bash
uvicorn app.main:app --reload --port 8000
```

## Frontend Setup
```bash
cd frontend
npm install
```

Create `frontend/.env.local` using `frontend/.env.local.example`:
```bash
NEXT_PUBLIC_FIREBASE_API_KEY=...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=...
NEXT_PUBLIC_FIREBASE_PROJECT_ID=...
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=...
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=...
NEXT_PUBLIC_FIREBASE_APP_ID=...
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

After changing `.env.local`, restart the Next.js dev server.

Run the UI:
```bash
npm run dev
```

## Demo Flow

The recommended demo sequence reinforces the full intelligence lifecycle:

1. **Contribute** — Sign up as a contributor, submit 2–3 real interview experiences in plain text.
2. **Discover** — Use semantic search ("questions about OS concepts in service companies") to surface relevant experiences by intent, not exact keywords. Apply filters (company, year, difficulty) to narrow results.
3. **Analyze** — Open the Placement Analytics dashboard. Show topic frequency, difficulty distribution, most-repeated questions, and company-wise interview progression — framing it as a decision-support view for the Placement Cell.
4. **Act** — Save high-priority questions to a practice list. Show status tracking (unvisited → practicing → revised) and granular progress counts.
5. **Compound** — Explain that every contribution is permanent: next year's batch inherits this year's data, making the archive more valuable over time.

**Key talking points for judges:**
- This is an *institutional knowledge system*, not a personal prep tool.
- Semantic search enables *intent-based discovery* — students describe what they're looking for in natural language and the system finds relevant experiences even when wording differs.
- The analytics dashboard is a *decision-support system* for the Placement Cell: which topics to emphasize in training, which companies test what, how difficulty trends over years.
- The data *compounds*: 5 batches of contributions create a knowledge base that no single student could build alone.

## Deployment Notes
- **Frontend (Vercel):** set the same `NEXT_PUBLIC_*` env vars in Vercel.
- **Backend (Render/Railway):** set `FIREBASE_SERVICE_ACCOUNT_PATH` to a mounted secret file and `FIREBASE_PROJECT_ID`.
- Ensure the backend can write to `backend/data/faiss` (persistent disk in production).

## Notes for FAISS on Windows
If `faiss-cpu` fails to install on Windows, use WSL or a conda environment:
```bash
conda install -c pytorch faiss-cpu
```

## Security
- Firestore is the source of truth.
- The backend verifies Firebase ID tokens for protected endpoints.
- Client SDK has read-only access; all writes go through the authenticated API.
