/**
 * Exam PWA static-shell service worker (Prompt 10).
 *
 * Caches ONLY the explicit assets listed in PRECACHE_URLS — the offline
 * explanation page and the few static assets it needs. Everything else
 * (every navigation to an actual exam page, every /api/ call — questions,
 * drafts, submissions, evidence, results) is deliberately NEVER intercepted
 * here: this worker never calls `event.respondWith()` for those requests,
 * so they always go straight to the network exactly as if no service
 * worker existed. There is no offline queueing of any kind — a failed
 * network request while offline simply fails; nothing is cached, retried,
 * or synced later. No final exam submission can ever be accepted offline.
 *
 * Update suppression: this worker does NOT call `self.skipWaiting()` on
 * install. A new version sits in the browser's normal "waiting" state
 * until the page explicitly posts {type: "SKIP_WAITING"} — which the app
 * (see useExamServiceWorker.ts) only ever does when it knows no exam
 * attempt is currently in progress. This is what prevents a mid-attempt
 * reload/activation.
 */

const CACHE_VERSION = "exams-shell-v1";
const PRECACHE_URLS = [
  "/exams-offline.html",
  "/exams-manifest.webmanifest",
  "/logo.svg",
  "/favicon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE_URLS))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("exams-shell-") && key !== CACHE_VERSION)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only ever serve from cache for an EXACT match against the explicit
  // precached static asset list. Never a generic same-origin cache-first
  // strategy — that would risk silently serving stale JS/CSS/API-shaped
  // responses.
  const url = new URL(request.url);
  if (request.method === "GET" && PRECACHE_URLS.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request))
    );
    return;
  }

  // Navigations within the exam scope: network-only, falling back to the
  // offline explanation page ONLY when the network request itself fails
  // (true offline) — a normal HTTP error response (4xx/5xx) is NOT
  // intercepted, so the app's own error handling/auth redirects still work.
  if (request.mode === "navigate" && url.pathname.startsWith("/exams/")) {
    event.respondWith(
      fetch(request).catch(() => caches.match("/exams-offline.html"))
    );
    return;
  }

  // Everything else (all /api/ calls, all other requests) — deliberately
  // not intercepted at all. No event.respondWith() call means the browser
  // handles it exactly as if this service worker did not exist.
});
