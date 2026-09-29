# ADR 0010 — Institution identity, roster import, and OTP fallback login

**Status:** Accepted

## Context

Prompt 2 was deferred while Prompts 3, 4, and 5 were built on top of two
explicit, documented stand-ins: a raw UID-list "enrollment snapshot" (ADR
0008 §2) and an `EXAM_FACULTY_EMAILS` config allowlist for faculty authority
(ADR 0008 §3). This ADR implements the real institution/membership/roster/
login layer Prompt 2 asks for, and reconciles it with those stand-ins —
replacing neither outright, but giving both a real alternative source that
coexists with the original bootstrap path.

## Branch topology note

This work is committed on `exams/phase-2-institution-identity`, stacked
**on top of** `exams/phase-5-kt-generation` (not as a sibling of
phase-3-4/phase-5 off of phase-1). Prompt 2 needed to extend files already
committed in phase-3-4 (`app/api/exams_dependencies.py`) — building it after,
on top of, the phases that came first in this session is what makes that a
small, additive edit instead of a rebase. The branch name still reflects the
*prompt number*, not commit order; `main` remains untouched throughout.

## Decisions

### 1. Membership uniqueness via document ID, not a query

`Membership` documents are keyed by `f"{institution_id}::{normalized_identity_key}"`
— Firestore has no native composite-unique index, so the document id itself
*is* the uniqueness constraint. `normalize_identity_key` never coerces to a
number (leading zeros are preserved by construction — the value is a string
from end to end).

### 2. Roster import: dry-run and apply share one pure planner

`roster_import.py` validates duplicate-within-file, unauthorized role grants
(an uploader can never grant a `role_scope` above their own — checked via
`can_grant_role`), and email shape — identically for dry-run (validates
everything, writes nothing) and apply (bounded batches from a persisted
`applied_cursor`). Apply is resumable: calling it again after a crash
re-plans the same slice deterministically and re-applies via upsert
(idempotent by identity_key), never re-appending. Removing a row from a
re-uploaded file is never a delete — there is no delete path in this module
at all; deactivation is the separate, explicit `revoke` action.

### 3. OTP is a short-lived proof, not a second password store

Only an HMAC-SHA256 digest (server secret + per-code salt) is ever persisted
— the plaintext code exists only long enough to hand to the delivery sink.
Single active challenge per identity (a new challenge invalidates the old
one), single-use (consumed on success), attempt-capped, and rate-limited via
the *existing* `SlidingWindowLimiter` (`app.core.rate_limit`) reused as-is —
no new limiter implementation. `challenge()` always returns the same generic
acknowledgement regardless of whether the identity/email combination exists,
preventing enumeration.

### 4. Firebase custom sign-in only after proof; UID policy

A brand-new institutional identity (no prior Firebase account) is issued a
freshly minted `uid` (`exam-{uuid4}`) the first time OTP verification
succeeds, persisted on the membership so subsequent logins reuse it. An
existing legacy Firebase user proves *both* factors — a valid legacy ID
token (existing account ownership) and OTP (institutional identity) — via
`/api/exams/v1/auth/link`, which sets `membership.uid` to the *existing* uid,
preserving it and all legacy ownership. A membership already linked to a
**different** uid is an explicit, logged conflict (`IdentityAlreadyLinkedError`,
HTTP 409) — never silently overwritten; recovery is an operator action, not
automated in this phase.

### 5. Revocation during a session: authorization_version, not token revocation

Every custom token embeds an `exam_authz_version` developer claim, made
readable app-wide via a small additive change to
`app.api.dependencies.get_current_user_uncached` (Prompt 1) — it now also
surfaces `auth_time` and `exam_*` claims already present on the verified
token, without changing behavior for any existing caller. `revoke()` bumps
`Membership.authorization_version`; `check_active_membership` compares the
token's claim against the *current* Firestore value on every request. A
revoked/rotated membership is rejected immediately, even though the
underlying Firebase token remains cryptographically valid until it expires.

### 6. SSO is a contract, not an integration; MFA is honestly scoped

`SsoIdentityProvider`/`UnconfiguredSsoProvider` mirror the Prompt 5 pattern
(real interface, stub implementation) — no SAML/OIDC library or real IdP was
available to integrate/test against. `require_recent_staff_reauth` checks
Firebase's own `auth_time` claim for staff-scoped actions (e.g. revoking a
membership) — this is a recency check, **not MFA**; an email OTP is never
labeled as a second factor, per the master prompt.

### 7. Reconciliation with Prompts 3/4/5 — additive, not destructive

- `app/api/exams_dependencies.py::require_exam_faculty` now accepts **either**
  the original `EXAM_FACULTY_EMAILS` bootstrap grant **or** a real, active
  institution `FACULTY`/`ADMIN` membership — existing pilots using the email
  allowlist keep working unchanged.
- New `require_active_institution_membership` and `require_recent_staff_reauth`
  dependencies were added to the same file (Prompt 3/4's file), extending it
  rather than duplicating auth logic elsewhere.
- `app/domain/exams/institution_enrollment.py` (new file) resolves a real
  `enrolled_uids` list from active, already-logged-in student memberships.
  It calls the existing, unmodified
  `ExamCommandService.create_run(..., enrolled_uids=...)` — Prompt 3's own
  code is untouched; only the *source* of the list changed, exactly the seam
  ADR 0008 reserved.
- `PROMPT 2's /exams/login`: implemented as a minimal, explicitly
  placeholder Next.js page (no design tokens) — Prompt 7 owns the real
  "student screens" and will replace this markup without needing new backend
  endpoints. `/api/exams/v1/me` reports server-confirmed capabilities
  (memberships + bootstrap-faculty flag) for role routing.
- The exam frontend calls a **new**, separate `examApiFetch` client pinned to
  one configured origin (`NEXT_PUBLIC_EXAMS_API_BASE_URL`) — the legacy
  `apiFetch` targets the legacy backend and must not be reused here, since
  the exam API is an independently deployed service. `next.config.ts`'s CSP
  `connect-src` was extended to allow this origin.

## Consequences

- **Positive:** 49 new tests (roster/normalization, OTP, auth-service
  challenge/verify/link/revoke, and the dependency/enrollment reconciliation
  seams) — full suite 215/215 passing. Prompts 3/4/5 required zero core-logic
  changes, only additive extensions in files that already existed for this
  exact purpose.
- **Negative / follow-up required:** no real SSO provider, no real OTP
  delivery provider (email/SMS) — `DevSinkOtpDelivery` logs codes and is
  test/dev-only. `verify_and_sign_in`'s Firestore transaction (uid
  provisioning) is tested with a lightweight in-memory double and a
  patched-through `firestore.transactional`, proving the reuse-vs-mint LOGIC,
  not true concurrent-writer transactional correctness — that requires a
  real Firestore/emulator run, consistent with the same limitation already
  recorded for Prompt 3/4 (ADR 0008 §1). `EXAM_OTP_HMAC_SECRET` unset means
  OTPs do not verify across process restarts or multiple instances — must be
  set for any non-single-instance deployment. No CSV upload endpoint exists
  yet (roster rows are submitted as JSON in the request body) — wiring an
  actual `multipart/form-data` CSV parser onto the existing
  `RosterRowInput`-shaped dry-run/apply endpoints is a small follow-up, not a
  design change.

## Migration / configuration steps

- New settings, all with safe defaults: `EXAM_OTP_HMAC_SECRET` (unset —
  dev-only ephemeral secret, **must** be set before any multi-instance
  deployment), `EXAM_OTP_TTL_SECONDS` (300), `EXAM_OTP_MAX_ATTEMPTS` (5),
  `EXAM_STAFF_REAUTH_MAX_AGE_SECONDS` (900).
- New frontend env vars (both optional): `NEXT_PUBLIC_EXAMS_ENABLED`,
  `NEXT_PUBLIC_EXAMS_API_BASE_URL`.
- New Firestore collections (`institutions`, `institution_memberships`,
  `institution_roster_imports`, `institution_otp_challenges`) — no rules
  changes needed (default-deny catch-all; documentation comment added).
- No data migration. Existing `EXAM_FACULTY_EMAILS`-based deployments are
  unaffected — that grant path is unchanged and still checked first.
