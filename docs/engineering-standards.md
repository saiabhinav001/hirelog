# Engineering Standards

This document defines the naming, casing, and wiring rules used in this repository.

## Naming and Casing

### Python (backend)
- Modules and filenames: `snake_case`
- Variables and functions: `snake_case`
- Classes and Pydantic models: `PascalCase`
- Constants and environment variables: `UPPER_SNAKE_CASE`

### TypeScript and React (frontend)
- Components and React context files: `PascalCase` filenames (for example: `AuthContext.tsx`)
- Utility modules and route helpers: `camelCase` exports
- Variables and functions: `camelCase`
- Types and interfaces: `PascalCase`
- Route segments under `src/app`: lowercase, URL-safe names

### API Payload Casing
- Backend API request and response payloads use `snake_case`.
- Frontend type definitions mirror backend payload keys exactly to avoid translation bugs.
- Do not introduce mixed payload casing (for example, avoid mixing `camelCase` with `snake_case` in the same API contract).

## Wiring Rules

### Auth Token Retrieval
- Frontend API calls must get tokens via `getClientAuthToken` from `src/lib/authToken.ts`.
- Do not call `auth.currentUser.getIdToken()` directly in page modules.
- This guarantees consistent behavior for Firebase auth and e2e bypass mode.

### Environment Variables
- Public frontend variables must be prefixed with `NEXT_PUBLIC_`.
- Backend variables remain `UPPER_SNAKE_CASE` in `.env`.
- Keep env example files minimal and aligned with currently referenced variables only.

### Reliability and Health
- Use `/health/live`, `/health/ready`, and `/health/deep` for runtime checks.
- Keep API and worker split deployment consistent with `SEARCH_INDEX_WORKER_MODE` and queue backend settings.

## File Hygiene

- Do not keep backup files in source directories (for example, `*_backup.*`).
- Remove empty docs and stale duplicates to prevent drift.
- Prefer one source-of-truth sample file per configuration surface.

## Pull Request Checklist

- Naming and casing conventions followed.
- No direct `auth.currentUser` token calls in app pages.
- No unused or duplicate config templates introduced.
- Lint, type-check, tests, and build pass.
