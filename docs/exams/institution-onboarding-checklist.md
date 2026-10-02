# Institution Onboarding Checklist

Concrete, provider-level decisions an adopting institution must make before a
real exam is scheduled. Every item below has a safe, non-functional default
today (listed) so the module can be deployed and demoed without any of this
— but each unconfigured item is a real functional gap, not a nice-to-have.
Compiled from every ADR's "institutional decisions" notes and restated once
here for release planning — see
[production-qualification-report.md §9](production-qualification-report.md)
for the same table with source ADR references.

## Identity & access

- [ ] Decide faculty-authority source: `EXAM_FACULTY_EMAILS` (quick pilot,
      config-only) vs. real institution/roster membership (ADR 0010,
      `institutions`/`institution_memberships` collections) — these are two
      independent grant paths, whichever matches first wins.
- [ ] If using institution membership: import the real student/faculty
      roster (identity keys + contact emails) and decide the OTP delivery
      provider (SMS/email) — default is `DevSinkOtpDelivery`, a dev-only sink
      that does not actually deliver anything.
- [ ] Set `EXAM_OTP_HMAC_SECRET` to a stable, shared secret for any
      multi-instance deployment — the default is a random per-process
      secret, which breaks OTP verification across restarts/instances.

## Content & grading

- [ ] Decide the generation-model provider for KT-sourced question drafting
      (`EXAM_GENERATION_PROVIDER_BASE_URL`/`_API_KEY`) — default is a
      deterministic fake, never a real model.
- [ ] Decide the malware-scan provider for uploaded KT source material
      (`EXAM_MALWARE_SCANNER_ENDPOINT`) — default (`NullMalwareScanner`)
      never reports a file "clean", which is a safe-fail but blocks real use.
- [ ] If scanned/image-based documents are expected as KT sources: an OCR
      provider is not implemented in this system at all yet.
- [ ] Provision a real Judge0-compatible, hardened, isolated compute
      instance for coding-question grading
      (`EXAM_JUDGE_PROVIDER_BASE_URL`/`_API_KEY`) — default is a
      deterministic fake judge that never executes submitted code.
- [ ] Provision a real JPlag CLI jar + Java runtime for similarity/plagiarism
      review (`EXAM_JPLAG_JAR_PATH`, `EXAM_JPLAG_JAVA_EXECUTABLE`) — default
      is a deterministic fake similarity provider.

## Proctoring & evidence

- [ ] Provision a real evidence storage bucket
      (`EXAM_EVIDENCE_STORAGE_BUCKET`, a Firebase Storage bucket) — default
      is in-memory storage that does not survive a process restart.
- [ ] Review and explicitly set `EXAM_EVIDENCE_RETENTION_DAYS` (default 90)
      to match institutional data-retention policy.
- [ ] Decide per-student camera accommodations deliberately
      (`camera_waived` is explicit/faculty-authored per ADR 0013/0015, never
      auto-derived from a permission-denied event).

## Managed lockdown (Safe Exam Browser)

- [ ] Only enable a lockdown profile for platforms you have **actually
      piloted** with a real SEB client — `piloted_platforms` is a
      declarative, admin-attested field; the system cannot verify OS/client
      version automatically (ADR 0015).
- [ ] Generate and record the real SEB Config Key / Browser Exam Key values
      for your `.seb` configuration file.
- [ ] Complete the real-client pilot checklist in
      [accessibility-and-lockdown-checklist.md](accessibility-and-lockdown-checklist.md)
      (quit-password flow, URL filtering, camera-in-SEB behavior, crash
      recovery) before relying on lockdown for a graded exam.

## Accessibility

- [ ] Complete the manual verification checklist in
      [accessibility-and-lockdown-checklist.md](accessibility-and-lockdown-checklist.md)
      (real screen reader, zoom/reflow, focus order, color contrast) — only
      the login page has been automatically axe-scanned so far.
- [ ] Confirm PWA install/update behavior on at least one real Android and
      one real desktop device (also in that checklist).

## Operational readiness

- [ ] Re-run the load test scripts
      (`backend/scripts/exam_load_test.py`,
      `backend/scripts/exam_staging_seed.py`) against your real staging
      Firestore project (not the local emulator) at your expected cohort
      size — see
      [production-qualification-report.md §3](production-qualification-report.md)
      for why the numbers in this repo are local/emulator-only.
- [ ] Decide real cost expectations once real usage data exists — not
      estimated from local load-test numbers (§8 of the qualification
      report).
- [ ] Read [rollback-runbook.md](rollback-runbook.md) and confirm whoever is
      on call for your first real exam knows the three rollback levers and
      when to use each.
- [ ] Add the exam-specific section of
      [backup-dr.md](../runbooks/backup-dr.md) to your incident-response
      documentation.
- [ ] Upgrade `next`/`starlette`/`transformers` per
      [production-qualification-report.md §4](production-qualification-report.md)
      before going live — pre-existing dependency findings, not
      exam-specific, but blocking for a real production release of this
      codebase.
