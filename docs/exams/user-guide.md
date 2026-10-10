# Hirelog Exams — End-to-End User Guide

A practical, verified-by-hand walkthrough of running the exams module locally:
environment setup, institution/admin bootstrapping, the full faculty
(teacher) workflow for building and running an exam, and the full student
workflow for taking one. Every step here has been exercised live against a
real Firebase project during development — not just read from the code.

> This module is independent of the legacy HireLog search/dashboard app. It
> has its own backend process (`app.exam_main:app`, port `8020` by default)
> and lives under the `/exams/*` frontend routes (port `3000`).

---

## 1. Running it locally

### Prerequisites
- Python 3.12 + the backend's virtualenv (`backend/.venv`) with
  `pip install -r requirements.txt -r requirements-dev.txt`.
- Node.js + `npm install` inside `frontend/`.
- A Firebase project (Authentication + Firestore enabled) — either a real
  one or the Firestore emulator.

### Backend `.env` (in `backend/`)
```
ENV=development
FIREBASE_SERVICE_ACCOUNT_PATH=<path to your service-account JSON>
FIREBASE_PROJECT_ID=<your-firebase-project-id>
ALLOWED_ORIGINS=http://localhost:3000
EXAMS_ENABLED=true
EXAM_FACULTY_EMAILS=<comma-separated teacher emails — see §2>
```
Never commit this file or the service-account JSON — both must stay out of
git (already covered by this repo's `.gitignore`).

### Frontend `.env.local` (in `frontend/`)
```
NEXT_PUBLIC_FIREBASE_API_KEY=...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=...
NEXT_PUBLIC_FIREBASE_PROJECT_ID=...
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=...
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=...
NEXT_PUBLIC_FIREBASE_APP_ID=...
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_EXAMS_ENABLED=true
NEXT_PUBLIC_EXAMS_API_BASE_URL=http://localhost:8020
```
(All of these values come from Firebase Console → Project Settings → your
web app's config.)

### Start both servers
```powershell
# Backend (exams API), from backend/
.venv\Scripts\python.exe -m uvicorn app.exam_main:app --host 127.0.0.1 --port 8020

# Frontend, from frontend/
npm run dev
```
Then open `http://localhost:3000/exams/faculty` (teacher) or
`http://localhost:3000/exams/student` (student).

**Local testing note:** `backend/.env`'s `EXAMS_ENABLED=true` is loaded by
every local `pytest` run too. If you're running the backend test suite,
override it: `$env:EXAMS_ENABLED="false"` before `pytest` (CI is unaffected
since it has no `.env` file).

---

## 2. Admin / institution setup

There are two ways to grant **faculty** access. There is currently only one
practical way to grant **student** access (see the limitation noted below).

### Fastest path — bootstrap faculty by email (recommended for local testing)
Add the teacher's account email to `EXAM_FACULTY_EMAILS` in `backend/.env`
(comma-separated for multiple teachers), restart the backend. Anyone signing
in with that email is instantly treated as faculty — **with full admin
authority** on the institution endpoints too — no institution or roster
needed at all. This is the path used throughout development and the
quickest way to try the app yourself.

### Full path — real institutions + roster import (for actual deployments)
There is no admin web UI for this yet — it's API-only (`curl`/Postman/a
script), authenticated as a bootstrap faculty user:

1. **Create an institution**
   ```
   POST /api/exams/v1/faculty/institutions
   { "name": "Example University" }
   ```
   → returns `{ "id": "<institution_id>", ... }`

2. **Validate a roster (dry run)** — one row per person:
   ```
   POST /api/exams/v1/faculty/institutions/<institution_id>/roster/dry-run
   {
     "rows": [
       { "identity_key": "STU001", "contact_email": "student1@example.edu", "role_scope": "student" },
       { "identity_key": "FAC001", "contact_email": "teacher1@example.edu", "role_scope": "faculty" }
     ]
   }
   ```
   → returns a job with a `job_id` and per-row validation results.

3. **Apply the roster** (creates the actual memberships):
   ```
   POST /api/exams/v1/faculty/institutions/<institution_id>/roster/<job_id>/apply
   { "rows": [ ...same rows... ] }
   ```

4. Enrolled people then sign in at **`/exams/login`** (a *separate* login
   page from the regular `/login`) using their `institution_id` +
   `identity_key` + `contact_email` → an OTP code is sent and verified
   there, independent of a Firebase email/password account.

### ⚠️ Known limitation: student access has no email-based bootstrap
Faculty has `EXAM_FACULTY_EMAILS` as a zero-setup shortcut; **students do
not** — `isStudent` strictly requires an **active institution membership**
with `role_scope: "student"` (see `frontend/src/lib/examAuth.tsx`). For
local testing without the full roster flow, the fastest workaround is a
one-off script that directly creates an `Institution` + a `Membership`
(`role_scope=STUDENT`, `status=ACTIVE`) for your test student's Firebase
UID. There's no shortcut around this today.

---

## 3. Faculty (teacher) guide

Sign in at `/exams/faculty` with a faculty-authorized account (see §2).

### Step 1 — Create an exam
On `/exams/faculty`, type a title (e.g. *"Midterm — Data Structures"*) and
click **Create**. This opens the exam's workspace page.

### Step 2 — Add reference material
In **Reference materials**, paste a URL (e.g. a lecture-notes page or
Wikipedia article) and click **Add reference link**, then click **Approve**
on it. Only approved sources can be used for question generation — this is
a deliberate rights/quality gate, not a formality you can skip.

### Step 3 — Create a question blueprint
In **Question blueprint**, give it a title, pick a **question type**:
- **Single choice (MCQ)**, **Multiple select**, or **Short answer** — no
  extra config needed.
- **Coding** — also pick a language (Python 3.11 / Java 17 / C++17).

Click **Create blueprint**. Each blueprint currently always creates exactly
**one question slot** (medium difficulty, 5 marks, 4 requested variants —
these aren't configurable from the UI yet).

> **⚠️ Known limitation:** if you create **more than one blueprint** under
> the same exam intending a multi-question test, be aware that "Assign
> questions to students" (Step 8) currently reserves each blueprint into the
> **same slot 0** for a run — the last one processed wins, silently
> overwriting the earlier one for that student. For now, treat **one
> blueprint = one exam's worth of questions** and use **separate runs** if
> you want students to see different question types. This is a genuine gap
> in the current slot-assignment logic, not a workflow you're doing wrong.

### Step 4 — Generate questions
Click **Generate questions** on the blueprint card. This calls the
configured generation provider (a deterministic fake provider in local/dev
— no real AI model call unless `EXAM_GENERATION_PROVIDER_BASE_URL`/`_API_KEY`
are configured) to draft candidate questions from your approved sources.
A few seconds later, 4 draft candidates appear, each `ready_for_human_review`.

### Step 5 — Review candidates
For each candidate, read the `public_prompt` text and click **Approve** or
**Reject**. Only `approved` candidates can ever be served to a student.

### Step 6 — Publish a revision
In **Finalize & schedule**, set **Slot count** (how many question slots the
published revision exposes — match this to how many blueprint slots you
actually populated) and click **Publish revision**. This creates an
immutable, versioned snapshot — re-publishing later creates revision 2, 3,
etc.; it never edits a prior one.

### Step 7 — Schedule a session (a "run")
Still in **Finalize & schedule**, enter the **enrolled student UIDs**
(comma-separated Firebase UIDs — find these in Firebase Console → Authentication,
or from your own sign-up flow) and click **Schedule session**. This creates
a new run in `draft` status, visible under **Scheduled sessions**.

### Step 8 — Lobby: get the run ready, then release it
Click **Lobby** on the new session. In order:
1. **Assign questions to students** — reserves one approved candidate per
   enrolled student for each blueprint slot. Safe to click multiple times
   (fills in anything missing; never un-assigns existing ones).
2. **Publish run** — a *run-level* status transition (draft → published),
   separate from Step 6's revision publish. Required before release.
3. **Release to students** — opens a confirmation dialog (shows scope,
   version, consequence) before actually releasing. Once released, enrolled
   students can begin admission immediately.

### Step 9 — Live control room
Click **Live control** on a released session to see enrolled students'
live attempt status, warnings, and current question — plus **Pause**,
**Resume**, **Close**, and **End** controls (each behind its own
confirmation dialog explaining the consequence).

### Step 10 — Results
Once students submit, click **Results** on the session to see grades/scores
per student (MCQ auto-graded; coding auto-graded against hidden tests;
short-answer may need manual review depending on configuration).

---

## 4. Student guide

Sign in at `/exams/student` (same `/login` as everywhere else, unless your
institution uses the OTP flow at `/exams/login` — see §2). You'll only see
exams where you're an enrolled, approved student (see the §2 limitation).

### Preflight — before you can begin
Click **Begin** on an assigned exam. You'll land on a checklist that gates
the actual **Begin exam** button until *everything* below is satisfied:
- ✅ **Notice** — read and check "I have read and understood this notice."
- ✅ **Lockdown checklist** — two self-declared checkboxes ("closed other
  apps/tabs", "disabled interfering extensions" — the browser can't verify
  either of these, they're honor-system), plus a real **Enter fullscreen**
  step (skipped automatically if your browser doesn't support the
  Fullscreen API).
- ✅ **Rehearsal** — confirm you can see/interact with the page.

### Taking the exam
Each exam shows **one question at a time** — you cannot go back to a
previous question once you've moved on.

- **Single/multiple choice** — click the radio buttons / checkboxes.
- **Short answer** — type into the text box.
- **Coding** — a real syntax-highlighted code editor (Python/Java/C++),
  with:
  - A **language** dropdown (if the question allows more than one).
  - **Run** — executes your code against the question's sample test cases;
    results appear under the **Test Result** tab (the **Testcase** tab shows
    the sample inputs/expected outputs up front).
  - Final grading (on submit) runs against additional hidden tests you
    never see — passing the visible sample tests is a sanity check, not a
    guarantee of full marks.

Your answer **autosaves** a couple of seconds after you stop typing/changing
it (see "Save status" in the header) — only the final **Submit exam** is
irreversible.

### While you're being monitored
The header bar always shows: time remaining, connection health, camera
status, save status, a **Warnings** counter, and the delivery profile. A few
things happen automatically and are logged (visibly, via an on-screen
banner, and to the faculty-visible incident ledger):
- Switching browser tabs/windows or losing focus.
- Exiting fullscreen — a **"Resume fullscreen"** button appears until you
  click it.
- Attempting to copy or paste **anywhere on the exam page**, including
  inside the code editor — it's blocked outright, not just logged.

None of this can *literally* stop you from switching tabs or closing other
apps (no webpage can do that) — it's detection and an audit trail for your
institution's policy, not an OS-level lock.

### Submitting and results
Click **Submit exam** → confirm in the dialog → you'll land on a **receipt**
page (receipt ID, finalized timestamp, number of questions sealed). Results
appear on the **Results** page once your institution releases them — not
automatically right after you submit.

---

## 5. Appendix

### Glossary
| Term | Meaning |
|---|---|
| Exam definition | The top-level container for one course's exam (title, owner). |
| Reference material / KT source | Approved source content used to generate questions. |
| Blueprint | Defines question slots (type, difficulty, marks) for a definition. |
| Candidate | One AI-drafted question, pending faculty approval. |
| Revision | An immutable, versioned snapshot of a definition's approved content. |
| Run / session | One scheduled sitting of an exam for a specific group of students. |
| Variant assignment | Which specific approved candidate a given student sees for a given slot. |

### Known limitations (honest, not hidden)
- No admin web UI for institution/roster management — API calls only.
- No email-based bootstrap for students (unlike faculty's `EXAM_FACULTY_EMAILS`).
- Multiple blueprints per definition can collide on the same run slot during
  "Assign questions to students" (see §3, Step 3).
- Blueprint creation is fixed at 1 slot, medium difficulty, 5 marks, 4
  variants — not yet configurable from the UI.
- "Managed lockdown" (real Safe Exam Browser integration) exists at the
  protocol level but has never been exercised against a real SEB
  installation — see `docs/adr/0015-pwa-accessibility-and-managed-lockdown.md`.
