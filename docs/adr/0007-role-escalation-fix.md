# ADR 0007 — Fix public profile role-escalation before adding exam authority

**Status:** Accepted

## Context

The Hirelog Exams implementation pack (master contract #2) requires fixing the
baseline public profile role-escalation path before any exam-specific
institutional/faculty authority is added, and explicitly warns against blindly
copying "the legacy role hierarchy or five-minute role cache."

Static inspection of the pre-existing (Prompt 0 baseline) code confirmed a real,
exploitable privilege-escalation path:

- `POST /api/users` accepted a client-supplied `role` field
  (`UserCreate.role: Literal["viewer","contributor","placement_cell"]`).
- The handler computed `role = max(payload.role, stored_role)` by priority, so
  any authenticated viewer could send `{"role": "placement_cell"}` directly to
  the API (bypassing the UI, which only ever sends `"viewer"`) and have it
  persisted via the Admin SDK — which does not go through `firestore.rules`.
- `require_placement_cell` (gating the moderation queue, visibility toggles,
  dashboard admin stats, and search-runtime admin actions) trusts exactly this
  stored field, so the escalation was not just cosmetic — it granted real
  access to privileged endpoints.

The Firestore rules already prevented a *direct client Firestore write* from
changing `role`, but that only protects the Firestore SDK path, not the backend
API path, which is explicitly noted in the pack: "Verify rule behavior in the
emulator and API behavior independently because the Admin SDK bypasses
Firestore client rules."

Separately, the "contributor" role is defined in the priority table and the
`require_contributor` dependency exists, but no route currently depends on
`require_contributor` — experience submission only requires
`get_current_user` (any signed-in user). "Contributor" is therefore inert for
authorization today; this ADR does not change that, since Prompt 1's mandate
is only to remove the ability to *reach* a privileged role via the payload,
not to redesign the (currently unused) contributor gate.

## Decision

1. **Remove `role` from the client-writable DTO entirely.** `UserCreate` no
   longer has a `role` field. A client cannot request any role — privileged or
   not — through this endpoint. If a future feature needs a client-chosen,
   *non-privileged* self-service role, it must be reintroduced as an explicit,
   narrowly-typed field with server-side validation that it can never reach a
   privileged value, not as a reuse of an authority-bearing field.
2. **The route only ever compares server-observed roles.** The "never regress
   an already-privileged user on re-auth" behavior is preserved, but the two
   values compared are now (a) the role `get_current_user` already resolved
   for this request via the `PLACEMENT_CELL_EMAILS` allowlist, and (b) the
   previously stored Firestore value — never anything from the request body.
3. **Firestore rules restrict the writable key set**, not just the `role`
   value, on both `create` and `update` for `users/{userId}`. This is a
   defense-in-depth measure for the *client Firestore* path and also documents
   the intended shape of the document so a future field (e.g. an exam
   authority field) added to the API schema without a matching rule update
   does not silently become client-writable.
4. **Identity verification is separated from cached authorization.**
   `get_current_user` (5-minute in-memory cache, keyed by token hash) remains
   for legacy, lower-stakes routes. A new `get_current_user_uncached`
   dependency always re-verifies the Firebase ID token and re-reads the
   Firestore role fresh, with no TTL cache. New exam authorization routes
   (faculty/proctor/admission checks, in later phases) must use the uncached
   path so a revocation or role change is observed immediately, rather than
   inheriting a role that can be up to five minutes stale.
5. **The frontend E2E auth bypass fails loud in production.**
   `NEXT_PUBLIC_E2E_BYPASS_AUTH` is a build-time flag used only for local
   Playwright smoke runs, where all backend calls are intercepted via
   `page.route(...)` mocks. It never reaches a real backend today, but there
   was no code-level guard against it being left on in a production build —
   only operational discipline. `e2eAuth.ts` now throws at module load if the
   flag is `true` while `NODE_ENV === "production"`, so a misconfigured
   production build fails to start instead of silently bypassing auth.

## Consequences

- **Positive:** the escalation path is closed at the schema layer (cannot be
  reintroduced by a future handler bug reading a field that no longer exists),
  not just by handler logic; existing legitimate role behavior (viewer default,
  email-allowlist promotion to `placement_cell`, no regression on re-auth) is
  unchanged and covered by regression tests; new exam routes have a
  purpose-built, non-cached identity/authorization primitive to build on.
- **Negative / follow-up required:** the Firestore rules change was reviewed
  statically only — no Firebase emulator was available in this environment to
  run `@firebase/rules-unit-testing` against it (no `firebase.json`, no
  Firebase CLI/Java). This must be verified with the emulator before the rules
  file is deployed. The frontend production-guard test was verified by
  inspection and a normal (flag-unset) production build; a CI job that
  actually sets the flag in a production build and asserts the build fails
  has not been added yet.
- The "contributor" role remains defined but unenforced by any route; this is
  pre-existing behavior, unchanged by this ADR, and should be revisited when
  exam faculty/institution roles are introduced (Prompt 2+) so the same
  inert-role trap is not repeated for new roles.

## Migration notes

- No data migration required. No existing user documents are touched; the fix
  only changes what a *future* `POST /api/users` call is allowed to influence.
- Any already-escalated document (if this ever ran in a real deployment before
  the fix) is **not** automatically demoted by this change, since the route
  no longer reads `payload.role` at all but still preserves the max of
  server-observed values — a manually escalated Firestore document would keep
  its `role` until an operator corrects it directly. This is intentionally out
  of scope for a "focused security patch" per the master prompt ("do not
  mass-change users"); operators should audit `users` collection documents
  where `role != 'viewer'` against `PLACEMENT_CELL_EMAILS` before relying on
  this fix alone in an already-deployed system.
- Deploy order: ship the backend (`schemas.py`, `users.py`,
  `dependencies.py`) and `firestore.rules` together — the API fix is the
  primary control; the rules change is defense-in-depth for the client SDK
  path only.
