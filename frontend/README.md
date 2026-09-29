# HireLog Frontend

Next.js 16 application for search, contribution, dashboard analytics, and practice workflows.

## Stack
- Next.js 16 + React 19
- TypeScript
- Tailwind CSS 4
- Firebase Auth (client)
- Vitest + Testing Library (unit tests)
- Playwright (e2e smoke tests)

## Local Setup

```bash
npm install
cp .env.example .env.local
# Fill .env.local values
npm run dev
```

Open: `http://localhost:3000`

## Environment Variables

Defined in `.env.example`:

| Variable | Required | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | Yes | Backend base URL, e.g. `http://localhost:8000` |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Yes | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Yes | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Yes | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Yes | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Yes | Firebase web config |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Yes | Firebase web config |
| `NEXT_PUBLIC_E2E_BYPASS_AUTH` | Optional | Use only for local Playwright smoke runs |

## Scripts

| Script | Purpose |
|---|---|
| `npm run dev` | Start local development server |
| `npm run lint` | Run ESLint |
| `npm run type-check` | Run TypeScript checks |
| `npm run test` | Run unit tests in watch mode |
| `npm run test:ci` | Run unit tests once (CI mode) |
| `npm run test:e2e` | Run Playwright smoke suite |
| `npm run build` | Build production bundle |

## Deployment (Vercel)

1. Import repository in Vercel.
2. Set root directory to `frontend`.
3. Configure all required `NEXT_PUBLIC_*` variables.
4. Deploy.

## Key Directories

- `src/app/` App Router routes.
- `src/components/` reusable UI components.
- `src/context/` auth, theme, and toast providers.
- `src/lib/` API client, token helper, Firebase setup, shared types.
- `e2e/` Playwright smoke tests.
- `src/test/` shared unit test setup.
