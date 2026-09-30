import { expect, test } from "@playwright/test";

/**
 * Exercises the REAL service worker in a REAL browser (Playwright/
 * Chromium) — unlike useExamServiceWorker.test.ts (jsdom, mocked
 * `navigator.serviceWorker`), this verifies the actual /exams-sw.js file
 * registers, precaches the offline shell, and serves it when a navigation
 * genuinely fails offline. What this does NOT cover: real "Add to Home
 * Screen" installation (no reliable cross-platform Playwright API for
 * that), and multi-account/old-client/new-deploy scenarios that need a
 * previously-installed app across real browser sessions — see
 * docs/exams/accessibility-and-lockdown-checklist.md for what remains a
 * manual/pilot-only verification.
 */
test("service worker registers and precaches the offline shell on an exam page", async ({ page }) => {
  await page.goto("/exams/login");
  await page.evaluate(() => navigator.serviceWorker.ready);

  const cachedUrls: string[] = await page.evaluate(async () => {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const keys = await caches.keys();
      const shellKey = keys.find((k) => k.startsWith("exams-shell-"));
      if (shellKey) {
        const cache = await caches.open(shellKey);
        const requests = await cache.keys();
        const paths = requests.map((r) => new URL(r.url).pathname);
        if (paths.length > 0) return paths;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return [];
  });

  expect(cachedUrls).toContain("/exams-offline.html");
  expect(cachedUrls).toContain("/exams-manifest.webmanifest");
});

test("a failed exam-page navigation while offline shows the offline explanation, never a browser error page", async ({ page, context }) => {
  await page.goto("/exams/login");
  await page.waitForFunction(() => navigator.serviceWorker.getRegistration("/exams/").then((r) => !!r));
  // Give the SW a moment to finish precaching after the registration resolves.
  await page.waitForTimeout(500);

  await context.setOffline(true);
  await page.goto("/exams/login");

  await expect(page.getByRole("heading", { name: "You're offline" })).toBeVisible();
  await expect(page.getByText(/cannot submit a final answer while offline/i)).toBeVisible();

  await context.setOffline(false);
});
