# Exams — accessibility, PWA, and managed-lockdown verification (Prompt 10)

This records exactly what was automatically verified in this environment versus
what still requires a human and/or real devices before production — consistent
with the master prompt's own instruction not to claim mock/automated coverage
proves something only a real environment can prove.

## What was automated (and passes)

**Accessibility**
- `e2e/exams-accessibility.spec.ts` — `@axe-core/playwright` scan of
  `/exams/login` (zero violations after fixing two real findings: duplicate
  unlabeled `<nav>` landmarks site-wide, and a keyboard trap in the code
  editor's Tab handling — see ADR 0015).
- `e2e/exams-accessibility.spec.ts` — keyboard-only reachability of the
  login form's submit button.
- `CodeEditorPanel.test.tsx` — regression test that Tab moves focus to the
  next control rather than inserting spaces (WCAG 2.1.2 no keyboard trap).

**PWA**
- `e2e/exams-pwa.spec.ts` — real service worker registration in a real
  Chromium browser; precached files actually present in cache storage;
  a navigation to an exam page while genuinely offline (`context.setOffline`)
  shows the offline explanation page, not a browser error page.
- `useExamServiceWorker.test.ts` — update suppression while an attempt is
  active, release once it ends, and (after a real bug was found via the
  Playwright test above) that a page is NOT reloaded on its first-ever SW
  activation, only on a genuine update to an already-controlled page.
- `useCameraCapture.test.ts` — camera permission is never requested when
  `camera_waived` is set; an honest `camera_waived_accommodation` reason is
  recorded instead.

**Managed lockdown**
- `test_lockdown_decisions.py` — SEB Config Key / Browser Exam Key header
  verification against an independently-computed SHA256 reference vector
  (not just round-tripped against the function under test), including the
  fail-closed "enabled but unconfigured" case.
- `test_lockdown_service.py` — every admission attempt (pass or fail) is
  audited.

## What was NOT performed in this environment (explicit, not silently skipped)

**Manual accessibility testing** — no real screen reader (NVDA/JAWS/VoiceOver/
TalkBack), no real zoom/reflow testing on an actual device, no real
switch-access or voice-control testing. Before enabling this module for a real
cohort, a human must manually verify, at minimum:
- [ ] Full exam flow (login → admit → question → save draft → submit) is
      operable with NVDA or VoiceOver, including the code editor fallback.
- [ ] Page zoom to 200% and 400% does not clip or overlap content on the
      exam question screen, the header (timer/camera/save status), and the
      faculty gradebook table.
- [ ] Focus order and visible focus indicators are correct on every exam
      screen listed in Prompt 7/8/9 (not just the login page, which is all
      the automated axe scan covers today).
- [ ] Color contrast in both light and dark theme meets WCAG AA (axe's
      `color-contrast` rule was NOT reliably evaluable under Playwright's
      headless rendering in every case — spot-check manually).

**Managed-lockdown (SEB) real-client pilot** — nothing in this phase has been
run against an actual Safe Exam Browser installation. Specifically unverified:
- [ ] A real `.seb` config file, opened in a real SEB client (Windows and
      macOS), actually sends `X-SafeExamBrowser-ConfigKeyHash` /
      `X-SafeExamBrowser-RequestHash` matching what `compute_seb_hash`
      expects.
- [ ] OS/client version behavior: which SEB versions actually support the
      classic WebView header mechanism this backend checks (the SEB
      JavaScript API, a newer alternative mechanism, is not implemented at
      all in this phase).
- [ ] Camera/editor/network behavior INSIDE a real SEB kiosk session (e.g.
      does the plain-textarea code editor work identically; does the
      `Permissions-Policy: camera=(self)` header interact correctly with
      SEB's own camera access).
- [ ] Permitted tools / exit / recovery: quit password flow, URL filtering,
      and what happens if SEB crashes mid-exam.
- [ ] Only platforms an institution has ACTUALLY piloted should ever be
      added to a run's `piloted_platforms` list — this is a declarative,
      admin-attested field (`app/domain/lockdown/models.py`), not something
      this system can verify automatically (no header SEB sends in the
      classic mechanism reliably identifies OS/client version).

**PWA installation** — no automated test simulates a real "Add to Home
Screen"/install-prompt flow (`beforeinstallprompt` is unreliable to trigger
deterministically cross-platform in Playwright). Before production:
- [ ] Manually install the PWA on at least one Android/Chrome and one
      desktop/Chrome device; confirm it opens scoped to `/exams/` only.
- [ ] Confirm an installed PWA, opened DURING an active attempt, still
      correctly defers any pending service-worker update until the attempt
      ends (mirrors the automated `useExamServiceWorker` test, but on a real
      installed app rather than a browser tab).
- [ ] Multi-account logout: sign out one institutional account and sign in
      as a different one on the same device/installed PWA; confirm no
      cached exam content or token from the previous account is reachable.
- [ ] Old-client/new-deploy: with an already-installed PWA from a prior
      deployment, ship a new deployment and confirm the update banner/
      reload behavior matches the "update after completion" contract in
      practice, not just in the unit/e2e simulation.

## Support matrix (document observed, not assumed)

| Platform / client | PWA install | Camera capture | Managed lockdown (SEB) |
|---|---|---|---|
| Desktop Chrome/Edge (Chromium) | Automated e2e only (real install untested) | Untested in this environment (no real camera) | Untested — no real SEB client available |
| Desktop Firefox | Untested | Untested | Untested |
| Desktop Safari | Untested | Untested | Untested (SEB for macOS uses a different WebView mechanism — see ADR 0015) |
| Android Chrome | Untested | Untested | N/A (no official SEB build referenced for Android in this phase) |
| iOS Safari | Untested | Untested | Untested (SEB for iOS exists but not piloted here) |
| Safe Exam Browser (any platform/version) | N/A (strict mode denies PWA-only installs) | Untested | Untested — header verification logic is implemented and unit-tested; never exercised against a real SEB binary |

Every cell not marked "automated e2e" in this table is **unsupported until a
human explicitly pilots and records it** — the master prompt is explicit that
untested platforms must be marked unsupported, not assumed to work by browser
parity.
