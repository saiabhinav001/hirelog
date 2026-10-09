# ADR 0017 — Server-owned exam role policy, verified staff bootstrap, and resource scope

**Status:** Accepted

## Context

The Prompt 0 audit of the existing exam implementation (`docs/exams/baseline.md`
§5, `docs/exams/requirements-traceability.md`) found that authorization was
structurally incomplete rather than absent:

1. **Membership revocation was not enforced.** `InstitutionAuthService.check_active_membership`
   and the `exam_authz_version` token claim existed (ADR 0010 §5), but no exam
   route called them, so a revoked student or faculty member with a still-valid
   Firebase token kept access until the token expired.
2. **Any faculty could act on any resource.** `require_exam_faculty` proved a
   *role*, never ownership: any faculty account could open another teacher's
   exam definition, answer keys, submissions, camera-evidence view tickets and
   exports, create institutions and import/revoke any institution's roster.
3. **The `EXAM_FACULTY_EMAILS` allowlist trusted an unverified e-mail string**
   and was not bound to a stable identity.
4. **(Found while implementing this ADR.)** The dependency read the allowlisted
   e-mail from `users/{uid}.email`, which a client may write when it creates its
   own profile (`firestore.rules` allows the `email` key on create). A forged
   stored e-mail could therefore have granted exam authority.

The master prompt requires server-owned roles `student`, `teacher`,
`super_admin`, bootstrap super-admins granted only through verified
authentication and a server-side allowlist, bound to stable UIDs and logged, and
every exam endpoint validating identity, active membership, institution/course
scope, resource ownership, role version and exam state. It forbids a second role
store and forbids mass-editing production users.

## Decisions

### 1. One policy, computed on the server, reusing the existing role model

`app/domain/authz/policy.py` is a pure module (no Firestore, no FastAPI). It
maps the **existing** `RoleScope` / `institution_memberships` model onto the
three required roles instead of adding a parallel store:

| Exam role | Source (server-side only) |
|---|---|
| `student` | ACTIVE `RoleScope.STUDENT` membership; access to a run is then decided by enrollment |
| `teacher` | verified `EXAM_FACULTY_EMAILS` entry, or ACTIVE `FACULTY` / `ADMIN` membership |
| `super_admin` | verified `EXAM_SUPER_ADMIN_EMAILS` entry; implies `teacher` |

`RoleScope.PROCTOR` still grants nothing. The only inputs are the **verified
token e-mail**, the caller's memberships and the resource being touched. No
request field and no `users/{uid}` field (including `role` and `email`) is ever
an input.

### 2. Verified, UID-bound, audited staff bootstrap

An allowlisted address counts only when the token carries `email_verified=true`.
The first UID that proves it is bound in `exam_staff_bindings/{sha256(email)}`
through an atomic `create()` (first writer wins). A **different** UID presenting
a bound address gets no authority, and the attempt is audited in
`exam_authz_audit` (throttled per process). Successful first binding is audited
too. If the binding store cannot be reached the request fails closed (503); no
authority is ever granted on an unverifiable binding. Authority itself is
recomputed from the live allowlist on every request, so removing an address
takes effect on the next request.

`EXAM_ALLOW_UNVERIFIED_STAFF_EMAIL` is a development escape hatch for e-mail /
password emulator accounts; it is ignored (and an error is logged once) when
`ENV=production`. No address appears in source or committed configuration.

### 3. Token e-mail, never the stored profile e-mail

`get_current_user_uncached` now surfaces `token_email`, `email_verified` and
`sign_in_provider` from the verified Firebase token. Exam authority reads only
these. `users/{uid}.email` remains display data. `firestore.rules` is
deliberately unchanged in this phase; see Consequences.

### 4. Membership freshness is enforced on every exam route

`get_current_exam_user` (the dependency behind every authenticated exam route)
applies `check_token_membership_freshness` whenever the session carries
`exam_institution_id`: a revoked or removed membership returns 403
`membership_not_active`; a token older than a revocation bump returns 403
`stale_authorization_version`. Memberships are loaded once per request and
memoized on the request's user dict. Legacy sessions without exam claims incur
no membership lookup.

### 5. Resource scope as dependencies, not conventions

`app/api/exams_scope.py` adds the missing half of authorization. A teacher may
act on a resource as its **owner**, as an **ADMIN of the resource's
institution**, or as a **super_admin**. Ownership chains are resolved
server-side: appeal / snapshot / export / review / submission → run →
definition; candidate / generation request → blueprint → definition; approved
source → upload (for sources created before `created_by` existed). A missing
resource is 404; an existing resource the caller may not touch is a generic 403.
Routes whose id arrives in the body call the same `authorize_*` helpers inline.
Students are checked on every request by `authorize_student_for_run`: the run
exists, the caller is enrolled, and — when the run belongs to an institution —
the caller holds a **current, ACTIVE student membership** in it.

Ticket redemption routes (`/faculty/gradebook/export/download/{ticket_id}`,
`/faculty/proctoring/view/{ticket_id}`, the student snapshot upload ticket, the
live socket) are intentionally not scoped by id: the ticket is minted only by a
route that **is** scoped and is bound to the issuing identity.

### 6. Creation-time scope and roster authority

> Amended by [ADR 0018](0018-roster-linking-otp-and-teacher-assignment.md) §5: institution
> ADMINs no longer grant ADMIN/FACULTY through the roster — that is a super-admin
> function; ADMINs and FACULTY manage STUDENT rows.

`institution_id` is added as an **optional** field on `ExamDefinition` and
`ExamRun`, and `created_by` on `ApprovedSource` (non-strict models; legacy
documents read as `None`, no backfill). A new definition defaults to the
teacher's only teaching institution, requires an explicit choice when they teach
in several, and may be unscoped only for bootstrap/pilot teachers with no
membership. A run inherits its definition's institution and, when scoped, may
enroll only that institution's ACTIVE students. Institution creation is
`super_admin` only; roster import/apply is allowed to institution ADMINs (up to
ADMIN) and FACULTY (STUDENT rows only); revocation is ADMIN/super-admin only
and still needs recent re-authentication.

### 7. Secure by default, no compatibility switch

The module is behind `EXAMS_ENABLED=false` and not in production use, so per
`docs/exams/compatibility-and-migration.md` §3 the strict behavior is the
default rather than shipping a flag that reproduces the defect. The migration
notes list the configuration operators must set.

### 8. Test strategy: API authorization and database rules are separate layers

The exam API uses privileged server SDKs that bypass `firestore.rules`, so every
check must hold in the API on its own. Coverage lives in
`backend/tests/test_exam_authz_*.py` and `test_exam_staff_binding.py`: pure
policy, dependency behavior (forged role / e-mail, stale token, revoked role,
legacy login), resource-scope matrices, route-table introspection that fails when
a route or path parameter lacks its scope dependency, an OpenAPI check that no
request body can carry a role/authority field, and static checks of
`firestore.rules`.

## Consequences

- **Positive:** the four defects above are closed behind tests (137 new tests;
  full backend suite 627 passing). Existing routes, schemas, collections and
  services are reused; additive optional fields only; no production user is
  edited. A route added later without a scope dependency fails the route-table
  guard.
- **Behavior changes operators will notice:** teachers see and act only on their
  own resources (plus their institution's, for ADMINs); `list_sources` is
  per-owner; unverified e-mail/password accounts no longer receive staff
  authority; institution creation, roster and revoke authority is narrower.
- **Residual risks / follow-ups (not closed here):**
  - `verify_id_token` is not called with `check_revoked=True`, so a Firebase
    session revoked at the provider remains valid until expiry; exam membership,
    version and allowlist changes still take effect immediately.
  - The legacy placement-cell promotion in `app.api.dependencies` still trusts an
    allowlisted e-mail without verification or UID binding (decision D-11). It is
    outside the exam module and deliberately not changed here, because enforcing
    verification would lock out legacy e-mail/password users who were never sent a
    verification mail. Recommendation: send verification mail at legacy signup (or
    require Google sign-in) first, then apply the same verified-e-mail rule.
  - `firestore.rules` still lets a client set `users/{uid}.email` at creation.
    The value is display-only for exam authority, but tightening the rule needs
    emulator-backed rules tests (Phase 11).
  - The first-writer-wins binding relies on Firestore's `create()` precondition;
    the unit tests prove the logic with a stand-in, not concurrent writers
    (needs an emulator / staging run). Conflict-audit throttling is per process.
  - `/live/ws` is authorized when the ticket is minted; an already-open socket of
    a student revoked afterwards keeps receiving read-only events until it
    closes (every mutation is re-checked per request).
  - `list_pending_short_answer_reviews` filters the existing global pending list
    after the fact, so a teacher may see fewer than the page limit; a per-run
    query is a follow-up.
  - One membership query per request (plus one binding read for allowlisted
    staff) is added; there is deliberately no cache, to keep revocation
    immediate.
- **Open decisions:** the master prompt names four super-admin addresses but
  lists three (D-1); the operator supplies the final list through
  `EXAM_SUPER_ADMIN_EMAILS`.

## Migration / configuration steps

See `docs/exams/compatibility-and-migration.md` §6 for the full list. In short:
set `EXAM_SUPER_ADMIN_EMAILS` (and review `EXAM_FACULTY_EMAILS`) in the exam API
environment; staff sign in with a verified address (Google sign-in or verified
e-mail); no data migration or backfill is required; rollback is reverting the
commit (new collections and optional fields are inert).
