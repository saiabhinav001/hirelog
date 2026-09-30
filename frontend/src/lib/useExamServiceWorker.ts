"use client";

import { useEffect, useRef } from "react";

/**
 * Registers the exam PWA's static-shell service worker and controls when a
 * newer version is allowed to take over.
 *
 * `attemptActive` gates the ONLY unsafe moment: telling a waiting worker to
 * `skipWaiting()`. While `true`, a pending update is left waiting — it is
 * only released once this hook is called again with `attemptActive` false
 * (or omitted), which happens naturally once an attempt is submitted/
 * finalized and the exam page unmounts or re-renders with the new status.
 * This hook is safe to mount from multiple components simultaneously
 * (registering the same scriptURL+scope is idempotent).
 */
export function useExamServiceWorker(options?: { attemptActive?: boolean }): void {
  const attemptActive = options?.attemptActive ?? false;
  const attemptActiveRef = useRef(attemptActive);
  useEffect(() => {
    attemptActiveRef.current = attemptActive;
  }, [attemptActive]);

  useEffect(() => {
    if (typeof navigator === "undefined" || !navigator.serviceWorker) return;

    let cancelled = false;
    // A page's VERY FIRST load under a newly-registered service worker
    // transitions from "no controller" to "has controller" exactly like an
    // update does — but it is not an update and must never reload the page
    // (that reload was, in an earlier version of this hook, destroying the
    // page's JS execution context immediately after first install). Only
    // an ALREADY-controlled page taking on a DIFFERENT controller is a real
    // update.
    const hadControllerAtMount = !!navigator.serviceWorker.controller;

    const releaseWaitingWorkerIfSafe = (registration: ServiceWorkerRegistration) => {
      if (attemptActiveRef.current) return;
      registration.waiting?.postMessage({ type: "SKIP_WAITING" });
    };

    navigator.serviceWorker
      .register("/exams-sw.js", { scope: "/exams/" })
      .then((registration) => {
        if (cancelled) return;
        releaseWaitingWorkerIfSafe(registration);
        registration.addEventListener("updatefound", () => {
          const installing = registration.installing;
          installing?.addEventListener("statechange", () => {
            if (installing.state === "installed" && registration.waiting) {
              releaseWaitingWorkerIfSafe(registration);
            }
          });
        });
      })
      .catch(() => {
        // Registration failures (unsupported browser, disabled SW, etc.)
        // never block the exam itself — the app works identically without
        // a service worker, just without the installable/offline-explainer shell.
      });

    let reloadedOnce = false;
    const onControllerChange = () => {
      if (!hadControllerAtMount || reloadedOnce) return;
      reloadedOnce = true;
      window.location.reload();
    };
    navigator.serviceWorker.addEventListener("controllerchange", onControllerChange);

    return () => {
      cancelled = true;
      navigator.serviceWorker?.removeEventListener("controllerchange", onControllerChange);
    };
  }, []);

  // Re-evaluate whenever attemptActive flips to false (e.g. exam just
  // finished) — release any update that had been held back.
  useEffect(() => {
    if (attemptActive) return;
    if (typeof navigator === "undefined" || !navigator.serviceWorker) return;
    navigator.serviceWorker.getRegistration("/exams/").then((registration) => {
      registration?.waiting?.postMessage({ type: "SKIP_WAITING" });
    });
  }, [attemptActive]);
}
