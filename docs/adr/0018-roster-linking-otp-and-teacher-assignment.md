# ADR 0018 — Roster import, account linking, OTP delivery and teacher assignment

**Status:** Accepted

## Context

Prompt 2 asks for institution/course membership and roster management on top of the
existing institution domain (ADR 0010): CSV import that is bounded, resumable and
preserves leading zeros, per-institution normalization, duplicate/colliding-identity
detection, row-level errors, OTP delivery through a *configured* provider, account
linking that proves both identities and keeps the existing UID, conflict recovery with
audit, and teacher assignment by a super-admin with course-scoped capabilities.
Prompt 0 classified the open rows as ID-03, ID-05, ID-06, ID-07, ID-08, ID-09 and
AUTH-13 (`docs/exams/requirements-traceability.md` §2).

Reading the existing code against that list found defects that mattered more than the
missing endpoints:

1. **A roster import could take over an identity.** Apply upserted the whole membership:
   it replaced `contact_email` (the OTP destination, and the OTP is what yields a
   session for the linked uid — including a linked legacy account) and `role_scope` for
   any existing identity, whatever the uploader's own standing. A FACULTY uploader could
   demote an ADMIN or re-point a student's e-mail.
2. **A roster import silently undid revocations** (`status` was always written ACTIVE).
3. **A full-document write could erase a concurrent login** — the `uid` read moments
   earlier was written back, so a student signing in during an import could be
   re-provisioned a different uid and lose their attempts.
4. **Apply was not tied to the dry-run** (it re-validated whatever rows it was sent) and
   judged only one batch at a time, so a duplicate in a later batch overwrote the first.
5. **OTP delivery had no "configured provider" concept**: the dev sink was the default in
   production and logged one-time codes.
6. **A code could be consumed twice** by concurrent requests (check, then consume).
7. **Linking overwrote with a plain write**, persisted no audit, left two identities
   possible on one uid (making `check_active_membership` ambiguous) and had no recovery.

## Decisions

### 1. CSV is a second input format on the same routes
`POST …/roster/dry-run` and `…/roster/{job_id}/apply` keep their paths and JSON body
and additionally accept `multipart/form-data` with a CSV `file` (and an optional
`batch_size`). `domain/institution/roster_csv.py` is a pure parser: UTF-8 (BOM
tolerated), ≤ 1 MiB, ≤ 5000 rows, header-driven with exactly the columns
`identity_key`, `contact_email`, optional `role_scope`. An unknown column (a `password`
column, say) rejects the file rather than being ignored. File problems reject the file
with a code; a bad row becomes a row-level error carrying its CSV line number, so it
never hides the rest of the report. Every cell stays a string, which is what keeps
leading zeros. The institution scope check runs before the body is read.

### 2. Per-institution normalization is an immutable policy
`Institution.identity_policy` (optional) adds `remove_characters`, `min_length`,
`max_length` and `charset` (`any`/`alphanumeric`) on top of the universal
trim/collapse/UPPERCASE step. It is set at creation and never edited, because membership
document ids embed the normalized key. The output is always a fixed point of the
universal step, so the repository can keep deriving ids without the policy. Control and
invisible characters are rejected on import so two identical-looking IDs can never be
different rows. Formatting variants that normalize together are reported as duplicates
against the first row.

### 3. The planner judges the whole file and the stored state
Structure (normalization, duplicates, roles, e-mail shape) is decided over the entire
file even when one batch is applied. Against stored memberships an import may add rows
and edit what its uploader may edit; it may **not** undo a revocation
(`membership_revoked_reinstate_required`), modify a member who outranks the uploader
(`uploader_not_authorized_to_modify`), or change an existing contact e-mail without
administrator authority (`contact_email_change_requires_admin`). An identical row is
`skip_unchanged`, so replaying an import is a no-op. Apply must present rows whose
digest equals the dry-run's (`409 roster_rows_changed` otherwise).

### 4. Writes are field-safe, re-checked and audited
Creating a student is an atomic create. Anything that changes who an identity is (role,
contact e-mail) re-reads the membership inside a transaction, re-applies the same pure
rules, bumps `authorization_version`, and commits its audit event with the change.
`uid` and `status` are never rewritten by a roster. Results are de-duplicated by row
index, so a replayed batch replaces rather than repeats. One batch event records counts
only.

### 5. Teacher assignment is a super-admin function
`max_grantable_role_scope` is now `ADMIN` for a super-admin and `STUDENT` for everyone
else (this amends ADR 0017 §6, where institution ADMINs could grant up to ADMIN).
Revoking, reinstating or unlinking a membership needs the authority to grant that
membership's role, so an institution ADMIN manages students but cannot touch teachers.
`POST …/teachers` is the explicit, audited grant of FACULTY/ADMIN; it runs through the
same one-row roster rules as an import.

### 6. OTP: adapter contract, fail-closed delivery, atomic single use
`EXAM_OTP_DELIVERY_PROVIDER` selects the adapter. Empty means none chosen: the synthetic
dev sink outside production and `UnconfiguredOtpDelivery` in production, where
`/auth/challenge` returns the same `503 otp_delivery_not_configured` for every caller
(it says nothing about any identity) — as it does in production without
`EXAM_OTP_HMAC_SECRET`. `dev_sink` cannot be selected in production, and codes are never
logged there. A delivery failure is swallowed so it looks exactly like success. No
provider adapter exists: choosing SSO versus verified-e-mail OTP is institutional
decision D-6. A code is checked and consumed in one transaction. An e-mail OTP proves
control of a mailbox; it is **not** multi-factor authentication and is not described as
such anywhere.

### 7. Linking, conflict recovery and audit
Link runs in a transaction. Two conflicts are refused and persisted to the audit log:
the identity is linked to a different uid (`identity_already_linked`), or this uid
already holds a different identity in the same institution
(`account_already_linked_in_institution`). Success, first-login uid provisioning,
revocation, reinstatement and unlinking are audited. Recovery is `unlink` (detach the
wrong account, bump the version, the rightful owner then links or signs in) and
`reinstate` (the only way back from a revocation). Both need a reason and a recent
sign-in.

### 8. D-2 resolved: a definition is a course; co-instructors are its teachers
The project owner chose "definition = course". `ExamDefinition.instructor_uids`
(optional) lists co-instructors, who may manage that definition's runs, blueprints and
gradebook through the existing ownership chain (`can_manage_resource` gained
`co_owner_uids`). Only a super-admin or an ADMIN of the definition's institution may
change the list (`PUT …/definitions/{id}/instructors`); the owner and co-instructors
cannot hand access on. Assignees must be linked, ACTIVE FACULTY/ADMIN members of that
institution; unscoped definitions cannot have instructors. Access ends with the
membership (teacher authority is recomputed per request). `GET …/definitions` lists
owned and co-instructed definitions. Finer capabilities inside a course (a proctor-only
or grade-only assistant) are not modeled; that would be a new decision.

### 9. Super-admin management surface and re-authentication
Added read routes (institutions the caller can see, members with masked e-mails and no
uids, roster job progress) and the recovery and assignment routes above. Creating an
institution, applying a roster, revoking, reinstating, unlinking, assigning a teacher
and changing instructors require a recent sign-in (`require_recent_staff_reauth`, a
recency check on the Firebase session, not MFA). This closes AUTH-13's authority-changing
cases. There is no UI yet.

### 10. One audit log
Events reuse `exam_authz_audit` (ADR 0017): `institution_created`, `roster_batch_applied`,
`membership_role_granted`, `membership_role_changed`, `membership_contact_changed`,
`teacher_assigned`, `identity_provisioned`, `identity_linked`, `identity_link_conflict`,
`identity_unlinked`, `membership_revoked`, `membership_reinstated`, `instructors_set`.
They carry actor, institution, college ID and reasons — never an e-mail address or a
code.

## Consequences

- **Positive:** the seven defects above are closed behind tests (147 new tests; full
  backend suite 774 passing). No new collection; new fields are additive and optional.
- **Behavior changes operators will notice:** FACULTY/ADMIN roles can only be granted by
  a super-admin; institution ADMINs and FACULTY manage students; a roster cannot change
  an existing member's e-mail unless an ADMIN/super-admin runs it, cannot reactivate a
  revoked member, and cannot be applied with rows other than the validated ones; a
  uid can hold only one identity per institution; production OTP sign-in is unavailable
  until a delivery provider and `EXAM_OTP_HMAC_SECRET` are configured.
- **Residual risks / follow-ups:**
  - The transactional logic is proven with in-memory stand-ins, not an emulator or
    concurrent writers (needs the emulator/staging phase). Sensitive roster rows cost
    one transaction each; a large import that changes many roles is bounded by
    `batch_size` but slow.
  - Rate limits are per identity (and per process unless Redis is configured). Someone
    who knows an identity and its roster e-mail can exhaust that identity's quota; no
    per-IP limit was added.
  - A compromised mailbox compromises the identity: that is inherent to e-mail OTP.
    Changing a contact e-mail is therefore restricted and audited.
  - After an unlink and re-link to a different uid, runs already created still hold the
    old uid in their enrollment snapshot and need re-enrolling (Phase 3, per-attempt
    grant/revoke).
  - An institution's identity policy cannot be changed once members exist (no
    migration tool).
  - Legacy data could already hold several memberships for one uid in an institution;
    new links are prevented from creating that, existing ones are not rewritten.
  - The membership listing loads the whole institution and pages in memory (no
    composite index); fine for thousands of members.
- **Open decisions:** D-6 (SSO versus verified-e-mail OTP and its delivery provider).
  D-2 is resolved as above.

## Migration / configuration steps

See `docs/exams/compatibility-and-migration.md` §7. In short: set
`EXAM_OTP_HMAC_SECRET` and (when the institution chooses) a delivery adapter; no data
migration or backfill; rollback is reverting the commit (the optional fields and audit
events are inert).
