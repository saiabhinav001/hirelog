import { expect, test } from "@playwright/test";

/**
 * Real-browser checks of the exam PWA cache boundary (Prompt 10): what the framework sends, and what the shipped
 * service worker actually stores after a session. Not covered here (manual / pilot): a CDN in front of the app,
 * installed-app behaviour, account switching across separate browser profiles, and active-attempt update suppression
 * against a real new deploy (unit-tested in src/lib/useExamServiceWorker.test.ts instead).
 */
test("exam pages are never publicly cacheable", async ({ request }) => {
  // The dev server rewrites Cache-Control itself ("no-cache, must-revalidate"); the exact production value
  // (`private, no-store`, from next.config.ts) is asserted in src/app/exams/examHeaders.test.ts and was checked
  // against `next start`. Either way nothing here may let a browser, proxy or CDN reuse the page.
  const response = await request.get("/exams/login");
  expect(response.status()).toBe(200);
  const cacheControl = response.headers()["cache-control"] ?? "";
  expect(cacheControl).toMatch(/no-store|no-cache/);
  expect(cacheControl).not.toMatch(/public|s-maxage|immutable|max-age=[1-9]/);
});

test("after browsing the exam area the service worker has cached only the static shell", async ({ page }) => {
  await page.goto("/exams/login");
  await page.evaluate(() => navigator.serviceWorker.ready);
  // Visit more exam routes so any accidental runtime caching would have a chance to happen.
  for (const path of ["/exams/student", "/exams/login"]) {
    await page.goto(path);
  }
  await page.waitForTimeout(500);

  const cached: Record<string, string[]> = await page.evaluate(async () => {
    const out: Record<string, string[]> = {};
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      out[name] = (await cache.keys()).map((r) => new URL(r.url).pathname).sort();
    }
    return out;
  });

  expect(Object.keys(cached).filter((k) => !k.startsWith("exams-shell-"))).toEqual([]);
  const shell = Object.values(cached).flat().sort();
  expect(shell).toEqual(["/exams-manifest.webmanifest", "/exams-offline.html", "/favicon.svg", "/logo.svg"]);
});
