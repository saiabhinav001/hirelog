# HireLog — Frontend

Next.js 16 app with React 19, Tailwind CSS 4, TypeScript, and Firebase Auth.

## Local Development

```bash
npm install
cp .env.example .env.local
# Edit .env.local with your Firebase config and backend URL
npm run dev
```

Open http://localhost:3000

## Environment Variables

See `.env.example` for the full list. All variables are prefixed with `NEXT_PUBLIC_` for client-side access.

| Variable | Description |
|----------|-------------|
| `NEXT_PUBLIC_API_BASE_URL` | Backend API URL (e.g. `http://localhost:8000`) |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Firebase Web API key |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Firebase Auth domain |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Firebase project ID |
| `NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET` | Firebase storage bucket |
| `NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID` | Firebase messaging sender ID |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Firebase app ID |

## Deploying to Vercel

1. Import this repo in Vercel
2. Set **Root Directory** to `frontend`
3. Add all `NEXT_PUBLIC_*` environment variables in Vercel project settings
4. Deploy

## Project Structure

- `src/app/` — Next.js App Router pages
- `src/components/` — Shared UI components (Navbar, Footer, etc.)
- `src/context/` — React context providers (Auth, Theme, Toast)
- `src/lib/api.ts` — API client with retry logic
- `src/lib/firebase.ts` — Firebase client SDK initialization
- `src/lib/types.ts` — TypeScript interfaces
