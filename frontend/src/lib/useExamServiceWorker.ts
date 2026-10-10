"use client";

import { useEffect } from "react";

/**
 * Number of mounted components currently holding an exam attempt open. Module-level on purpose: the exam layout
 * mounts one instance of this hook for every exam page and the exam-taking page mounts a second one, so a
 * per-instance flag lets the layout's instance (which never knows about the attempt) activate a waiting service
 * worker - and reload the page - in the middle of an attempt.
 */
let activeAttemptHolds = 0;

const isAttemptActive = () => activeAttemptHolds > 0;

function releaseWaitingWorker(registration: ServiceWorkerRegistration | undefined | null) {
  if (isAttemptActive()) return;
  registration?.waiting?.postMessage({ type: "SKIP_WAITING" });
}

/**
 * Registers the exam PWA's static-shell service worker and controls when a newer version is allowed to take over.
 *
 * `attemptActive` is the ONLY unsafe moment: telling a waiting worker to `skipWaiting()` (and the controllerchange
 * reload that follows). While ANY mounted component holds an attempt open, a pending update stays waiting and no
 * update reload happens; it is released once the last holder lets go (attempt submitted/finalized, or the page
 * unmounts). Safe to mount from any number of components at once.
 */
export function useExamServiceWorker(options?: { attemptActive?: boolean }): void {
  const attemptActive = options?.attemptActive ?? false;

  // Hold / release the shared attempt lock; releasing the last hold lets a waiting update through.
  useEffect(() => {
    if (!attemptActive) return;
    activeAttemptHolds += 1;
    return () => {
      activeAttemptHolds -= 1;
      if (typeof navigator === "undefined" || !navigator.serviceWorker) return;
      navigator.serviceWorker
        .getRegistration("/exams/")
        .then((registration) => releaseWaitingWorker(registration))
        .catch(() => undefined);
    };
  }, [attemptActive]);

  useEffect(() => {
    if (typeof navigator === "undefined" || !navigator.serviceWorker) return;

    let cancelled = false;
    // A page's VERY FIRST load under a newly-registered service worker transitions from "no controller" to "has
    // controller" exactly like an update does - but it is not an update and must never reload the page. Only an
    // ALREADY-controlled page taking on a DIFFERENT controller is a real update.
    const hadControllerAtMount = !!navigator.serviceWorker.controller;

    navigator.serviceWorker
      .register("/exams-sw.js", { scope: "/exams/" })
      .then((registration) => {
        if (cancelled) return;
        releaseWaitingWorker(registration);
        registration.addEventListener("updatefound", () => {
          const installing = registration.installing;
          installing?.addEventListener("statechange", () => {
            if (installing.state === "installed" && registration.waiting) {
              releaseWaitingWorker(registration);
            }
          });
        });
      })
      .catch(() => {
        // Registration failures (unsupported browser, disabled SW, etc.) never block the exam itself - the app works
        // identically without a service worker, just without the installable/offline-explainer shell.
      });

    let reloadedOnce = false;
    const onControllerChange = () => {
      if (!hadControllerAtMount || reloadedOnce) return;
      // Never reload under an active attempt. (The page keeps running its current code; the shell cache holds no
      // app code, so nothing is stale.)
      if (isAttemptActive()) return;
      reloadedOnce = true;
      window.location.reload();
    };
    navigator.serviceWorker.addEventListener("controllerchange", onControllerChange);

    return () => {
      cancelled = true;
      navigator.serviceWorker?.removeEventListener("controllerchange", onControllerChange);
    };
  }, []);
}
